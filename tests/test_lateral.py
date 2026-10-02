"""Tests for lateral (RowTransformFunction) lookup and `_by_<parent>` fan-out functions."""

from __future__ import annotations

import dataclasses
import threading
from typing import Any
from unittest.mock import patch

import httpx
import pyarrow as pa
import pytest

import vgi_cloudflare.client as client
from tests.harness import invoke_lateral
from vgi_cloudflare.catalog import build_catalog, merge_resources
from vgi_cloudflare.client import CloudflareError
from vgi_cloudflare.descriptor import Column, Pagination, PathParam, ResourceDescriptor
from vgi_cloudflare.generated import GENERATED_RESOURCES
from vgi_cloudflare.items import ITEM_RESOURCES, ZONE
from vgi_cloudflare.resources import CORE_RESOURCES
from vgi_cloudflare.runtime import lateral_list_name, make_item_function, make_lateral_list_function

DNS_RECORDS = next(d for d in CORE_RESOURCES if d.schema == "dns" and d.name == "records")


def _wrapped(result: Any, **result_info: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"success": True, "errors": [], "result": result}
    if result_info:
        body["result_info"] = result_info
    return body


def _route(handler):  # noqa: ANN001
    """Patch the HTTP client with ``handler(path, params) -> httpx.Response``; record calls."""
    calls: list[tuple[str, dict[str, Any]]] = []
    lock = threading.Lock()

    def _fake_get(path, params=None, headers=None):  # noqa: ANN001
        with lock:
            calls.append((path, dict(params or {})))
        return handler(path, params or {})

    return patch.object(client._http, "get", side_effect=_fake_get), calls


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"success": False, "errors": [{"code": 1001, "message": "not found"}]})


class TestLateralItem:
    def test_is_row_transform_with_path_params_as_inputs(self) -> None:
        fn = make_item_function(ZONE)
        assert fn.get_metadata().input_from_args

    def test_batch_maps_each_output_row_to_its_input_row(self) -> None:
        def handler(path: str, params: dict[str, Any]) -> httpx.Response:
            zone = path.rsplit("/", 1)[-1]
            if zone == "gone":
                return _not_found()
            return httpx.Response(200, json=_wrapped({"id": zone, "name": f"{zone}.com"}))

        patcher, calls = _route(handler)
        with patcher:
            table, parents = invoke_lateral(
                make_item_function(ZONE), {"zone_id": ["a", "gone", None, "b", "a"]}
            )

        # 404 and NULL inputs drop out; the duplicate "a" is fetched once but emitted twice.
        assert parents == [0, 3, 4]
        assert table.column("name").to_pylist() == ["a.com", "b.com", "a.com"]
        assert table.column("zone_id").to_pylist() == ["a", "b", "a"]
        assert sorted(p for p, _ in calls) == ["/zones/a", "/zones/b", "/zones/gone"]

    def test_non_404_error_fails_the_query(self) -> None:
        def handler(path: str, params: dict[str, Any]) -> httpx.Response:
            return httpx.Response(
                403, json={"success": False, "errors": [{"code": 9109, "message": "denied"}]}
            )

        patcher, _ = _route(handler)
        with patcher, pytest.raises(CloudflareError, match="403"):
            invoke_lateral(make_item_function(ZONE), {"zone_id": ["a"]})

    def test_all_rows_missing_emits_empty_batch(self) -> None:
        patcher, _ = _route(lambda path, params: _not_found())
        with patcher:
            table, parents = invoke_lateral(make_item_function(ZONE), {"zone_id": ["x", "y"]})
        assert table.num_rows == 0
        assert parents == []

    def test_path_values_are_escaped(self) -> None:
        patcher, calls = _route(lambda path, params: httpx.Response(200, json=_wrapped(None)))
        with patcher:
            invoke_lateral(make_item_function(ZONE), {"zone_id": ["../accounts"]})
        assert calls[0][0] == "/zones/..%2Faccounts"


