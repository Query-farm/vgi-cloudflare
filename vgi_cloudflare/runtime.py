"""Turn a :class:`ResourceDescriptor` into a VGI ``TableFunctionGenerator``.

One generic generator services every descriptor. The lifecycle:

  on_init  — read DuckDB's pushdown filters, resolve them into one or more
             *bindings* (a concrete path + query-string for the API). Required
             path params (URL ``{placeholders}``) must come from an equality/IN
             filter; otherwise we raise a clear error. Bindings are pushed onto
             the framework queue.
  process  — pop one binding, page through the Cloudflare API, and emit an
             Arrow batch per page. When the queue drains, finish.

Every pushed predicate is applied to the rows we emit (``auto_apply_filters``):
DuckDB drops the filters it pushes to a scan. The ones the API understands are
also sent on the query string, so less is fetched.

Lateral functions (singular-noun lookups like ``zone``, ``<table>_by_<parent>``
fan-outs like ``records_by_zone``) are VGI
``RowTransformFunction``s instead: their path params are per-row *input columns*,
so one registration serves ``f('id')``, ``FROM t, f(t.id)`` and ``LATERAL``.
Each input batch is fetched concurrently (deduplicated by key) and emitted as one
output batch whose ``parent_rows`` map every output row back to its input row.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, ClassVar, cast
from urllib.parse import quote

import pyarrow as pa
from vgi.arguments import Arg, SecretLookupEntry
from vgi.invocation import GlobalInitResponse
from vgi.metadata import FunctionExample
from vgi.protocol import BindResponse
from vgi.table_filter_pushdown import deserialize_filters
from vgi.table_function import (
    BindParams,
    InitParams,
    OutputCollector,
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
)
from vgi.table_in_out_function import RowTransformFunction

from .client import CloudflareAuth, CloudflareError, paginate
from .descriptor import Column, Example, ResourceDescriptor
from .secret import API_EMAIL_KEY, API_KEY_KEY, API_TOKEN_KEY, CLOUDFLARE_SECRET_TYPE

log = logging.getLogger(__name__)

#: Max concurrent Cloudflare requests per lateral input batch.
LATERAL_CONCURRENCY = 8


@dataclass(slots=True, frozen=True, kw_only=True)
class _NoArgs:
    """Resources take all their input via pushdown filters, not positional args."""


def _scalar_to_py(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _query_value(value: object) -> str:
    return str(_scalar_to_py(value))


def _navigate(item: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = item
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def _coerce(value: Any, type: pa.DataType) -> Any:
    """Coerce a raw JSON value to something pyarrow accepts for ``type``."""
    if value is None:
        return None
    if pa.types.is_string(type):
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"), sort_keys=True)
        if isinstance(value, bool):
            return "true" if value else "false"
        return value if isinstance(value, str) else str(value)
    if pa.types.is_boolean(type):
        return bool(value)
    if pa.types.is_floating(type):
        return float(value)
    if pa.types.is_integer(type):
        return int(value)
    if pa.types.is_timestamp(type):
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
    return value


def _extract(item: dict[str, Any], col: Column) -> Any:
    return _coerce(_navigate(item, col.json_path), col.type)


_PLACEHOLDER = re.compile(r"\{[^}]+\}")


def _format_path(desc: ResourceDescriptor, path_values: dict[str, str]) -> str:
    """Fill the URL template, escaping each value so an id can't alter the path.

    Placeholders are filled in URL order from ``path_params`` (also URL order), so a
    raw name that isn't an identifier (``{livestream-session-id}``) still works.
    """
    values = iter([quote(str(path_values[p.name]), safe="") for p in desc.path_params])
    return _PLACEHOLDER.sub(lambda _: next(values), desc.path)


def _fetch_all(
    auth: CloudflareAuth,
    desc: ResourceDescriptor,
    path_values: dict[str, str],
    query: dict[str, Any],
    *,
    not_found_ok: bool = False,
) -> list[dict[str, Any]]:
    """Fetch every result item for one binding (all pages)."""
    path = _format_path(desc, path_values)
    query = {**_defaults_for(desc, query), **query}
    try:
        return [raw for page in paginate(auth, path, query, desc) for raw in page]
    except CloudflareError as exc:
        if not_found_ok and exc.status == 404:
            return []
        raise


def _row_values(
    raw: dict[str, Any], path_values: dict[str, str], out_cols: list[str], col_index: dict[str, Column]
) -> list[Any]:
    return [path_values[c] if c in path_values else _extract(raw, col_index[c]) for c in out_cols]


def make_resource_function(
    desc: ResourceDescriptor, docs: FunctionDocs | None = None
) -> type[TableFunctionGenerator]:
    """Build a VGI table-function class for one resource descriptor."""

    output_schema = desc.output_schema()

    @bind_fixed_schema
    class _Resource(TableFunctionGenerator[_NoArgs, None]):
        DESCRIPTOR: ClassVar[ResourceDescriptor] = desc
        FIXED_SCHEMA: ClassVar[pa.Schema] = output_schema

        class Meta:
            name = desc.name
            description, examples, tags = _meta(desc, docs, f"Cloudflare {desc.name}")
            categories = list(desc.categories)
            projection_pushdown = True
            filter_pushdown = True
            # DuckDB drops the filters it pushes to us, so every predicate must be applied
            # here; the ones the API understands are also sent upstream to fetch less.
            auto_apply_filters = True
            required_secrets = [SecretLookupEntry(secret_type=CLOUDFLARE_SECRET_TYPE)]

        @classmethod
        def cardinality(cls, params: BindParams[_NoArgs]) -> TableCardinality:
            return TableCardinality(estimate=desc.cardinality_estimate, max=desc.cardinality_max)

        @classmethod
        def on_init(cls, params: InitParams[_NoArgs]) -> GlobalInitResponse:
            bindings = _resolve_bindings(
                desc,
                params.init_call.pushdown_filters,
                params.init_call.join_keys,
                params.init_call.output_schema,
            )
            params.storage.queue_push([json.dumps(b).encode("utf-8") for b in bindings])
            return GlobalInitResponse(max_workers=1)

        @classmethod
        def process(cls, params: ProcessParams[_NoArgs], state: None, out: OutputCollector) -> None:
            auth = _auth_from_secrets(params.secrets)
            out_cols = list(params.output_schema.names)
            col_index = desc.column_index()

            while True:
                item = params.storage.queue_pop()
                if item is None:
                    out.finish()
                    return

                binding = json.loads(item.decode("utf-8"))
                path_values: dict[str, str] = binding["path"]

                # The framework allows one emit per process() call, so a binding's
                # pages are collected into a single batch.
                items = _fetch_all(auth, desc, path_values, binding["query"])
                if not items:
                    continue  # Empty binding — fall through to the next one.
                rows = [_row_values(raw, path_values, out_cols, col_index) for raw in items]
                data = {c: [r[j] for r in rows] for j, c in enumerate(out_cols)}
                out.emit(pa.RecordBatch.from_pydict(data, schema=params.output_schema))
                return

    _Resource.__name__ = f"{_camel(desc.name)}Resource"
    _Resource.__qualname__ = _Resource.__name__
    _Resource.__doc__ = desc.description or f"Cloudflare {desc.name} table."
    return _Resource


def _build_item_args_class(desc: ResourceDescriptor) -> type:
    """Args for a lookup: path params positional (URL order), query params as named args.

    Named args default to the descriptor's ``default_query`` value (Radar's
    ``date_range => '7d'``) or to ``''``, meaning "not sent".
    """
    defaults = dict(desc.default_query)
    # Only lookups take query params as arguments; a list's are pushed-down filter columns.
    named = desc.query_params if desc.kind == "item" else ()
    if not desc.path_params and not named:
        return _NoArgs
    fields: list[tuple[str, Any] | tuple[str, Any, Any]] = []
    for i, p in enumerate(desc.path_params):
        annotation = Annotated[
            str,
            Arg(
                i,
                doc=p.doc or f"{p.name} (URL path parameter).",
                arrow_type=pa.string(),
                choices=list(p.choices) or None,
                pattern=p.pattern,
            ),
        ]
        fields.append((p.name, annotation))
    for q in named:
        doc = q.doc or f"Sent as the `{q.key}` query parameter."
        if pa.types.is_integer(q.type) or pa.types.is_floating(q.type):
            # Numeric: optional (None = not sent), with the spec's bounds.
            py = int if pa.types.is_integer(q.type) else float
            annotation = Annotated[py | None, Arg(q.name, doc=doc, default=None, ge=q.ge, le=q.le)]
            fields.append((q.name, annotation, dataclasses.field(default=None)))
            continue
        default = defaults.get(q.key, "")
        if default:
            doc = f"{doc} Default: `{default}`."
        choices = list(q.choices) + ([""] if q.choices and not default else []) or None
        pattern = f"(?:{q.pattern})?" if q.pattern and not default else q.pattern  # '' = unset
        annotation = Annotated[str, Arg(q.name, doc=doc, default=default, choices=choices, pattern=pattern)]
        fields.append((q.name, annotation, dataclasses.field(default=default)))
    return dataclasses.make_dataclass(f"{_camel(desc.name)}Args", fields, frozen=True, slots=True)


#: A default that an explicit choice of these other parameters replaces: Radar rejects
#: ``dateRange`` together with ``dateStart``/``dateEnd``.
_REPLACED_BY = {"dateRange": ("dateStart", "dateEnd")}


def _defaults_for(desc: ResourceDescriptor, query: dict[str, Any]) -> dict[str, str]:
    """The descriptor's default query params that the caller hasn't replaced."""
    return {k: v for k, v in desc.default_query if not any(query.get(o) for o in _REPLACED_BY.get(k, ()))}


def _named_query(desc: ResourceDescriptor, args: Any) -> dict[str, str]:
    """The query string from a lookup's named args (empty values are not sent).

    An argument left at its default is dropped when the caller set a parameter that
    replaces it (``date_start``/``date_end`` replace the default ``date_range``).
    """
    defaults = dict(desc.default_query)
    out = {}
    for q in desc.query_params:
        value = getattr(args, q.name, "")
        if value is not None and value != "":
            out[q.key] = str(value)
    for key, others in _REPLACED_BY.items():
        if key in out and out[key] == defaults.get(key) and any(out.get(o) for o in others):
            del out[key]
    return out


def _lateral_process(
    desc: ResourceDescriptor,
    params: ProcessParams[Any],
    batch: pa.RecordBatch,
    out: OutputCollector,
    *,
    not_found_ok: bool,
) -> None:
    """Fetch every input row's binding and emit one batch with ``parent_rows``.

    Rows with a NULL path param produce no output. Identical keys within the batch
    are fetched once; distinct keys are fetched concurrently.
    """
    auth = _auth_from_secrets(params.secrets)
    names = [p.name for p in desc.path_params]
    columns = [batch.column(n).to_pylist() for n in names]
    keys: list[tuple[str, ...] | None] = [
        None if any(v is None for v in row) else tuple(str(v) for v in row)
        for row in zip(*columns, strict=True)
    ]
    distinct = list(dict.fromkeys(k for k in keys if k is not None))

    query = _named_query(desc, params.args) if desc.kind == "item" else {}

    def fetch(key: tuple[str, ...]) -> list[dict[str, Any]]:
        return _fetch_all(auth, desc, dict(zip(names, key, strict=True)), query, not_found_ok=not_found_ok)

    results: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    if len(distinct) == 1:
        results[distinct[0]] = fetch(distinct[0])
    elif distinct:
        with ThreadPoolExecutor(max_workers=min(LATERAL_CONCURRENCY, len(distinct))) as pool:
            results = dict(zip(distinct, pool.map(fetch, distinct), strict=True))

    out_cols = list(params.output_schema.names)
    col_index = desc.column_index()
    rows: list[list[Any]] = []
    parent_rows: list[int] = []
    for i, key in enumerate(keys):
        if key is None:
            continue
        path_values = dict(zip(names, key, strict=True))
        for raw in results[key]:
            rows.append(_row_values(raw, path_values, out_cols, col_index))
            parent_rows.append(i)
    data = {c: [r[j] for r in rows] for j, c in enumerate(out_cols)}
    emit = cast(Callable[..., None], out.emit)
    emit(pa.RecordBatch.from_pydict(data, schema=params.output_schema), parent_rows=parent_rows)


def _make_lateral_function(
    desc: ResourceDescriptor, fn_name: str, *, item: bool, docs: FunctionDocs | None = None
) -> type[RowTransformFunction]:
    output_schema = desc.output_schema()
    args_class = _build_item_args_class(desc)
    kind = "get one by id" if item else "list, one call per input row"

    class _Lateral(RowTransformFunction[args_class]):
        DESCRIPTOR: ClassVar[ResourceDescriptor] = desc
        FIXED_SCHEMA: ClassVar[pa.Schema] = output_schema

        class Meta:
            name = fn_name
            description, examples, tags = _meta(desc, docs, f"Cloudflare {desc.name} ({kind})")
            categories = [*desc.categories, "lateral"]
            required_secrets = [SecretLookupEntry(secret_type=CLOUDFLARE_SECRET_TYPE)]

        @classmethod
        def on_bind(cls, params: BindParams[Any]) -> BindResponse:
            return BindResponse(output_schema=output_schema)

        @classmethod
        def process(
            cls, params: ProcessParams[Any], state: None, batch: pa.RecordBatch, out: OutputCollector
        ) -> None:
            _lateral_process(desc, params, batch, out, not_found_ok=item)

    suffix = "Item" if item else "Lateral"
    _Lateral.__name__ = f"{_camel(fn_name)}{suffix}"
    _Lateral.__qualname__ = _Lateral.__name__
    _Lateral.__doc__ = desc.description or f"Cloudflare {desc.name} ({kind})."
    return _Lateral


def make_item_function(
    desc: ResourceDescriptor, docs: FunctionDocs | None = None
) -> type[TableFunctionGenerator] | type[RowTransformFunction]:
    """Build a get-one-by-id function: path params are its (per-row) arguments.

    Called as ``cf.<schema>.<name>(arg1, arg2, ...)`` with the path params in URL
    order — with literals, or correlated (``FROM t, cf.zones.zone(t.id)`` / ``LATERAL``).
    Each input row yields **0 or 1 row** (0 when the API yields ``result: null``,
    404s, or an argument is NULL). Items with no path params (e.g. ``user()``)
    take no input, so they remain plain table functions.
    """
    if desc.path_params:
        return _make_lateral_function(desc, desc.name, item=True, docs=docs)
    return _make_singleton_function(desc, docs)


def make_lateral_list_function(
    desc: ResourceDescriptor, name: str | None = None, docs: FunctionDocs | None = None
) -> type[RowTransformFunction]:
    """Build ``<table>_by_<parent>(path params...)``: a list resource driven per input row.

    The pushdown table (``cf.dns.records WHERE zone_id = ...``) needs its ids as
    constants; this form takes them from another table, fanning each input row
    out to all of its (paginated) results::

        SELECT z.name, r.name, r.type
        FROM cf.zones.zones z, cf.dns.records_by_zone(z.id) r;
    """
    if not desc.path_params:
        raise ValueError(f"{desc.name}: a lateral list function needs at least one path param")
    return _make_lateral_function(desc, name or lateral_list_name(desc), item=False, docs=docs)


_PARAM_SUFFIXES = ("_identifier", "_id", "_tag", "_name", "_key")
_GENERIC_PARAMS = {"id", "identifier", "name", "tag", "key", "slug", "value", "param", "url"}


def _parent_noun(desc: ResourceDescriptor) -> str:
    """The noun for a resource's most specific parent: its last path param, de-suffixed.

    ``account_id`` -> ``account``, ``script_name`` -> ``script``. A generic param
    (``{id}``, ``{name}``) is named by the path segment before it, singularized.
    """
    param = desc.path_params[-1].name
    base = param
    for suffix in _PARAM_SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    if base and base not in _GENERIC_PARAMS:
        return base
    segments = desc.path.split("/")
    idx = segments.index("{" + param + "}")
    prev = segments[idx - 1].replace("-", "_") if idx > 0 else ""
    if prev.endswith("ies"):
        return prev[:-3] + "y"
    if prev.endswith("s") and not prev.endswith("ss"):
        return prev[:-1]
    return prev or param


def lateral_list_name(desc: ResourceDescriptor) -> str:
    """``records`` with parent ``{zone_id}`` -> ``records_by_zone``."""
    return f"{desc.name}_by_{_parent_noun(desc)}"


def _make_singleton_function(
    desc: ResourceDescriptor, docs: FunctionDocs | None = None
) -> type[TableFunctionGenerator]:
    """A zero-argument item (``get_user()``): one fetch, 0 or 1 row."""
    output_schema = desc.output_schema()
    col_index = desc.column_index()

    args_class = _build_item_args_class(desc)

    @bind_fixed_schema
    class _Item(TableFunctionGenerator[args_class, None]):
        DESCRIPTOR: ClassVar[ResourceDescriptor] = desc
        FIXED_SCHEMA: ClassVar[pa.Schema] = output_schema

        class Meta:
            name = desc.name
            description, examples, tags = _meta(desc, docs, f"Cloudflare {desc.name} (get one)")
            categories = list(desc.categories)
            projection_pushdown = True
            required_secrets = [SecretLookupEntry(secret_type=CLOUDFLARE_SECRET_TYPE)]

        @classmethod
        def cardinality(cls, params: BindParams[Any]) -> TableCardinality:
            return TableCardinality(estimate=1, max=1)

        @classmethod
        def on_init(cls, params: InitParams[Any]) -> GlobalInitResponse:
            # One fetch total: without this every scan thread runs process() and
            # emits its own copy of the row.
            return GlobalInitResponse(max_workers=1)

        @classmethod
        def process(cls, params: ProcessParams[Any], state: None, out: OutputCollector) -> None:
            auth = _auth_from_secrets(params.secrets)
            out_cols = list(params.output_schema.names)
            query = _named_query(desc, params.args)
            rows = [_row_values(raw, {}, out_cols, col_index) for raw in _fetch_all(auth, desc, {}, query)]
            data = {c: [r[j] for r in rows] for j, c in enumerate(out_cols)}
            out.emit(pa.RecordBatch.from_pydict(data, schema=params.output_schema))
            out.finish()

    _Item.__name__ = f"{_camel(desc.name)}Item"
    _Item.__qualname__ = _Item.__name__
    _Item.__doc__ = desc.description or f"Cloudflare {desc.name} get function."
    return _Item


def example_tags(examples: list[FunctionExample]) -> dict[str, str]:
    """Carry example descriptions in ``vgi.example_queries``.

    DuckDB's native ``duckdb_functions().examples`` holds bare SQL, so descriptions
    only reach clients (and vgi-lint) through this tag; it repeats the same SQL,
    which clients merge with the native list.
    """
    if not examples:
        return {}
    return {
        "vgi.example_queries": json.dumps([{"description": e.description, "sql": e.sql} for e in examples])
    }


def _examples(desc: ResourceDescriptor) -> list[FunctionExample]:
    return [FunctionExample(sql=e.sql, description=e.description) for e in desc.examples]


@dataclass(frozen=True)
class FunctionDocs:
    """Catalog metadata for a generated function (see ``docs.py``)."""

    description: str
    examples: tuple[Example, ...] = ()
    tags: dict[str, str] = dataclasses.field(default_factory=dict)


def _meta(desc: ResourceDescriptor, docs: FunctionDocs | None, fallback: str) -> tuple[str, list, dict]:
    """(description, examples, tags) for a function's Meta."""
    if docs is None:
        examples = _examples(desc)
        return desc.description or fallback, examples, example_tags(examples)
    examples = [FunctionExample(sql=e.sql, description=e.description) for e in docs.examples]
    return docs.description, examples, {**example_tags(examples), **docs.tags}


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _auth_from_secrets(secrets: dict[str, dict[str, pa.Scalar[Any]]]) -> CloudflareAuth:
    # Resolved secrets are keyed by secret *name* when DuckDB supplies one (e.g. the
    # two-phase lookup the lateral path uses), else by type — so fall back to a
    # type-aware lookup rather than assuming the key.
    cf = secrets.get(CLOUDFLARE_SECRET_TYPE)
    if cf is None and hasattr(secrets, "of_type"):
        cf = next(iter(secrets.of_type(CLOUDFLARE_SECRET_TYPE)), None)
    cf = cf or {}

    def _get(key: str) -> str | None:
        scalar = cf.get(key)
        if scalar is None:
            return None
        value = scalar.as_py()
        return value or None

    auth = CloudflareAuth(
        api_token=_get(API_TOKEN_KEY),
        api_key=_get(API_KEY_KEY),
        email=_get(API_EMAIL_KEY),
    )
    if not (auth.api_token or auth.api_key):
        auth = _token_file_auth() or auth
    return auth


