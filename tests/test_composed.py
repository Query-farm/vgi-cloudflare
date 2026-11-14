"""Composed examples sit on the objects that drive them and in their schemas' lists."""

from __future__ import annotations

import json
import re

from vgi_cloudflare import composed
from vgi_cloudflare.worker import CLOUDFLARE_CATALOG

_REF = re.compile(r"cloudflare\.(\w+)\.(\w+)")


def _objects() -> dict[tuple[str, str], dict[str, str]]:
    out: dict[tuple[str, str], dict[str, str]] = {}
    for schema in CLOUDFLARE_CATALOG.schemas:
        for fn in schema.functions:
            out[(schema.path[0], fn.Meta.name)] = fn.Meta.tags
    return out


def test_composed_examples_lead_their_objects_examples() -> None:
    objects = _objects()
    for key, examples in composed.BY_OBJECT.items():
        listed = [e["sql"] for e in json.loads(objects[key]["vgi.example_queries"])]
        for example in examples:
            # Agents see only an object's first five examples.
            assert example.sql in listed[:5], key


def test_composed_examples_are_in_their_schema_lists() -> None:
    by_name = {s.path[0]: s for s in CLOUDFLARE_CATALOG.schemas}
    for schema, examples in composed.BY_SCHEMA.items():
        listed = [e["sql"] for e in json.loads(by_name[schema].tags["vgi.example_queries"])]
        assert [e.sql for e in examples] == listed[: len(examples)]


def test_composed_examples_reference_existing_objects_and_compose() -> None:
    objects = _objects()
    tables = {(s.path[0], t.name) for s in CLOUDFLARE_CATALOG.schemas for t in s.tables}
    every = {e for group in composed.BY_OBJECT.values() for e in group}
    for example in every:
        refs = set(_REF.findall(example.sql))
        assert refs <= set(objects) | tables, refs
        assert len(refs) >= 2, example.sql  # composed: two or more distinct objects
        assert ";" not in example.sql  # one statement
        assert "ORDER BY" in example.sql
