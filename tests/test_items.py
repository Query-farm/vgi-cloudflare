"""Tests for get-one-by-id ("item") table functions."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pyarrow as pa

import vgi_cloudflare.client as client
from tests.harness import invoke_item
from vgi_cloudflare.descriptor import Column, Pagination, PathParam, ResourceDescriptor
from vgi_cloudflare.items import ACCOUNT, DNS_RECORD, USER, ZONE
from vgi_cloudflare.runtime import make_item_function


def _wrapped(result: Any) -> dict[str, Any]:
    return {"success": True, "errors": [], "result": result}


def _mock_get(body: dict[str, Any], capture: dict[str, Any] | None = None):
    def _fake_get(path, params=None, headers=None):  # noqa: ANN001
        if capture is not None:
            capture["path"] = path
        return httpx.Response(200, json=body)

    return patch.object(client._http, "get", side_effect=_fake_get)


class TestZeroArgs:
    def test_user_single_row(self) -> None:
        fn = make_item_function(USER)
        body = _wrapped({"id": "u1", "email": "a@b.com", "first_name": "Ada"})
        with _mock_get(body):
            table = invoke_item(fn)
        assert table.num_rows == 1
        assert table.to_pylist()[0]["email"] == "a@b.com"


class TestOneArg:
    def test_account_url_and_echo(self) -> None:
        fn = make_item_function(ACCOUNT)
        capture: dict[str, Any] = {}
        with _mock_get(_wrapped({"id": "acctZ", "name": "Acme"}), capture):
            table = invoke_item(fn, "acctZ")
        assert capture["path"] == "/accounts/acctZ"
        row = table.to_pylist()[0]
        assert row["account_id"] == "acctZ"  # path param echoed as a column
        assert row["name"] == "Acme"


class TestTwoArgs:
    def test_dns_record_url_order_and_both_echoed(self) -> None:
        fn = make_item_function(DNS_RECORD)
        capture: dict[str, Any] = {}
        body = _wrapped({"id": "rec1", "name": "www.example.com", "type": "A", "content": "192.0.2.1"})
        with _mock_get(body, capture):
            table = invoke_item(fn, "zoneABC", "rec1")
        # URL order: zone first, then record.
        assert capture["path"] == "/zones/zoneABC/dns_records/rec1"
        row = table.to_pylist()[0]
        assert row["zone_id"] == "zoneABC"
        assert row["dns_record_id"] == "rec1"
        assert row["content"] == "192.0.2.1"


class TestRowCounts:
    def test_null_result_is_zero_rows(self) -> None:
        fn = make_item_function(ZONE)
        with _mock_get(_wrapped(None)):  # API returns result: null (404 / not found)
            table = invoke_item(fn, "missing-zone")
        assert table.num_rows == 0

    def test_object_result_is_exactly_one_row(self) -> None:
        fn = make_item_function(ZONE)
        with _mock_get(_wrapped({"id": "z1", "name": "example.com", "status": "active"})):
            table = invoke_item(fn, "z1")
        assert table.num_rows == 1


class TestWrapperless:
    """Endpoints whose body IS the object (no `result` envelope), via result_path=''."""

    WRAP = ResourceDescriptor(
        name="raw_thing",
        path="/raw/{id}",
        kind="item",
        pagination=Pagination.NONE,
        result_path="",
        path_params=(PathParam("id", "Thing id."),),
        columns=(
            Column("foo", pa.string(), "A field."),
            Column("bar", pa.int64(), "A number."),
        ),
    )

    def test_whole_body_is_the_row(self) -> None:
        fn = make_item_function(self.WRAP)
        with _mock_get({"foo": "hello", "bar": 7}):  # no envelope — body is the object
            table = invoke_item(fn, "thing1")
        assert table.num_rows == 1
        row = table.to_pylist()[0]
        assert row == {"id": "thing1", "foo": "hello", "bar": 7}

    def test_empty_body_is_zero_rows(self) -> None:
        fn = make_item_function(self.WRAP)
        with _mock_get({}):
            table = invoke_item(fn, "thing1")
        # An empty dict body → falsy → 0 rows.
        assert table.num_rows == 0