#: Opt-in fallback for local tooling (e.g. ``vgi-lint simulate``) that cannot create a
#: ``cloudflare`` secret: a file holding an API token, used only when a query brings
#: no secret. Never set it on a shared deployment — every caller would get this token.
TOKEN_FILE_ENV = "VGI_CLOUDFLARE_TOKEN_FILE"


def _token_file_auth() -> CloudflareAuth | None:
    path = os.environ.get(TOKEN_FILE_ENV)
    if not path:
        return None
    try:
        token = Path(path).expanduser().read_text().strip()
    except OSError as exc:
        raise CloudflareError(f"{TOKEN_FILE_ENV}={path!r} could not be read: {exc.strerror}") from exc
    return CloudflareAuth(api_token=token) if token else None


def _resolve_bindings(
    desc: ResourceDescriptor,
    pushdown_filters: pa.RecordBatch | None,
    join_keys: list[pa.RecordBatch] | None,
    output_schema: pa.Schema | None = None,
) -> list[dict[str, Any]]:
    """Resolve pushdown filters into concrete (path, query) bindings.

    ``output_schema`` lets the parser type bare column predicates (``WHERE flag``).
    """
    filters = (
        deserialize_filters(pushdown_filters, join_keys=join_keys, output_schema=output_schema)
        if pushdown_filters is not None
        else None
    )

    def equality_values(col: str) -> list[Any] | None:
        if filters is None:
            return None
        values = filters.get_column_values(col)
        if values is None:
            return None
        return [v for v in values.to_pylist() if v is not None]

    # Required path params — each needs at least one equality/IN value.
    path_value_lists: dict[str, list[str]] = {}
    for p in desc.path_params:
        values = equality_values(p.name)
        if not values:
            raise RuntimeError(
                f"Table '{desc.name}' requires an equality filter on '{p.name}' "
                f"(e.g. WHERE {p.name} = '...'); it maps to a URL path segment. To take "
                f"{desc.path_params[-1].name} values from another query, use the function "
                f"{desc.schema}.{lateral_list_name(desc)}({', '.join(x.name for x in desc.path_params)})."
            )
        path_value_lists[p.name] = [str(v) for v in values]

    # Cartesian product of path-param values -> one path binding each.
    path_combos: list[dict[str, str]] = [{}]
    for name, values in path_value_lists.items():
        path_combos = [{**combo, name: v} for combo in path_combos for v in values]

    # Optional query-param filters: push single-valued equalities onto the query string.
    query: dict[str, Any] = {}
    for q in desc.query_params:
        values = equality_values(q.name)
        if values and len(values) == 1:
            query[q.key] = _query_value(values[0])

    return [{"path": combo, "query": dict(query)} for combo in path_combos]
