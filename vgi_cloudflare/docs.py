"""Agent- and human-facing metadata for every catalog object, built from descriptors.

Everything here is derived from facts the descriptor carries — the OpenAPI summary
and long-form description, the endpoint, path/query parameters with their docs,
and the result columns with their docs — so a generated doc says *what the object
returns, what it requires, and how to call it*, never just a restated name.

Docs are prose: runnable SQL lives in ``vgi.example_queries`` (never inline in a
doc), and a function's docs name its arguments without repeating their own
descriptions (those travel with the argument metadata).

Roles:

- ``table``     a list endpoint scanned with a filter on its path params
- ``fanout``    the same list called once per input row (``<table>_by_<parent>``)
- ``lookup``    a get endpoint called with its path ids (0/1 row per call)
- ``singleton`` a parameterless get endpoint (also exposed as a table)
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pyarrow as pa

from .descriptor import Example, ResourceDescriptor

CATALOG = "cloudflare"

#: Agent tools show at most this much of a tag (vgi-lint VGI417).
MAX_TAG_CHARS = 4000

# Parents with a canonical table to drive fan-out examples from.
_PARENT_TABLES = {"zone": ("zones", "zones"), "account": ("accounts", "accounts")}

# DuckDB type names that must be code-formatted when they appear in prose.
_TYPE_NAMES = (
    "TIMESTAMPTZ UHUGEINT UBIGINT UINTEGER USMALLINT UTINYINT HUGEINT SMALLINT TINYINT TIMESTAMP "
    "INTERVAL NUMERIC DECIMAL BOOLEAN VARCHAR INTEGER BIGINT VARINT DOUBLE STRUCT FLOAT BLOB DATE "
    "LIST REAL UUID MAP"
).split()
_TYPE_MENTION = re.compile(r"(?<![\w.`])(" + "|".join(_TYPE_NAMES) + r")(?![\w`])")


def duckdb_type(t: pa.DataType) -> str:
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "VARCHAR"
    if pa.types.is_boolean(t):
        return "BOOLEAN"
    if pa.types.is_integer(t):
        return "BIGINT"
    if pa.types.is_floating(t):
        return "DOUBLE"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_timestamp(t):
        return "TIMESTAMP WITH TIME ZONE" if t.tz else "TIMESTAMP"
    return "VARCHAR"


def field_doc(f: pa.Field) -> str:
    meta = f.metadata or {}
    return (meta.get(b"comment") or b"").decode()


def result_columns_schema(schema: pa.Schema) -> str:
    """``vgi.result_columns_schema``: the static result shape of a table function.

    Descriptions are shortened (first sentence, then a hard cap) only as far as
    needed to fit the agent context budget; every column stays listed.
    """

    def render(limit: int | None) -> str:
        cols = []
        for f in schema:
            doc = field_doc(f)
            if limit is not None and len(doc) > limit:
                first = doc.split(". ")[0].rstrip(".") + "."
                doc = first if len(first) <= limit else doc[: limit - 1].rsplit(" ", 1)[0] + "…"
            cols.append({"name": f.name, "type": duckdb_type(f.type), "description": doc})
        return json.dumps(cols)

    out = render(None)
    for limit in (200, 120, 80, 50, 30, 20, 12):
        if len(out) <= MAX_TAG_CHARS:
            break
        out = render(limit)
    return out


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "general"


_ACRONYMS = {"ai", "api", "dns", "ssl", "tls", "dlp", "dex", "r2", "d1", "waf", "acm", "csrs", "cni", "ip"}
_ACRONYMS |= {"ips", "rum", "mtls", "ct", "dcv", "sso", "oauth", "iam", "bgp", "http", "moq", "dls", "zt"}
_ACRONYMS |= {"cfd", "url", "asn"}


def humanize(text: str) -> str:
    words = text.replace("-", "_").split("_")
    return " ".join(w.upper() if w in _ACRONYMS else w.capitalize() for w in words if w)


_SQLISH_SPAN = re.compile(r"`((?:select|with|from|create|insert|update|delete|copy|values)\s[^`]*)`", re.I)


def prose(text: str) -> str:
    """Spec-derived text made safe for docs.

    DuckDB type names are code-formatted, and a code span that reads like a SQL
    statement (Cloudflare's "`Create a meeting` API") becomes a quotation, since
    docs keep runnable SQL in examples only.
    """
    return _TYPE_MENTION.sub(r"`\1`", _SQLISH_SPAN.sub(r'"\1"', text))


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _first_paragraph(md: str, limit: int = 400) -> str:
    """The first prose paragraph of a Markdown doc, flattened to one line."""
    for para in re.split(r"\n\s*\n", md.strip()):
        para = para.strip()
        if para and not para.startswith(("#", "```", "|", "-", "*", ">")):
            flat = " ".join(para.split())
            return flat if len(flat) <= limit else flat[: limit - 1].rsplit(" ", 1)[0] + "…"
    return ""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _extra_detail(desc: ResourceDescriptor) -> str:
    """The endpoint description's first paragraph, unless it just repeats the summary."""
    first = _first_paragraph(desc.doc)
    return "" if not first or _norm(first) == _norm(desc.description) else prose(first)


def _by_id(desc: ResourceDescriptor) -> bool:
    """True when the URL ends in a path param (a lookup by id), not a fixed segment."""
    return desc.path.rstrip("/").endswith("}")


def _noun(desc: ResourceDescriptor) -> str:
    """What one row is: the singular of the resource name ("records" -> "record")."""
    name = re.sub(r"_\d+$", "", desc.name.removesuffix("_by_id"))
    words = name.split("_")
    last = words[-1]
    if desc.kind == "list":
        if last.endswith("ies"):
            last = last[:-3] + "y"
        elif last.endswith("s") and not last.endswith("ss"):
            last = last[:-1]
    return " ".join([*words[:-1], last])


def _placeholder(name: str) -> str:
    return f"'<{name}>'"


def _summary(desc: ResourceDescriptor) -> str:
    return prose(_sentence(desc.description)) if desc.description else f"Cloudflare GET {desc.path}."


def comment(desc: ResourceDescriptor) -> str:
    """The object's one-line description: the spec summary, with the endpoint when it's terse."""
    base = _sentence(desc.description)
    if len(base) < 40 or _norm(base) == _norm(desc.name):
        return f"{base.rstrip('.') or humanize(desc.name)} (Cloudflare GET {desc.path})."
    return base


#: DuckDB reserved words (``duckdb_keywords()``): must be quoted as identifiers.
_RESERVED = set(
    "all analyse analyze and any array as asc asymmetric both case cast check collate column constraint "
    "create default deferrable desc describe distinct do else end except false fetch for foreign from group "
    "having in initially intersect into lambda lateral leading limit not null offset on only or order pivot "
    "pivot_longer pivot_wider placing primary qualify references returning select show some summarize "
    "symmetric table then to trailing true union unique unpivot using variadic when where window with".split()
)


def ident(name: str) -> str:
    """A column name usable in example SQL (quoted when reserved or not a plain identifier)."""
    return f'"{name}"' if name in _RESERVED or not re.fullmatch(r"[a-z_][a-z0-9_]*", name) else name


def _key_columns(desc: ResourceDescriptor, n: int = 3) -> list[str]:
    """A few informative columns to project in examples (id/name first), quoted as needed."""
    names = [c.name for c in desc.columns]
    preferred = [c for c in ("id", "name", "status", "type", "created_on") if c in names]
    rest = [c for c in names if c not in preferred]
    return [ident(c) for c in ((preferred + rest)[:n] or names[:1])]


def _scope_noun(desc: ResourceDescriptor) -> str:
    return " and one ".join(p.name.removesuffix("_id") for p in desc.path_params)


def _args(desc: ResourceDescriptor) -> str:
    return ", ".join(p.name for p in desc.path_params)


# ---------------------------------------------------------------------------
# Examples (the only place runnable SQL appears)
# ---------------------------------------------------------------------------


def _qualified(desc: ResourceDescriptor, name: str | None = None) -> str:
    return f"{CATALOG}.{desc.schema}.{name or desc.name}"


def table_examples(desc: ResourceDescriptor) -> list[Example]:
    """Examples that scan the table itself (fan-out examples live on the fan-out)."""
    cols = ", ".join(_key_columns(desc))
    where = " AND ".join(f"{ident(p.name)} = {_placeholder(p.name)}" for p in desc.path_params)
    scope = ""
    if desc.path_params:
        verb = "is" if len(desc.path_params) == 1 else "are"
        scope = f" for one {_scope_noun(desc)} ({_args(desc)} {verb} required: each maps to the URL path)"
    out = [
        Example(
            f"SELECT {cols} FROM {_qualified(desc)}" + (f" WHERE {where}" if where else ""),
            f"List {_noun(desc)}s{scope}.",
        )
    ]
    pushed = [q for q in desc.query_params if q.name in {c.name for c in desc.columns}]
    if pushed:
        q = pushed[0]
        cond = " AND ".join([*([where] if where else []), f"{ident(q.name)} = {_placeholder(q.name)}"])
        out.append(
            Example(
                f"SELECT {cols} FROM {_qualified(desc)} WHERE {cond}",
                f"Only {_noun(desc)}s with a given {q.name}: the {q.name} filter is sent to the API.",
            )
        )
    elif len(desc.path_params) == 1:
        p = desc.path_params[0].name
        out.append(
            Example(
                f"SELECT {ident(p)}, count(*) AS n FROM {_qualified(desc)} "
                f"WHERE {ident(p)} IN ({_placeholder(p + '_1')}, {_placeholder(p + '_2')}) "
                f"GROUP BY {ident(p)}",
                f"Count {_noun(desc)}s for several {p.removesuffix('_id')}s at once (one API call each).",
            )
        )
    return out


def fanout_example(desc: ResourceDescriptor, fanout: str) -> Example:
    *outer, last = desc.path_params
    parent = last.name.removesuffix("_id").removesuffix("_identifier")
    cols = ", ".join(f"r.{c}" for c in _key_columns(desc, 2))
    literal_args = [_placeholder(p.name) for p in outer]
    if parent in _PARENT_TABLES and not outer:
        schema, table = _PARENT_TABLES[parent]
        sql = (
            f"SELECT p.name AS {parent}, {cols} FROM {CATALOG}.{schema}.{table} p, "
            f"{_qualified(desc, fanout)}(p.id) r"
        )
        what = f"Every {_noun(desc)} across all of your {parent}s, one API call per {parent}."
    else:
        args = ", ".join([*literal_args, f"t.{last.name}"])
        sql = (
            f"SELECT t.{last.name}, {cols} FROM (VALUES ({_placeholder(last.name)})) t({last.name}), "
            f"{_qualified(desc, fanout)}({args}) r"
        )
        what = f"{humanize(_noun(desc))}s for each {last.name} in another relation (one API call per row)."
    return Example(sql, what)


def lookup_examples(desc: ResourceDescriptor) -> list[Example]:
    cols = ", ".join(_key_columns(desc))
    args = ", ".join(_placeholder(p.name) for p in desc.path_params)
    first = (
        f"Fetch one {_noun(desc)} by {_args(desc)}; an unknown id returns no rows."
        if _by_id(desc)
        else f"Read the {_noun(desc)} object for one {_scope_noun(desc)}."
    )
    out = [Example(f"SELECT {cols} FROM {_qualified(desc)}({args})", first)]
    if len(desc.path_params) == 1:
        p = desc.path_params[0].name
        out.append(
            Example(
                f"SELECT t.{p}, d.* EXCLUDE ({p}) FROM (VALUES ({_placeholder(p)})) t({p}) "
                f"LEFT JOIN LATERAL {_qualified(desc)}(t.{p}) d ON true",
                f"Read the {_noun(desc)} for each row of another relation, keeping rows with no match.",
            )
        )
    return out


def singleton_examples(desc: ResourceDescriptor) -> list[Example]:
    cols = ", ".join(_key_columns(desc))
    return [Example(f"SELECT {cols} FROM {_qualified(desc)}", f"Read the {_noun(desc)}: {_summary(desc)}")]


def example_tag(examples: Iterable[Example]) -> str:
    return json.dumps([{"description": e.description, "sql": e.sql} for e in examples])


# ---------------------------------------------------------------------------
# Object docs
# ---------------------------------------------------------------------------


def _columns_md(schema: pa.Schema, budget: int) -> str:
    """A column table, cut short (with a count) to fit ``budget`` characters."""
    rows = ["| Column | Type | Description |", "| --- | --- | --- |"]
    used = sum(len(r) + 1 for r in rows)
    for i, f in enumerate(schema):
        doc = prose(field_doc(f)).replace("|", "\\|") or "—"
        row = f"| `{f.name}` | {duckdb_type(f.type)} | {doc} |"
        if used + len(row) + 80 > budget:
            rows.append(f"\n…and {len(schema) - i} more columns (`DESCRIBE` the object to see all).")
            break
        rows.append(row)
        used += len(row) + 1
    return "\n".join(rows)


def _md(
    title: str, intro: str, sections: list[tuple[str, str]], schema: pa.Schema, *, column_table: bool = True
) -> str:
    """Assemble ``doc_md`` within the agent context budget; the column table gets what's left.

    Functions without a backing table declare their columns in
    ``vgi.result_columns_schema``, so their doc just counts them.
    """
    head = f"# {title}\n\n{intro}\n\n" + "".join(f"## {h}\n\n{body}\n\n" for h, body in sections if body)
    if len(head) > MAX_TAG_CHARS - 400:
        head = head[: MAX_TAG_CHARS - 401].rsplit("\n", 1)[0] + "\n\n"
    if not column_table:
        return head + f"Returns {len(schema)} columns, described in the declared result schema.\n"
    return head + "## Columns\n\n" + _columns_md(schema, MAX_TAG_CHARS - len(head) - 20) + "\n"


def _endpoint_detail(desc: ResourceDescriptor) -> str:
    text = prose(desc.doc.strip())
    return text if len(text) <= 1500 else text[:1499].rsplit(" ", 1)[0] + "…"


@dataclass(frozen=True)
class ObjectDocs:
    comment: str
    doc_llm: str
    doc_md: str
    examples: tuple[Example, ...]


def table_docs(desc: ResourceDescriptor, schema: pa.Schema, fanout: str | None) -> ObjectDocs:
    noun = _noun(desc)
    llm = [f"{_summary(desc)} Each row is one {noun} from Cloudflare GET {desc.path}."]
    filters = ""
    if desc.path_params:
        verb = "maps" if len(desc.path_params) == 1 else "map"
        llm.append(
            f"A scan must filter {_args(desc)} with = or IN (it {verb} to the URL path), so start from a "
            f"known id."
        )
        filters = "\n".join(
            f"- `{p.name}` — {prose(p.doc) or 'URL path parameter.'}" for p in desc.path_params
        )
    pushed = [q for q in desc.query_params if q.name in {c.name for c in desc.columns}]
    if pushed:
        llm.append(
            f"Equality filters on {', '.join(q.name for q in pushed)} are sent to the API; "
            "other filters run in DuckDB."
        )
    if fanout:
        llm.append(
            f"To take {desc.path_params[-1].name} values from another table instead of constants, use the "
            f"function {_qualified(desc, fanout)}."
        )
    if extra := _extra_detail(desc):
        llm.append(extra)
    md = _md(
        _qualified(desc),
        f"{_summary(desc)} One row per {noun}, from Cloudflare GET `{desc.path}`.",
        [
            ("Endpoint", _endpoint_detail(desc)),
            ("Required filters", filters),
            ("Filters sent to the API", ", ".join(f"`{q.name}`" for q in pushed)),
            ("Per-row form", f"`{_qualified(desc, fanout)}` takes the ids per input row." if fanout else ""),
        ],
        schema,
    )
    return ObjectDocs(comment(desc), " ".join(llm), md, tuple(table_examples(desc)))


def fanout_docs(desc: ResourceDescriptor, schema: pa.Schema, name: str) -> ObjectDocs:
    noun = _noun(desc)
    last = desc.path_params[-1].name
    llm = (
        f"{_summary(desc)} The per-row form of the table {_qualified(desc)}: pass {_args(desc)} from columns "
        f"of another relation (a lateral join) and get every {noun} for each input row, paginated. Rows are "
        f"fetched concurrently and a repeated id is fetched once; an API error fails the query. Use the "
        f"table instead when {last} is a constant."
    )
    md = _md(
        _qualified(desc, name),
        f"{_summary(desc)} Calls Cloudflare GET `{desc.path}` once per input row and returns every {noun} "
        f"for it: the lateral counterpart of `{_qualified(desc)}`, whose filters need constants. "
        f"Arguments, in URL order: {_args(desc)} (each is also returned as a column).",
        [("Endpoint", _endpoint_detail(desc))],
        schema,
        column_table=False,
    )
    note = f"{comment(desc).rstrip('.')} — one API call per {last.removesuffix('_id')}, for lateral joins."
    return ObjectDocs(note, llm, md, (fanout_example(desc, name),))


def lookup_docs(desc: ResourceDescriptor, schema: pa.Schema) -> ObjectDocs:
    noun = _noun(desc)
    if _by_id(desc):
        what = (
            f"Fetches one {noun} from Cloudflare GET {desc.path}: one row when it exists, none when the id "
            "is unknown (404) or an argument is NULL."
        )
    else:
        what = (
            f"Reads the {noun} object at Cloudflare GET {desc.path}: one row, or none when it doesn't exist "
            "(404) or an argument is NULL."
        )
    llm = [
        f"{_summary(desc)} {what}",
        "Pass literals, or columns of another relation for a lateral join (a left lateral join keeps rows "
        "with no match); a batch of rows is fetched concurrently.",
    ]
    if extra := _extra_detail(desc):
        llm.append(extra)
    md = _md(
        _qualified(desc),
        f"{_summary(desc)} {what.replace(f'GET {desc.path}', f'GET `{desc.path}`')} "
        f"Arguments, in URL order: {_args(desc)}.",
        [("Endpoint", _endpoint_detail(desc))],
        schema,
        column_table=False,
    )
    return ObjectDocs(comment(desc), " ".join(llm), md, tuple(lookup_examples(desc)))


def singleton_docs(desc: ResourceDescriptor, schema: pa.Schema) -> ObjectDocs:
    llm = [
        f"{_summary(desc)} Reads Cloudflare GET {desc.path} (no parameters); scan it like a table, or "
        "call it without arguments."
    ]
    if extra := _extra_detail(desc):
        llm.append(extra)
    md = _md(
        _qualified(desc),
        f"{_summary(desc)} Reads Cloudflare GET `{desc.path}`, which takes no parameters.",
        [("Endpoint", _endpoint_detail(desc))],
        schema,
    )
    return ObjectDocs(comment(desc), " ".join(llm), md, tuple(singleton_examples(desc)))


def keywords(desc: ResourceDescriptor) -> list[str]:
    words: list[str] = []
    for text in (desc.api_tag, humanize(desc.area), humanize(desc.name), desc.schema):
        t = text.strip().lower()
        if t and t not in words:
            words.append(t)
    return words


#: Classifying tag (vgi-lint VGI123/VGI132: a small, reused vocabulary).
CLASSIFICATION = {"provider": "cloudflare"}


# ---------------------------------------------------------------------------
# Categories (navigation sections within a schema)
# ---------------------------------------------------------------------------

MAX_CATEGORIES = 12


def _area_key(desc: ResourceDescriptor, single_area: bool) -> str:
    if not single_area:
        return desc.area or desc.schema
    # One product area for the whole schema: split on the next path segment.
    segs = [s for s in desc.path.strip("/").split("/") if not s.startswith("{")]
    if desc.area in segs:
        rest = segs[segs.index(desc.area) + 1 :]
        if rest:
            return rest[0].replace("-", "_")
    return desc.area or desc.schema


def assign_categories(descs: Sequence[ResourceDescriptor]) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Map each descriptor (by name) to a category slug, and build the schema registry."""
    single = len({d.area for d in descs}) <= 1
    keys = {d.name: _area_key(d, single) for d in descs}
    counts = Counter(keys.values())
    keep = [k for k, _ in counts.most_common()]
    other: list[str] = []
    if len(keep) > MAX_CATEGORIES:
        keep, other = keep[: MAX_CATEGORIES - 1], keep[MAX_CATEGORIES - 1 :]
    by_cat: dict[str, list[ResourceDescriptor]] = defaultdict(list)
    mapping: dict[str, str] = {}
    for d in descs:
        cat = "more" if keys[d.name] in other else slug(keys[d.name])
        mapping[d.name] = cat
        by_cat[cat].append(d)
    registry = []
    for k in [*keep, *(["more"] if other else [])]:
        cat = "more" if k == "more" else slug(k)
        if cat == "more":
            title = "More APIs"
            description = "Smaller API areas: " + ", ".join(humanize(o) for o in other) + "."
        else:
            title = humanize(k)
            summaries = list(dict.fromkeys(m.description.rstrip(".") for m in by_cat[cat] if m.description))
            description = (
                f"{title}: " + "; ".join(summaries[:3]) + ("; …" if len(summaries) > 3 else "") + "."
            )
        registry.append({"name": cat, "title": title, "description": prose(description)})
    return mapping, registry


def dedupe_comments(comments: dict[tuple[str, str], tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Make shared descriptions unique by naming the endpoint they come from.

    ``comments`` maps an object key to (comment, path).
    """
    counts = Counter(c for c, _ in comments.values())
    return {
        k: (f"{c.rstrip('.')} (GET {p})." if counts[c] > 1 and p not in c else c)
        for k, (c, p) in comments.items()
    }