class TestLateralList:
    def test_name_and_shape(self) -> None:
        fn = make_lateral_list_function(DNS_RECORDS)
        assert fn.Meta.name == "records_by_zone"
        assert fn.get_metadata().input_from_args

    def test_fans_out_each_input_row_across_pages(self) -> None:
        def handler(path: str, params: dict[str, Any]) -> httpx.Response:
            zone = path.split("/")[2]
            if zone == "z1":
                page = params["page"]
                recs = [{"id": f"r{page}", "name": f"p{page}.z1", "type": "A"}]
                return httpx.Response(200, json=_wrapped(recs, total_pages=2))
            if zone == "empty":
                return httpx.Response(200, json=_wrapped([], total_pages=0))
            return httpx.Response(
                200, json=_wrapped([{"id": "r9", "name": "z2", "type": "MX"}], total_pages=1)
            )

        patcher, _ = _route(handler)
        with patcher:
            table, parents = invoke_lateral(
                make_lateral_list_function(DNS_RECORDS), {"zone_id": ["z1", "empty", "z2"]}
            )

        assert parents == [0, 0, 2]
        assert table.column("name").to_pylist() == ["p1.z1", "p2.z1", "z2"]
        assert table.column("zone_id").to_pylist() == ["z1", "z1", "z2"]

    def test_list_404_is_an_error(self) -> None:
        patcher, _ = _route(lambda path, params: _not_found())
        with patcher, pytest.raises(CloudflareError, match="404"):
            invoke_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": ["nope"]})


class TestRateLimit:
    def test_429_is_retried(self) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"Retry-After": "0"}, json={"success": False, "errors": []}),
                httpx.Response(200, json=_wrapped({"id": "a", "name": "a.com"})),
            ]
        )
        patcher, calls = _route(lambda path, params: next(responses))
        with patcher:
            table, _ = invoke_lateral(make_item_function(ZONE), {"zone_id": ["a"]})
        assert len(calls) == 2
        assert table.num_rows == 1


class TestNaming:
    def _list(self, path: str, *params: str) -> ResourceDescriptor:
        return ResourceDescriptor(
            name="things",
            schema="s",
            path=path,
            path_params=tuple(PathParam(p) for p in params),
            columns=(Column("id", pa.string()),),
        )

    @pytest.mark.parametrize(
        ("path", "params", "expected"),
        [
            ("/zones/{zone_id}/things", ("zone_id",), "things_by_zone"),
            ("/accounts/{account_identifier}/things", ("account_identifier",), "things_by_account"),
            ("/accounts/{a}/workers/scripts/{script_name}/things", ("a", "script_name"), "things_by_script"),
            ("/accounts/{a}/policies/{id}/things", ("a", "id"), "things_by_policy"),
            ("/accounts/{a}/categories/{name}/things", ("a", "name"), "things_by_category"),
        ],
    )
    def test_fan_out_named_by_most_specific_parent(
        self, path: str, params: tuple[str, ...], expected: str
    ) -> None:
        assert lateral_list_name(self._list(path, *params)) == expected

    def test_catalog_names(self) -> None:
        cat = build_catalog(merge_resources(CORE_RESOURCES + ITEM_RESOURCES, GENERATED_RESOURCES))
        names = {(s.path[0], f.Meta.name) for s in cat.schemas for f in s.functions}
        for expected in [
            ("dns", "record"),
            ("dns", "records_by_zone"),
            ("zones", "zone"),
            ("accounts", "user"),
        ]:
            assert expected in names
        # No verb prefixes ("list" alone is a real Cloudflare noun, e.g. Rules lists).
        assert not any(n.startswith("get_") for _, n in names)


class TestPathFormatting:
    def test_non_identifier_placeholder_is_filled_in_order(self) -> None:
        desc = ResourceDescriptor(
            name="session",
            schema="s",
            path="/accounts/{account_id}/sessions/{livestream-session-id}",
            kind="item",
            path_params=(PathParam("account_id"), PathParam("livestream_session_id")),
            columns=(Column("id", pa.string()),),
        )
        patcher, calls = _route(lambda path, params: httpx.Response(200, json=_wrapped({"id": "x"})))
        with patcher:
            invoke_lateral(make_item_function(desc), {"account_id": ["a1"], "livestream_session_id": ["s/1"]})
        assert calls[0][0] == "/accounts/a1/sessions/s%2F1"

    def test_generated_paths_and_params_line_up(self) -> None:
        import re

        for d in GENERATED_RESOURCES:
            assert len(re.findall(r"\{[^}]+\}", d.path)) == len(d.path_params), d.path


