"""A tiny fake Cloudflare API for end-to-end SQL tests without a token.

    python tests/mock_cloudflare.py <port>

Serves three zones (``z1``..``z3``); zone ``zN`` has ``N`` DNS records per page
across two pages, so fan-out counts are 2/4/6. Unknown zones 404. Point the
worker at it with ``CLOUDFLARE_API_BASE=http://127.0.0.1:<port>/client/v4``.

For continued output (one reply per round of upstream pages):

- DNS records of zone ``pN`` (not in the zone list): ``N`` pages of 3 records.
- Agent-memory namespaces of account ``aN``: ``N`` cursor pages of 2.
- GraphQL analytics: every dataset has 3 groups per second of its window (country
  ``DE``/``FR``/``US``, count 1), capped at the query's ``limit`` like the real API,
  so an hour (10,800 groups) takes two time pages.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

ZONES = [{"id": f"z{i}", "name": f"zone{i}.com", "status": "active"} for i in range(1, 4)]
NOT_FOUND = {"success": False, "errors": [{"code": 1001, "message": "not found"}]}


def _body(result: Any, **result_info: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"success": True, "errors": [], "result": result}
    if result_info:
        body["result_info"] = result_info
    return body


def route(path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    parts = path.split("/")[3:]  # drop "", "client", "v4"
    if parts == ["zones"]:
        return 200, _body(ZONES, total_pages=1)
    if len(parts) == 2 and parts[0] == "zones":
        zone = next((z for z in ZONES if z["id"] == parts[1]), None)
        return (200, _body(zone)) if zone else (404, NOT_FOUND)
    if len(parts) == 3 and parts[0] == "zones" and parts[2] == "dns_records" and parts[1][:1] == "p":
        n, page = int(parts[1][1:]), int(query.get("page", ["1"])[0])
        records = [
            {"id": f"{parts[1]}-{page}-{k}", "name": f"r{k}.{parts[1]}", "type": "A", "content": "192.0.2.1"}
            for k in range(3)
        ]
        return 200, _body(records if page <= n else [], total_pages=n)
    if parts[:1] == ["accounts"] and parts[2:] == ["agent-memory", "namespaces"]:
        n, page = int(parts[1][1:]), int(query.get("cursor", ["c1"])[0][1:])
        items = [{"id": f"{parts[1]}-{page}-{k}", "name": f"ns{k}"} for k in range(2)]
        return 200, _body(items, cursor=f"c{page + 1}" if page < n else "")
    if len(parts) == 3 and parts[0] == "zones" and parts[2] == "dns_records":
        zone = next((z for z in ZONES if z["id"] == parts[1]), None)
        if zone is None:
            return 404, NOT_FOUND
        n, page = int(zone["id"][1:]), int(query.get("page", ["1"])[0])
        records = [
            {
                "id": f"{zone['id']}-p{page}-{k}",
                "name": f"r{k}.{zone['name']}",
                "type": "A",
                "content": "192.0.2.1",
            }
            for k in range(n)
        ]
        return 200, _body(records, total_pages=2)
    return 404, NOT_FOUND


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def graphql(body: dict[str, Any]) -> dict[str, Any]:
    """Three groups per second of the window, ordered by time, capped at ``limit``."""
    query, v = body["query"], body["variables"]
    dataset = re.search(r"(\w+)\(\s*limit", query).group(1)
    limit = int(re.search(r"limit: (\d+)", query).group(1))
    groups: list[dict[str, Any]] = []
    t, until = _time(v["since"]), _time(v["until"])
    while t <= until and len(groups) < limit:
        stamp = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        for country in ("DE", "FR", "US")[: limit - len(groups)]:
            groups.append(
                {
                    "dimensions": {"datetime": stamp, "date": stamp[:10], "clientCountryName": country},
                    "count": 1,
                    "sum": {"edgeResponseBytes": 1, "requests": 1},
                }
            )
        t += timedelta(seconds=1)
    plural = "accounts" if "accountTag" in query else "zones"
    return {"data": {"viewer": {plural: [{dataset: groups}]}}}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        status, body = route(url.path, parse_qs(url.query))
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = json.dumps(graphql(body)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), _Handler).serve_forever()
