"""Assemble resource descriptors into a multi-schema VGI Catalog."""

from __future__ import annotations

import dataclasses
import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vgi.catalog import Catalog, ForeignKeyDef, Schema, Table

from . import docs
from .descriptor import ResourceDescriptor
from .runtime import (
    FunctionDocs,
    make_item_function,
    make_lateral_list_function,
    make_resource_function,
)
from .schema_docs import CATALOG_COMMENT, CATALOG_DOC_LLM, CATALOG_DOC_MD, CATALOG_KEYWORDS, SCHEMA_DOCS

CATALOG_NAME = "cloudflare"
SOURCE_URL = "https://github.com/Query-farm/vgi-cloudflare"
DEFAULT_SCHEMA = "zones"  # schema used for unqualified table names


def merge_resources(
    primary: Sequence[ResourceDescriptor],
    secondary: Sequence[ResourceDescriptor],
) -> list[ResourceDescriptor]:
    """Merge descriptor lists keyed by (schema, name), keeping ``primary`` on collisions.

    Hand-written resources (primary) win over generated ones (secondary) so
    their curated schemas take precedence.
    """
    by_key: dict[tuple[str, str], ResourceDescriptor] = {}
    for desc in secondary:
        by_key.setdefault((desc.schema, desc.name), desc)
    for desc in primary:
        twin = by_key.get((desc.schema, desc.name))
        if twin is not None:
            # Curated columns win; spec-derived documentation fills what isn't curated.
            desc = dataclasses.replace(
                desc,
                doc=desc.doc or twin.doc,
                api_tag=desc.api_tag or twin.api_tag,
                area=desc.area or twin.area,
            )
        by_key[(desc.schema, desc.name)] = desc
    return [by_key[k] for k in sorted(by_key)]


def build_catalog(
    descriptors: Sequence[ResourceDescriptor],
    *,
    extra: dict[str, dict[str, Any]] | None = None,
) -> Catalog:
    """Build the ``cloudflare`` catalog, grouping resources into per-product schemas.

    Each descriptor's ``schema`` selects its DuckDB schema (``cloudflare.dns``, ...).
    A list scoped by URL ids becomes one function of those ids (``dns.records(zone_id)``),
    usable with literals or laterally; an unscoped list (``zones.zones``) is a Table.
    Item descriptors become a lookup function, and a Table too when they take no
    parameters. Every object carries
    docs generated from its descriptor (``docs.py``). ``extra`` injects
    non-descriptor objects per schema:
    ``{"analytics": {"functions": [...], "tables": [...], "categories": [...]}}``.
    """
    by_schema: dict[str, list[ResourceDescriptor]] = defaultdict(list)
    for d in descriptors:
        by_schema[d.schema].append(d)

    # Docs per object, then make descriptions unique catalog-wide.
    plans: list[tuple[ResourceDescriptor, str, str, docs.ObjectDocs]] = []
    for d in descriptors:
        out = d.output_schema()
        if d.kind == "list" and d.path_params:
            # A scoped list is a function of its path ids: dns.records(zone_id).
            plans.append((d, "fanout", d.name, docs.fanout_docs(d, out, d.name)))
        elif d.kind == "list":
            plans.append((d, "table", d.name, docs.table_docs(d, out, None)))
        elif d.path_params:
            plans.append((d, "lookup", d.name, docs.lookup_docs(d, out)))
        else:
            plans.append((d, "singleton", d.name, docs.singleton_docs(d, out)))
    comments = docs.dedupe_comments({(d.schema, n): (o.comment, d.path) for d, _, n, o in plans})

    categories: dict[str, dict[str, str]] = {}
    registries: dict[str, list[dict[str, str]]] = {}
    for schema_name, group in by_schema.items():
        categories[schema_name], registries[schema_name] = docs.assign_categories(group)

    funcs: dict[str, list] = defaultdict(list)
    tables: dict[str, list] = defaultdict(list)
    for d, role, name, o in plans:
        out = d.output_schema()
        comment = comments[(d.schema, name)]
        tags = {
            "vgi.doc_llm": o.doc_llm,
            "vgi.doc_md": o.doc_md,
            "vgi.category": categories[d.schema][d.name],
        }
        if role in ("fanout", "lookup"):
            # No backing table: declare the result shape for agents.
            tags["vgi.result_columns_schema"] = docs.result_columns_schema(out)
        fdocs = FunctionDocs(comment, o.examples, tags)
        if role == "table":
            fn = make_resource_function(d, fdocs)
        elif role == "fanout":
            fn = make_lateral_list_function(d, name, fdocs)
        else:
            fn = make_item_function(d, fdocs)
        funcs[d.schema].append(fn)
        if role in ("table", "singleton"):
            tables[d.schema].append(
                Table(
                    name=d.name,
                    function=fn,
                    comment=comment,
                    required_filters=tuple((p.name,) for p in d.path_params),
                    primary_key=_primary_key(d),
                    not_null=tuple(p.name for p in d.path_params),
                    foreign_key=_foreign_keys(d),
                    tags={
                        **tags,
                        **docs.CLASSIFICATION,
                        "vgi.keywords": json.dumps(docs.keywords(d)),
                        "vgi.example_queries": docs.example_tag(o.examples),
                    },
                )
            )

    for schema_name, items in (extra or {}).items():
        funcs[schema_name].extend(items.get("functions", []))
        tables[schema_name].extend(items.get("tables", []))
        registries[schema_name] = [*registries.get(schema_name, []), *items.get("categories", [])]

    schemas = [
        _schema(
            name,
            funcs[name],
            tables.get(name, []),
            registries.get(name, []),
            by_schema.get(name, []),
            (extra or {}).get(name, {}).get("examples"),
        )
        for name in sorted(funcs)
    ]
    return Catalog(
        name=CATALOG_NAME,
        default_schema=DEFAULT_SCHEMA,
        schemas=schemas,
        comment=CATALOG_COMMENT,
        source_url=SOURCE_URL,
        tags=catalog_tags(),
    )