class TestTokenFileFallback:
    def test_used_only_without_a_secret(self, tmp_path, monkeypatch) -> None:  # noqa: ANN001
        from vgi_cloudflare.runtime import TOKEN_FILE_ENV, _auth_from_secrets

        token_file = tmp_path / "token"
        token_file.write_text("from-file\n")
        monkeypatch.setenv(TOKEN_FILE_ENV, str(token_file))
        assert _auth_from_secrets({}).api_token == "from-file"
        secret = {"cloudflare": {"api_token": pa.scalar("from-secret")}}
        assert _auth_from_secrets(secret).api_token == "from-secret"  # a secret always wins

    def test_unset_means_no_fallback(self, monkeypatch) -> None:  # noqa: ANN001
        from vgi_cloudflare.runtime import TOKEN_FILE_ENV, _auth_from_secrets

        monkeypatch.delenv(TOKEN_FILE_ENV, raising=False)
        assert _auth_from_secrets({}).api_token is None


class TestQueryArguments:
    def _radar(self) -> ResourceDescriptor:
        from vgi_cloudflare.descriptor import QueryParam

        return ResourceDescriptor(
            name="http_versions",
            schema="radar",
            path="/radar/http/summary/http_version",
            kind="item",
            pagination=Pagination.NONE,
            query_params=(
                QueryParam("date_range", pa.string(), api_name="dateRange"),
                QueryParam("location", pa.string()),
            ),
            default_query=(("dateRange", "7d"),),
            columns=(Column("summary_0", pa.string()),),
        )

    def test_default_window_sent_and_named_args_override(self) -> None:
        from tests.harness import invoke_resource

        patcher, calls = _route(lambda path, params: httpx.Response(200, json=_wrapped({"summary_0": {}})))
        fn = make_item_function(self._radar())
        with patcher:
            invoke_resource(fn)  # scanned with no arguments: the default window applies
        assert calls[0][1] == {"dateRange": "7d"}
        assert fn.FunctionArguments(date_range="28d", location="US") is not None

    def test_explicit_dates_replace_the_default_window(self) -> None:
        from vgi_cloudflare.runtime import _named_query

        desc = self._radar()
        from vgi_cloudflare.descriptor import QueryParam

        desc = dataclasses.replace(
            desc,
            query_params=(*desc.query_params, QueryParam("date_start", pa.string(), api_name="dateStart")),
        )
        args = make_item_function(desc).FunctionArguments(date_start="2025-01-01T00:00:00Z")
        assert _named_query(desc, args) == {"dateStart": "2025-01-01T00:00:00Z"}
        assert _named_query(desc, make_item_function(desc).FunctionArguments()) == {"dateRange": "7d"}
        # An endpoint with start/end but no default window: nothing to replace.
        bare = dataclasses.replace(desc, default_query=())
        args = make_item_function(bare).FunctionArguments(date_start="2025-01-01T00:00:00Z")
        assert _named_query(bare, args) == {"dateStart": "2025-01-01T00:00:00Z"}

    def test_bare_array_body_is_rows(self) -> None:
        desc = ResourceDescriptor(
            name="jobs",
            schema="workers",
            path="/accounts/{account_id}/slurper/jobs",
            path_params=(PathParam("account_id"),),
            pagination=Pagination.NONE,
            columns=(Column("id", pa.string()),),
        )
        patcher, _ = _route(lambda path, params: httpx.Response(200, json=[{"id": "j1"}, {"id": "j2"}]))
        with patcher:
            table, parents = invoke_lateral(make_lateral_list_function(desc), {"account_id": ["a"]})
        assert table.column("id").to_pylist() == ["j1", "j2"]
        assert parents == [0, 0]
