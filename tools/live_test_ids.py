"""Print ``<zone_id> <account_id>`` for the token in ``CLOUDFLARE_API_TOKEN``.

Used by ``make test-stdio`` so the live analytics tests (which need literal ids
at bind time) run against whatever zone/account the token can see. Prints
nothing if the token is missing or sees no zones.
"""

from __future__ import annotations

import os

import httpx

API = "https://api.cloudflare.com/client/v4"


def main() -> None:
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}"}
    try:
        zones = httpx.get(f"{API}/zones", params={"per_page": 50, "order": "name"}, headers=headers).json()
        accounts = httpx.get(f"{API}/accounts", headers=headers).json()
    except httpx.HTTPError:
        return
    if zones.get("result") and accounts.get("result"):
        print(zones["result"][0]["id"], accounts["result"][0]["id"])


if __name__ == "__main__":
    main()