def _primary_key(d: ResourceDescriptor) -> tuple[tuple[str, ...], ...]:
    """``id`` identifies a row within its parent, so the key includes the path ids."""
    if "id" not in {c.name for c in d.columns}:
        return ()
    return ((*(p.name for p in d.path_params), "id"),)


# Scope ids that reference a canonical parent table: column -> (schema, table).
_SCOPE_PARENTS = {"zone_id": ("zones", "zones"), "account_id": ("accounts", "accounts")}


def _foreign_keys(d: ResourceDescriptor) -> tuple[ForeignKeyDef, ...]:
    fks = []
    for p in d.path_params:
        parent = _SCOPE_PARENTS.get(p.name)
        if parent and (d.schema, d.name) != parent:
            fks.append(
                ForeignKeyDef(
                    columns=(p.name,),
                    referenced_table=parent[1],
                    referenced_columns=("id",),
                    referenced_schema_path=[parent[0]],
                )
            )
    return tuple(fks)


def _schema(
    name: str,
    functions: list,
    tables: list,
    registry: list[dict[str, str]],
    descs: Sequence[ResourceDescriptor],
    extra_examples: list | None = None,
) -> Schema:
    sd = SCHEMA_DOCS.get(name)
    if sd is None:
        return Schema(path=[name], functions=functions, tables=tables)
    sections = "\n".join(f"- **{c['title']}** — {c['description']}" for c in registry)
    md = f"# {sd.title}\n\n{sd.comment}\n\n{sd.llm}\n\n" + (f"{sd.md_extra}\n\n" if sd.md_extra else "")
    md += f"## Sections\n\n{sections}\n" if sections else ""
    examples = extra_examples or [e for d in descs if d.kind == "list" for e in docs.table_examples(d)][:3]
    if not examples:  # e.g. radar: parameterless reports only
        examples = [e for d in descs if not d.path_params for e in docs.singleton_examples(d)][:3]
    if not examples:  # lookups only
        examples = [docs.lookup_examples(d)[0] for d in descs if d.path_params][:3]
    tags = {
        "vgi.title": sd.title,
        "vgi.doc_llm": sd.llm,
        "vgi.doc_md": md,
        "vgi.keywords": json.dumps(list(sd.keywords)),
        "vgi.categories": json.dumps(registry),
        **docs.CLASSIFICATION,
    }
    if examples:
        tags["vgi.example_queries"] = docs.example_tag(examples)
    return Schema(path=[name], comment=sd.comment, functions=functions, tables=tables, tags=tags)


#: Natural-language tasks `vgi-lint simulate` gives an LLM analyst (public: name + prompt
#: only). Their private graders live in vgi-agent-tests.yaml; see tools/agent_tasks.py.
AGENT_TEST_TASKS = json.loads((Path(__file__).parent / "agent_tasks.json").read_text())

#: Guaranteed-runnable examples. Credential-free (bind only), so they run in CI.
EXECUTABLE_EXAMPLES = [
    {
        "name": "dns_records_require_zone",
        "description": (
            "The DNS records function takes the zone id as its argument and returns it as the "
            "zone_id column (no Cloudflare credentials needed to inspect the schema)."
        ),
        "sql": (
            "SELECT column_name FROM (DESCRIBE SELECT * FROM cloudflare.dns.records('some-zone')) "
            "WHERE column_name = 'zone_id'"
        ),
        "expected_result": [["zone_id"]],
    }
]


def catalog_tags() -> dict[str, str]:
    return {
        "vgi.title": "Cloudflare API",
        "vgi.doc_llm": CATALOG_DOC_LLM,
        "vgi.doc_md": CATALOG_DOC_MD,
        "vgi.keywords": json.dumps(list(CATALOG_KEYWORDS)),
        "vgi.source_url": SOURCE_URL,
        "vgi.author": "Query Farm LLC <hello@query.farm>",
        "vgi.copyright": (
            "Worker (c) 2026 Query Farm LLC - https://query.farm. Data returned is the caller's own "
            "Cloudflare account data, read through Cloudflare's API under its terms of use."
        ),
        "vgi.license": "MIT",
        "vgi.support_contact": f"{SOURCE_URL}/issues",
        "vgi.support_policy_url": f"{SOURCE_URL}/blob/main/README.md",
        "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
        "vgi.executable_examples": json.dumps(EXECUTABLE_EXAMPLES),
    }
