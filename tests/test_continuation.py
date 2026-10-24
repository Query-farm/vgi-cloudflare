"""Continued table-in-out output: one reply per round of upstream pages.

A list or analytics function answers an input batch with several replies: the
first from ``process()``, the rest from ``resume()`` on the client's continuation
ticks. The harness round-trips the state through its codec between turns (the
HTTP path), so these tests also prove the resume position survives serialization.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import threading
import time
from typing import Any
from unittest.mock import patch

import httpx
import pyarrow as pa
import pytest
from vgi.protocol import _is_state_codec

import vgi_cloudflare.client as client
from tests.harness import invoke_resource, run_lateral
from vgi_cloudflare.client import CloudflareError
from vgi_cloudflare.descriptor import Column, Pagination, PathParam, ResourceDescriptor
from vgi_cloudflare.graphql import DATASETS, GRAPHQL_ROWS_PER_CALL, make_graphql_function
from vgi_cloudflare.items import ZONE
from vgi_cloudflare.resources import CORE_RESOURCES, ZONES
from vgi_cloudflare.runtime import (
    LATERAL_CONCURRENCY,
    LATERAL_ROWS_PER_CALL,
    FanOutState,
    make_item_function,
    make_lateral_list_function,
    make_resource_function,
)

DNS_RECORDS = next(d for d in CORE_RESOURCES if d.schema == "dns" and d.name == "records")
DAILY = next(d for d in DATASETS if d.name == "http_requests_daily")


def _wrapped(result: Any, **result_info: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"success": True, "errors": [], "result": result}
    if result_info:
        body["result_info"] = result_info
    return body


def _route(handler):  # noqa: ANN001
    """Patch HTTP GET with ``handler(path, params)``; return (patcher, calls)."""
    calls: list[tuple[str, dict[str, Any]]] = []
    lock = threading.Lock()

    def _fake_get(path, params=None, headers=None):  # noqa: ANN001
        with lock:
            calls.append((path, dict(params or {})))
        return handler(path, params or {})

    return patch.object(client._http, "get", side_effect=_fake_get), calls


def _paged_zones(pages: dict[str, int]):  # noqa: ANN202
    """Zone ``z`` has ``pages[z]`` pages of one record each (``<z>-p<page>``)."""

    def handler(path: str, params: dict[str, Any]) -> httpx.Response:
        zone, page = path.split("/")[2], int(params["page"])
        rec = {"id": f"{zone}-p{page}", "name": f"{zone}-p{page}", "type": "A"}
        return httpx.Response(200, json=_wrapped([rec], total_pages=pages[zone]))

    return handler


def _cursor_desc() -> ResourceDescriptor:
    return ResourceDescriptor(
        name="things",
        schema="s",
        path="/accounts/{account_id}/things",
        path_params=(PathParam("account_id"),),
        pagination=Pagination.CURSOR,
        columns=(Column("id", pa.string()),),
    )


class TestRegistration:
    def test_lists_resume_and_bound_their_calls(self) -> None:
        fn = make_lateral_list_function(DNS_RECORDS)
        assert fn.has_resume_override()
        assert fn.get_metadata().input_rows_per_call == LATERAL_ROWS_PER_CALL

    def test_lookups_are_single_reply(self) -> None:
        fn = make_item_function(ZONE)
        assert not fn.has_resume_override()
        assert fn.get_metadata().input_rows_per_call == LATERAL_ROWS_PER_CALL

    def test_analytics_resume_and_bound_their_calls(self) -> None:
        fn = make_graphql_function(DAILY)
        assert fn.has_resume_override()
        assert fn.get_metadata().input_rows_per_call == GRAPHQL_ROWS_PER_CALL

    def test_state_is_a_codec(self) -> None:
        # Non-codec state is silently dropped over HTTP, losing the resume position.
        assert _is_state_codec(FanOutState)


class TestPageRounds:
    def test_one_reply_per_round_of_pages(self) -> None:
        # a: 3 pages, b: 1 page, c: 2 pages; "a" twice (fetched once), and a NULL row.
        patcher, calls = _route(_paged_zones({"a": 3, "b": 1, "c": 2}))
        with patcher:
            run = run_lateral(
                make_lateral_list_function(DNS_RECORDS),
                {"zone_id": ["a", "b", None, "c", "a"]},
                calls=calls,
            )
        replies = run.replies[0]
        assert [r.more for r in replies] == [True, True, False]
        assert [r.requests for r in replies] == [3, 2, 1]  # pending keys only, deduped
        ids = [r.batch.column("id").to_pylist() for r in replies]
        assert ids == [
            ["a-p1", "b-p1", "c-p1", "a-p1"],
            ["a-p2", "c-p2", "a-p2"],
            ["a-p3", "a-p3"],
        ]
        # Every reply's provenance indexes the one real input batch.
        assert [r.parent_rows for r in replies] == [[0, 1, 3, 4], [0, 3, 4], [0, 4]]
        assert run.table().column("zone_id").to_pylist() == [
            "a", "b", "c", "a", "a", "c", "a", "a", "a"
        ]  # fmt: skip
        assert sorted(p["page"] for _, p in calls) == [1, 1, 1, 2, 2, 3]

    def test_a_single_page_finishes_in_the_first_reply(self) -> None:
        patcher, calls = _route(_paged_zones({"a": 1, "b": 1}))
        with patcher:
            run = run_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": ["a", "b"]}, calls=calls)
        assert [r.more for r in run.replies[0]] == [False]
        assert len(calls) == 2  # no request for a page that isn't there

    def test_only_null_ids_is_one_empty_reply(self) -> None:
        patcher, calls = _route(_paged_zones({}))
        with patcher:
            run = run_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": [None, None]})
        assert [(r.batch.num_rows, r.more) for r in run.replies[0]] == [(0, False)]
        assert calls == []

    def test_empty_listing_has_no_more(self) -> None:
        patcher, _ = _route(lambda path, params: httpx.Response(200, json=_wrapped([], total_pages=5)))
        with patcher:
            run = run_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": ["a"]})
        assert [(r.batch.num_rows, r.more) for r in run.replies[0]] == [(0, False)]

    def test_named_args_ride_every_page(self) -> None:
        patcher, calls = _route(_paged_zones({"a": 2}))
        with patcher:
            run_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": ["a"]}, named={"type": "A"})
        assert [p.get("type") for _, p in calls] == ["A", "A"]

    def test_cursor_pagination(self) -> None:
        def handler(path: str, params: dict[str, Any]) -> httpx.Response:
            account, cursor = path.split("/")[2], params.get("cursor")
            n = {None: 1, f"{account}-c1": 2}.get(cursor, 3)
            nxt = f"{account}-c{n}" if n < (3 if account == "x" else 1) else None
            return httpx.Response(200, json=_wrapped([{"id": f"{account}{n}"}], cursor=nxt))

        patcher, calls = _route(handler)
        with patcher:
            run = run_lateral(make_lateral_list_function(_cursor_desc()), {"account_id": ["x", "y"]})
        assert [r.batch.column("id").to_pylist() for r in run.replies[0]] == [["x1", "y1"], ["x2"], ["x3"]]
        assert [p.get("cursor") for _, p in calls if "/x/" in _] == [None, "x-c1", "x-c2"]

    def test_unpaginated_list_is_one_reply(self) -> None:
        desc = dataclasses.replace(_cursor_desc(), pagination=Pagination.NONE)
        patcher, _ = _route(
            lambda path, params: httpx.Response(200, json=_wrapped([{"id": "1"}, {"id": "2"}]))
        )
        with patcher:
            run = run_lateral(make_lateral_list_function(desc), {"account_id": ["x"]})
        assert [(r.batch.num_rows, r.more) for r in run.replies[0]] == [(2, False)]

    def test_concurrency_is_bounded(self) -> None:
        active = peak = 0
        lock = threading.Lock()
        zones = {f"z{i}": 2 for i in range(20)}
        inner = _paged_zones(zones)

        def handler(path: str, params: dict[str, Any]) -> httpx.Response:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return inner(path, params)

        patcher, _ = _route(handler)
        with patcher:
            run = run_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": list(zones)})
        assert run.table().num_rows == 40
        assert 1 < peak <= LATERAL_CONCURRENCY

    def test_state_resets_for_the_next_input_batch(self) -> None:
        patcher, _ = _route(_paged_zones({"a": 2, "b": 2, "x": 1}))
        with patcher:
            run = run_lateral(
                make_lateral_list_function(DNS_RECORDS), [{"zone_id": ["a"]}, {"zone_id": ["x", "b"]}]
            )
        assert [r.batch.column("id").to_pylist() for r in run.replies[0]] == [["a-p1"], ["a-p2"]]
        assert [r.batch.column("id").to_pylist() for r in run.replies[1]] == [["x-p1", "b-p1"], ["b-p2"]]
        assert run.parents(1) == [0, 1, 1]


class TestStateCodec:
    def test_state_holds_positions_only_and_round_trips(self) -> None:
        patcher, _ = _route(_paged_zones({"a": 3, "b": 1, "c": 2}))
        fn = make_lateral_list_function(DNS_RECORDS)
        states: list[FanOutState] = []
        original = fn.resume.__func__

        def spy(cls, params, state, out):  # noqa: ANN001, ANN202
            states.append(copy.deepcopy(state))
            return original(cls, params, state, out)

        with patcher, patch.object(fn, "resume", classmethod(spy)):
            run_lateral(fn, {"zone_id": ["a", "b", "c", "a"]})
        # The state the first tick resumes from came through serialize/deserialize.
        first = states[0]
        assert first == FanOutState(keys=[["a"], ["c"]], rows=[[0, 3], [2]], positions=["2", "2"])
        assert [f.name for f in dataclasses.fields(FanOutState)] == ["keys", "rows", "positions"]
        assert FanOutState.deserialize_from_bytes(first.serialize_to_bytes()) == first


class TestErrors:
    def test_error_mid_continuation_fails_the_query(self) -> None:
        def handler(path: str, params: dict[str, Any]) -> httpx.Response:
            if params["page"] == 2:
                return httpx.Response(
                    500, json={"success": False, "errors": [{"code": 1, "message": "boom"}]}
                )
            return _paged_zones({"a": 3})(path, params)

        patcher, calls = _route(handler)
        fn = make_lateral_list_function(DNS_RECORDS)
        resumed_from: list[Any] = []
        original = fn.resume.__func__

        def spy(cls, params, state, out):  # noqa: ANN001, ANN202
            resumed_from.append(list(state.positions))
            return original(cls, params, state, out)

        with (
            patcher,
            patch.object(fn, "resume", classmethod(spy)),
            pytest.raises(CloudflareError, match="boom"),
        ):
            run_lateral(fn, {"zone_id": ["a"]})
        assert resumed_from == [["2"]]  # the first reply was sent; its tick failed
        assert [p["page"] for _, p in calls] == [1, 2]

    def test_list_404_on_a_later_page_is_an_error(self) -> None:
        def handler(path: str, params: dict[str, Any]) -> httpx.Response:
            if params["page"] == 2:
                return httpx.Response(
                    404, json={"success": False, "errors": [{"code": 7003, "message": "gone"}]}
                )
            return _paged_zones({"a": 2})(path, params)

        patcher, _ = _route(handler)
        with patcher, pytest.raises(CloudflareError, match="404"):
            run_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": ["a"]})

    def test_auth_rides_every_turn(self) -> None:
        seen: list[str] = []

        def _fake_get(path, params=None, headers=None):  # noqa: ANN001
            seen.append(headers["Authorization"])
            return _paged_zones({"a": 2})(path, params or {})

        with patch.object(client._http, "get", side_effect=_fake_get):
            run_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": ["a"]})
        assert seen == ["Bearer test-token", "Bearer test-token"]  # the resume turn too


class TestLimit:
    def test_a_limit_stops_fetching(self) -> None:
        # The client stops ticking after the first reply: later pages are never fetched.
        patcher, calls = _route(_paged_zones({"a": 50}))
        with patcher:
            run = run_lateral(make_lateral_list_function(DNS_RECORDS), {"zone_id": ["a"]}, max_replies=1)
        assert run.replies[0][-1].more
        assert len(calls) == 1


class TestAnalyticsPages:
    def test_one_reply_per_time_page_across_zones(self) -> None:
        desc = dataclasses.replace(DAILY, limit=2)
        days = [datetime.date(2024, 1, 1) + datetime.timedelta(days=i) for i in range(5)]
        # z1 has a group every day: pages of two from days 1, 2, 3, 4 and 5 (each full
        # page keeps its days before the last, and the next starts at that last day).
        # z2 has day 1 only, so it finishes in the first reply.
        have = {"z1": days, "z2": days[:1]}
        posts: list[dict[str, Any]] = []

        def _fake_post(path, json=None, headers=None):  # noqa: ANN001
            v = json["variables"]
            posts.append(v)
            hit = [d for d in have[v["tag"]] if v["since"] <= d.isoformat() <= v["until"]][: desc.limit]
            groups = [{"dimensions": {"date": d.isoformat()}, "sum": {"requests": 1}} for d in hit]
            return httpx.Response(200, json={"data": {"viewer": {"zones": [{desc.dataset: groups}]}}})

        with patch.object(client._http, "post", side_effect=_fake_post):
            run = run_lateral(
                make_graphql_function(desc),
                {"zone_id": ["z1", "z2"]},
                named={"since": days[0], "until": days[-1]},
            )
        replies = run.replies[0]
        assert [r.more for r in replies] == [True, True, True, True, False]
        assert [r.batch.column("zone_id").to_pylist() for r in replies] == [
            ["z1", "z2"], ["z1"], ["z1"], ["z1"], ["z1"]
        ]  # fmt: skip
        assert [p for r in replies for p in r.parent_rows] == [0, 1, 0, 0, 0, 0]
        assert (
            sorted(run.table().filter(pa.compute.equal(run.table()["zone_id"], "z1"))["date"].to_pylist())
            == days
        )
        assert [v["since"] for v in posts if v["tag"] == "z1"] == [d.isoformat() for d in days]


class TestUnscopedTablePages:
    def test_one_page_per_process_call(self) -> None:
        def handler(path: str, params: dict[str, Any]) -> httpx.Response:
            page = int(params["page"])
            return httpx.Response(200, json=_wrapped([{"id": f"z{page}", "name": "n"}], total_pages=3))

        patcher, calls = _route(handler)
        with patcher:
            table = invoke_resource(make_resource_function(ZONES))
        assert table.column("id").to_pylist() == ["z1", "z2", "z3"]
        assert [p["page"] for _, p in calls] == [1, 2, 3]

    def test_a_limit_stops_fetching(self) -> None:
        patcher, calls = _route(
            lambda path, params: httpx.Response(200, json=_wrapped([{"id": "z", "name": "n"}], total_pages=9))
        )
        with patcher:
            table = invoke_resource(make_resource_function(ZONES), max_calls=1)
        assert table.num_rows == 1
        assert len(calls) == 1
