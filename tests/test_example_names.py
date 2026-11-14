"""Generated example SQL quotes object names that are DuckDB reserved words."""

from __future__ import annotations

import json

from vgi_cloudflare.worker import CLOUDFLARE_CATALOG


def test_reserved_object_names_are_quoted_in_examples() -> None:
    # access.group, zones.default, stream.all, accounts.references: unquoted, DuckDB
    # fails to parse the call ("syntax error at or near '('").
    seen = 0
    for schema in CLOUDFLARE_CATALOG.schemas:
        for fn in schema.functions:
            name = fn.Meta.name
            if name not in {"group", "default", "all", "references"}:
                continue
            for example in json.loads(fn.Meta.tags.get("vgi.example_queries", "[]")):
                seen += 1
                assert f'cloudflare.{schema.path[0]}."{name}"' in example["sql"], example["sql"]
                assert f"cloudflare.{schema.path[0]}.{name}(" not in example["sql"]
    assert seen >= 8
