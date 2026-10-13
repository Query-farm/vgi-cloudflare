"""Tests for the descriptor-driven GraphQL analytics functions (mocked GraphQL POST)."""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any
from unittest.mock import patch

import httpx
import pyarrow as pa
import pytest

import vgi_cloudflare.client as client
from tests.harness import invoke_lateral
from vgi_cloudflare.client import CloudflareError
from vgi_cloudflare.graphql import DATASETS, build_query, make_graphql_function

_BY_NAME = {d.name: d for d in DATASETS}
_UTC = pa.timestamp("us", tz="UTC")
_DAY = (datetime.date(2024, 1, 1), datetime.date(2024, 1, 7))
_TIME = (
    pa.scalar(datetime.datetime(2024, 3, 1, tzinfo=datetime.UTC), type=_UTC),
    pa.scalar(datetime.datetime(2024, 3, 2, tzinfo=datetime.UTC), type=_UTC),
)


def _window(desc) -> dict[str, Any]:  # noqa: ANN001
    since, until = _DAY if desc.time_field == "date" else _TIME
    return {"since": since, "until": until}


def _call(desc, ids: list[str | None], window: dict[str, Any] | None = None):  # noqa: ANN001
    """Call a dataset function for each scope id, with a window (default: one per grain)."""
    return invoke_lateral(make_graphql_function(desc), {desc.scope_id: ids}, named=window or _window(desc))


def _mock_graphql(zone_or_account: str, dataset: str, groups: list[dict], capture: dict | None = None):
    body = {"data": {"viewer": {zone_or_account: [{dataset: groups}]}}}

    def _fake_post(path, json=None, headers=None):  # noqa: ANN001
        if capture is not None:
            capture.setdefault("bodies", []).append(json)
            capture["body"] = json
        return httpx.Response(200, json=body)

    return patch.object(client._http, "post", side_effect=_fake_post)


class TestHttpRequestsDaily:
    def test_sum_and_uniq_extraction(self) -> None:
        groups = [
            {
                "dimensions": {"date": "2024-01-01"},
                "sum": {"requests": 1000, "bytes": 2048, "threats": 5},
                "uniq": {"uniques": 120},
            }
        ]
        with _mock_graphql("zones", "httpRequests1dGroups", groups):
            table, _ = _call(_BY_NAME["http_requests_daily"], ["z1"])
        row = table.to_pylist()[0]
        assert row["zone_id"] == "z1"
        assert row["date"] == datetime.date(2024, 1, 1)
        assert row["requests"] == 1000
        assert row["threats"] == 5
        assert row["unique_visitors"] == 120


class TestFirewallEvents:
    def test_dimensions_and_count(self) -> None:
        groups = [
            {
                "dimensions": {
                    "datetime": "2024-01-01T12:00:00Z",
                    "action": "block",
                    "source": "waf",
                    "clientCountryName": "US",
                    "clientRequestHTTPHost": "ex.com",
                    "ruleId": "r1",
                },
                "count": 42,
            }
        ]
        with _mock_graphql("zones", "firewallEventsAdaptiveGroups", groups):
            table, _ = _call(_BY_NAME["firewall_events"], ["z1"])
        row = table.to_pylist()[0]
        assert row["action"] == "block"
        assert row["client_country"] == "US"  # renamed dimension column
        assert row["rule_id"] == "r1"
        assert row["count"] == 42
        assert row["datetime"].year == 2024


class TestHealthChecks:
    def test_avg_metric(self) -> None:
        groups = [
            {
                "dimensions": {
                    "datetime": "2024-01-01T00:00:00Z",
                    "fqdn": "a.com",
                    "region": "weu",
                    "healthStatus": "healthy",
                    "originIP": "1.2.3.4",
                },
                "count": 10,
                "avg": {"rttMs": 12.5},
            }
        ]
        with _mock_graphql("zones", "healthCheckEventsAdaptiveGroups", groups):
            table, _ = _call(_BY_NAME["health_check_events"], ["z1"])
        row = table.to_pylist()[0]
        assert row["avg_rtt_ms"] == 12.5
        assert row["health_status"] == "healthy"


class TestAccountScope:
    def test_workers_invocations_uses_accounts(self) -> None:
        capture: dict[str, Any] = {}
        groups = [
            {
                "dimensions": {"datetime": "2024-01-01T00:00:00Z", "scriptName": "w", "status": "success"},
                "sum": {"requests": 10, "errors": 1, "subrequests": 3},
            }
        ]
        with _mock_graphql("accounts", "workersInvocationsAdaptive", groups, capture):
            table, _ = _call(_BY_NAME["workers_invocations"], ["acctZ"])
        assert capture["body"]["variables"]["tag"] == "acctZ"
        assert "accounts(filter: {accountTag:" in capture["body"]["query"]
        row = table.to_pylist()[0]
        assert row["account_id"] == "acctZ"
        assert row["script_name"] == "w"
        assert row["errors"] == 1


