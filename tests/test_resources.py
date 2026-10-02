"""End-to-end resource tests via the in-process harness with mocked Cloudflare HTTP."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pyarrow as pa
import pytest

import vgi_cloudflare.client as client
from tests.harness import eq_filters, invoke_resource, token_secrets
from vgi_cloudflare.resources import ACCOUNTS, DNS_RECORDS, ZONES
from vgi_cloudflare.runtime import make_resource_function


def _envelope(result: list[dict[str, Any]], *, page: int = 1, total_pages: int = 1) -> dict[str, Any]:
    return {
        "success": True,
        "errors": [],
        "result": result,
        "result_info": {"page": page, "total_pages": total_pages, "count": len(result)},
    }


def _mock_pages(*pages: dict[str, Any]):
    """Patch the client's HTTP GET to return the given envelopes in sequence."""
    responses = [httpx.Response(200, json=p) for p in pages]

    def _fake_get(path, params=None, headers=None):  # noqa: ANN001
        return responses.pop(0)

    return patch.object(client._http, "get", side_effect=_fake_get)


ZONE_ROW = {
    "id": "zone1",
    "name": "example.com",
    "status": "active",
    "paused": False,
    "type": "full",
    "development_mode": 0,
    "account": {"id": "acct1", "name": "My Account"},
    "name_servers": ["ns1.cloudflare.com", "ns2.cloudflare.com"],
    "created_on": "2024-01-01T00:00:00Z",
    "modified_on": "2024-02-01T12:30:00Z",
    "activated_on": "2024-01-02T00:00:00Z",
}

DNS_ROW = {
    "id": "rec1",
    "name": "www.example.com",
    "type": "A",
    "content": "192.0.2.1",
    "proxiable": True,
    "proxied": True,
    "ttl": 1,
    "priority": None,
    "locked": False,
    "comment": "primary",
    "created_on": "2024-01-01T00:00:00Z",
    "modified_on": "2024-01-01T00:00:00Z",
}


class TestZones:
    def test_basic_columns(self) -> None:
        fn = make_resource_function(ZONES)
        with _mock_pages(_envelope([ZONE_ROW])):
            table = invoke_resource(fn)
        assert table.num_rows == 1
        row = table.to_pylist()[0]
        assert row["id"] == "zone1"
        assert row["name"] == "example.com"
        assert row["status"] == "active"
        assert row["paused"] is False
        assert row["development_mode"] == 0

    def test_nested_account_source(self) -> None:
        fn = make_resource_function(ZONES)
        with _mock_pages(_envelope([ZONE_ROW])):
            table = invoke_resource(fn)
        row = table.to_pylist()[0]
        assert row["account_id"] == "acct1"
        assert row["account_name"] == "My Account"

    def test_list_column_json_encoded(self) -> None:
        fn = make_resource_function(ZONES)
        with _mock_pages(_envelope([ZONE_ROW])):
            table = invoke_resource(fn)
        row = table.to_pylist()[0]
        assert row["name_servers"] == '["ns1.cloudflare.com","ns2.cloudflare.com"]'

    def test_timestamp_coercion(self) -> None:
        fn = make_resource_function(ZONES)
        with _mock_pages(_envelope([ZONE_ROW])):
            table = invoke_resource(fn)
        created = table.column("created_on")[0].as_py()
        assert created is not None
        assert created.year == 2024 and created.month == 1

    def test_pagination_two_pages(self) -> None:
        fn = make_resource_function(ZONES)
        rows = [dict(ZONE_ROW, id=f"z{i}") for i in range(150)]
        page1 = _envelope(rows[:100], page=1, total_pages=2)
        page2 = _envelope(rows[100:], page=2, total_pages=2)
        with _mock_pages(page1, page2):
            table = invoke_resource(fn)
        assert table.num_rows == 150
        assert table.column("id")[0].as_py() == "z0"
        assert table.column("id")[149].as_py() == "z149"


class TestProjectionPushdown:
    def test_subset_of_columns(self) -> None:
        fn = make_resource_function(ZONES)
        projected = pa.schema([fn.FIXED_SCHEMA.field("id"), fn.FIXED_SCHEMA.field("name")])
        with _mock_pages(_envelope([ZONE_ROW])):
            table = invoke_resource(fn, output_schema=projected)
        assert table.schema.names == ["id", "name"]
        assert table.to_pylist()[0] == {"id": "zone1", "name": "example.com"}


class TestPathParams:
    def test_zone_id_from_pushdown_becomes_column(self) -> None:
        fn = make_resource_function(DNS_RECORDS)
        with _mock_pages(_envelope([DNS_ROW])):
            table = invoke_resource(fn, pushdown_filters=eq_filters(zone_id="zoneABC"))
        row = table.to_pylist()[0]
        assert row["zone_id"] == "zoneABC"
        assert row["name"] == "www.example.com"
        assert row["type"] == "A"

    def test_zone_id_used_in_url(self) -> None:
        fn = make_resource_function(DNS_RECORDS)
        captured: dict[str, Any] = {}

        def _fake_get(path, params=None, headers=None):  # noqa: ANN001
            captured["path"] = path
            captured["params"] = params
            return httpx.Response(200, json=_envelope([DNS_ROW]))

        with patch.object(client._http, "get", side_effect=_fake_get):
            invoke_resource(fn, pushdown_filters=eq_filters(zone_id="zoneABC"))
        assert captured["path"] == "/zones/zoneABC/dns_records"

    def test_query_param_pushed_to_querystring(self) -> None:
        fn = make_resource_function(DNS_RECORDS)
        captured: dict[str, Any] = {}

        def _fake_get(path, params=None, headers=None):  # noqa: ANN001
            captured["params"] = params
            return httpx.Response(200, json=_envelope([DNS_ROW]))

        with patch.object(client._http, "get", side_effect=_fake_get):
            invoke_resource(fn, pushdown_filters=eq_filters(zone_id="z", type="A"))
        assert captured["params"].get("type") == "A"

    def test_missing_required_path_param_raises(self) -> None:
        fn = make_resource_function(DNS_RECORDS)
        with pytest.raises(RuntimeError, match="requires an equality filter on 'zone_id'"):
            invoke_resource(fn)


class TestAuth:
    def test_bearer_header_sent(self) -> None:
        fn = make_resource_function(ACCOUNTS)
        captured: dict[str, Any] = {}

        def _fake_get(path, params=None, headers=None):  # noqa: ANN001
            captured["headers"] = headers
            return httpx.Response(200, json=_envelope([{"id": "a1", "name": "Acct"}]))

        with patch.object(client._http, "get", side_effect=_fake_get):
            invoke_resource(fn, secrets=token_secrets("secret-token-123"))
        assert captured["headers"]["Authorization"] == "Bearer secret-token-123"


class TestApiErrors:
    def test_success_false_raises(self) -> None:
        fn = make_resource_function(ACCOUNTS)
        bad = {
            "success": False,
            "errors": [{"code": 9109, "message": "Invalid access token"}],
            "result": None,
        }
        with _mock_pages(bad):
            with pytest.raises(client.CloudflareError, match="Invalid access token"):
                invoke_resource(fn)
