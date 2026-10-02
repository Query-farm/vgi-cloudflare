"""httpx-based Cloudflare API client: auth, pagination, and error handling.

Stateless by design — the API token arrives per query from the DuckDB secret,
so callers pass a :class:`CloudflareAuth` into each call. A module-global
``httpx.Client`` provides connection pooling across calls within a worker
process.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from .descriptor import Pagination, ResourceDescriptor

log = logging.getLogger(__name__)

#: Overridable for testing against a local mock (``CLOUDFLARE_API_BASE``).
API_BASE = os.environ.get("CLOUDFLARE_API_BASE", "https://api.cloudflare.com/client/v4")

_http = httpx.Client(timeout=30.0, base_url=API_BASE)

#: Retries on HTTP 429 (rate limited) before giving up; lateral fan-out makes it likely.
MAX_RATE_LIMIT_RETRIES = 4


@dataclass(frozen=True, slots=True)
class CloudflareAuth:
    """Resolved Cloudflare credentials for one query."""

    api_token: str | None = None
    api_key: str | None = None
    email: str | None = None

    def headers(self) -> dict[str, str]:
        if self.api_token:
            return {"Authorization": f"Bearer {self.api_token}"}
        if self.api_key and self.email:
            return {"X-Auth-Key": self.api_key, "X-Auth-Email": self.email}
        raise CloudflareError(
            "No Cloudflare credentials. Create a secret: "
            "CREATE SECRET cf (TYPE cloudflare, api_token '<token>');"
        )


class CloudflareError(RuntimeError):
    """A Cloudflare API error (non-2xx, or success:false)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _check(resp: httpx.Response, path: str) -> dict[str, Any]:
    """Parse a Cloudflare envelope, raising :class:`CloudflareError` on failure."""
    try:
        data = resp.json()
    except ValueError as exc:
        raise CloudflareError(
            f"Cloudflare {path}: non-JSON response (HTTP {resp.status_code})", resp.status_code
        ) from exc

    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
        errors = data.get("errors") if isinstance(data, dict) else None
        detail = (
            "; ".join(f"{e.get('code', '?')}: {e.get('message', '')}" for e in (errors or []))
            or resp.text[:200]
        )
        raise CloudflareError(f"Cloudflare {path}: HTTP {resp.status_code}: {detail}", resp.status_code)
    return data


def _get(auth: CloudflareAuth, path: str, params: dict[str, Any]) -> dict[str, Any]:
    clean = {k: v for k, v in params.items() if v is not None}
    headers = {"Accept": "application/json", **auth.headers()}
    resp = _http.get(path, params=clean, headers=headers)
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        if resp.status_code != 429:
            break
        time.sleep(_retry_delay(resp, attempt))
        resp = _http.get(path, params=clean, headers=headers)
    return _check(resp, path)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429: ``Retry-After`` if given, else backoff."""
    try:
        return min(float(resp.headers.get("Retry-After", "")), 60.0)
    except ValueError:
        return 0.5 * 2**attempt


GRAPHQL_PATH = "/graphql"


def graphql_post(auth: CloudflareAuth, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """POST a GraphQL query to the Cloudflare Analytics API; return the ``data`` object."""
    resp = _http.post(
        GRAPHQL_PATH,
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json", "Accept": "application/json", **auth.headers()},
    )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise CloudflareError(f"Cloudflare GraphQL: non-JSON response (HTTP {resp.status_code})") from exc
    if resp.status_code >= 400:
        raise CloudflareError(f"Cloudflare GraphQL: HTTP {resp.status_code}: {resp.text[:200]}")
    if payload.get("errors"):
        detail = "; ".join(e.get("message", "") for e in payload["errors"])
        raise CloudflareError(f"Cloudflare GraphQL error: {detail}")
    return payload.get("data") or {}


def _result_items(data: dict[str, Any] | list[Any], result_path: str) -> list[dict[str, Any]]:
    # result_path "" means the response body itself is the item (wrapperless).
    if isinstance(data, list):
        return data  # a bare JSON array body (no Cloudflare envelope)
    result = data if result_path == "" else data.get(result_path)
    if not result:
        # None, {}, or [] → no rows (result: null / 404 / empty).
        return []
    if isinstance(result, list):
        return result
    # Single-object endpoints return result as one object → one row.
    return [result]


def paginate(
    auth: CloudflareAuth,
    path: str,
    query: dict[str, Any],
    desc: ResourceDescriptor,
) -> Iterator[list[dict[str, Any]]]:
    """Yield one list of result items per page, following the descriptor's paging style."""
    if desc.pagination is Pagination.NONE:
        yield _result_items(_get(auth, path, query), desc.result_path)
        return

    if desc.pagination is Pagination.PAGE:
        page = 1
        while True:
            data = _get(auth, path, {**query, "page": page, "per_page": desc.per_page})
            items = _result_items(data, desc.result_path)
            yield items
            info = (data.get("result_info") if isinstance(data, dict) else None) or {}
            total_pages = info.get("total_pages")
            if not items or not total_pages or page >= total_pages:
                return
            page += 1
        return

    if desc.pagination is Pagination.CURSOR:
        cursor: str | None = None
        while True:
            q = {**query, "per_page": desc.per_page}
            if cursor:
                q["cursor"] = cursor
            data = _get(auth, path, q)
            items = _result_items(data, desc.result_path)
            yield items
            info = (data.get("result_info") if isinstance(data, dict) else None) or {}
            cursor = info.get("cursor")
            if not items or not cursor:
                return
        return

    raise CloudflareError(f"Unknown pagination style: {desc.pagination!r}")
