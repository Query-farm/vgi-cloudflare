"""Regression tests for the OpenAPI-generated resource set."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pyarrow as pa

import vgi_cloudflare.client as client
from tests.harness import eq_filters, invoke_resource
from vgi_cloudflare.catalog import build_catalog, merge_resources
from vgi_cloudflare.generated import GENERATED_RESOURCES
from vgi_cloudflare.resources import CORE_RESOURCES
from vgi_cloudflare.runtime import make_resource_function


def test_manifest_loads_many_resources() -> None:
    assert len(GENERATED_RESOURCES) > 200


def test_every_generated_descriptor_builds_a_function() -> None:
    for desc in GENERATED_RESOURCES:
        fn = make_resource_function(desc)
        assert isinstance(fn.FIXED_SCHEMA, pa.Schema)
        assert len(fn.FIXED_SCHEMA.names) == len(set(fn.FIXED_SCHEMA.names)), desc.name


def test_merged_catalog_has_unique_names_and_handwritten_wins() -> None:
    merged = merge_resources(CORE_RESOURCES, GENERATED_RESOURCES)
    # Names are unique within each (schema, name) namespace.
    keys = [(d.schema, d.name) for d in merged]
    assert len(keys) == len(set(keys))
    # Hand-written dns.records (12 result columns) wins over the generated one.
    dns = next(d for d in merged if d.schema == "dns" and d.name == "records")
    assert dns.columns[0].name == "id"
    assert "proxiable" in {c.name for c in dns.columns}
    # Catalog assembles without error: every descriptor is exactly one function. Tables
    # exist only for unscoped lists and parameterless lookups; a list scoped by URL ids
    # is a function of those ids instead.
    cat = build_catalog(merged)
    total_funcs = sum(len(s.functions) for s in cat.schemas)
    total_tables = sum(len(s.tables) for s in cat.schemas)
    assert total_funcs == len(merged)
    unscoped = sum(1 for d in merged if not d.path_params)
    assert total_tables == unscoped
    for schema in cat.schemas:
        names = [f.Meta.name for f in schema.functions]
        assert len(names) == len(set(names)), schema.path


def test_generated_resource_runs_end_to_end() -> None:
    members = next(d for d in GENERATED_RESOURCES if d.path == "/accounts/{account_id}/members")
    fn = make_resource_function(members)
    captured: dict[str, Any] = {}

    def _fake_get(path, params=None, headers=None):  # noqa: ANN001
        captured["path"] = path
        return httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "result": [{"id": "m1", "status": "accepted"}],
                "result_info": {"page": 1, "total_pages": 1, "count": 1},
            },
        )

    with patch.object(client._http, "get", side_effect=_fake_get):
        table = invoke_resource(fn, pushdown_filters=eq_filters(account_id="acct-xyz"))

    assert captured["path"] == "/accounts/acct-xyz/members"
    row = table.to_pylist()[0]
    assert row["account_id"] == "acct-xyz"
    assert row["id"] == "m1"