class TestWindowAndScope:
    def test_window_arguments_become_the_api_time_filter(self) -> None:
        capture: dict[str, Any] = {}
        with _mock_graphql("zones", "firewallEventsAdaptiveGroups", [], capture):
            _call(_BY_NAME["firewall_events"], ["z1"])
        v = capture["body"]["variables"]
        assert (v["since"], v["until"]) == ("2024-03-01T00:00:00Z", "2024-03-02T00:00:00Z")

    def test_window_is_required(self) -> None:
        # A silently defaulted window would truncate what the caller asked for.
        with _mock_graphql("zones", "httpRequests1dGroups", []), pytest.raises(KeyError, match="since"):
            _call(_BY_NAME["http_requests_daily"], ["z1"], window={"until": _DAY[1]})

    def test_each_scope_id_is_queried_and_rows_map_back(self) -> None:
        capture: dict[str, Any] = {}
        groups = [{"dimensions": {"date": "2024-01-01"}, "sum": {"requests": 1}}]
        with _mock_graphql("zones", "httpRequests1dGroups", groups, capture):
            table, parents = _call(_BY_NAME["http_requests_daily"], ["z1", None, "z2", "z1"])
        assert parents == [0, 2, 3]  # NULL id: no rows; repeated id fetched once
        assert table.column("zone_id").to_pylist() == ["z1", "z2", "z1"]
        assert sorted(b["variables"]["tag"] for b in capture["bodies"]) == ["z1", "z2"]


class TestQueryBuilder:
    def test_count_and_sections_placement(self) -> None:
        q = build_query(_BY_NAME["http_requests_adaptive"])
        assert "clientCountryName" in q
        assert "count" in q
        assert "sum { edgeResponseBytes }" in q
        assert "datetime_geq: $since" in q


class TestPaging:
    """A response at the group limit may be truncated, so the next page resumes at its last bucket."""

    def _run(self, desc, groups_for, since, until):  # noqa: ANN001
        desc = dataclasses.replace(desc, limit=4)
        calls: list[dict[str, Any]] = []

        def _fake_post(path, json=None, headers=None):  # noqa: ANN001
            v = json["variables"]
            calls.append(v)
            body = {"data": {"viewer": {"zones": [{desc.dataset: groups_for(v["since"], v["until"])[:4]}]}}}
            return httpx.Response(200, json=body)

        with patch.object(client._http, "post", side_effect=_fake_post):
            table, _ = _call(desc, ["z1"], window={"since": since, "until": until})
        return table, calls

    def test_daily_window_is_paged_until_complete(self) -> None:
        days = [datetime.date(2024, 1, 1) + datetime.timedelta(days=i) for i in range(10)]

        def groups_for(since: str, until: str) -> list[dict]:
            hit = [d for d in days if since <= d.isoformat() <= until]
            return [{"dimensions": {"date": d.isoformat()}, "sum": {"requests": 1}} for d in hit]

        table, calls = self._run(_BY_NAME["http_requests_daily"], groups_for, days[0], days[-1])
        assert table.column("date").to_pylist() == days  # every day, none twice
        assert [c["since"] for c in calls] == ["2024-01-01", "2024-01-04", "2024-01-07", "2024-01-10"]

    def test_bucket_cut_by_a_page_is_refetched_whole(self) -> None:
        # Three groups per second: every page of four ends partway through a bucket.
        stamps = [f"2024-03-01T00:00:0{s}Z" for s in range(4)]
        rows = [(t, c) for t in stamps for c in ("DE", "FR", "US")]

        def groups_for(since: str, until: str) -> list[dict]:
            return [
                {"dimensions": {"datetime": t, "clientCountryName": c}, "count": 1}
                for t, c in rows
                if since <= t <= until
            ]

        table, calls = self._run(_BY_NAME["http_requests_adaptive"], groups_for, *_TIME)
        got = list(
            zip(table.column("datetime").to_pylist(), table.column("client_country").to_pylist(), strict=True)
        )
        assert [(t.strftime("%Y-%m-%dT%H:%M:%SZ"), c) for t, c in got] == rows
        assert [c["since"] for c in calls] == stamps

    def test_bucket_filling_a_page_warns_and_paging_resumes_after_it(self) -> None:
        def groups_for(since: str, until: str) -> list[dict]:
            crowded = [{"dimensions": {"date": "2024-01-01"}, "sum": {"requests": i}} for i in range(6)]
            rest = [{"dimensions": {"date": "2024-01-02"}, "sum": {"requests": 9}}]
            return [g for g in crowded + rest if since <= g["dimensions"]["date"] <= until]

        table, calls = self._run(
            _BY_NAME["http_requests_daily"], groups_for, datetime.date(2024, 1, 1), datetime.date(2024, 1, 2)
        )
        assert [c["since"] for c in calls] == ["2024-01-01", "2024-01-02"]
        assert table.column("requests").to_pylist() == [0, 1, 2, 3, 9]  # emitted, not dropped

    def test_last_bucket_filling_a_page_ends_paging(self) -> None:
        def groups_for(since: str, until: str) -> list[dict]:
            return [{"dimensions": {"date": since}, "sum": {"requests": i}} for i in range(4)]

        day = datetime.date(2024, 1, 1)
        table, calls = self._run(_BY_NAME["http_requests_daily"], groups_for, day, day)
        assert len(calls) == 1
        assert table.num_rows == 4


class TestPlanGatedDataset:
    def test_access_error_names_the_dataset_and_plan(self) -> None:
        body = {"data": None, "errors": [{"message": "zone 'z1' does not have access to the path"}]}
        with patch.object(client._http, "post", return_value=httpx.Response(200, json=body)):
            with pytest.raises(CloudflareError, match="firewallEventsAdaptiveGroups.*plans"):
                _call(_BY_NAME["firewall_events"], ["z1"])
