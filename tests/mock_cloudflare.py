"""A tiny fake Cloudflare API for end-to-end SQL tests without a token.

    python tests/mock_cloudflare.py <port>

Serves three zones (``z1``..``z3``); zone ``zN`` has ``N`` DNS records per page
across two pages, so fan-out counts are 2/4/6. Unknown zones 404. Point the
worker at it with ``CLOUDFLARE_API_BASE=http://127.0.0.1:<port>/client/v4``.
"""

from __future__ import annotations

import json
import sys
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


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), _Handler).serve_forever()
