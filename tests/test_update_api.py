"""Maintenance must preserve published names, surface drift, and survive bad downloads."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from tools.generate_resources import Spec, generate_manifest
from tools.update_api import LOCK, MANIFEST, REPORT, REPORT_JSON, compare, refresh

COMMIT = "a" * 40


def spec_document() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"version": "4.0.0"},
        "paths": {
            "/accounts": {
                "get": {
                    "summary": "List accounts",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "result": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {"id": {"type": "string"}},
                                                },
                                            }
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/unsupported": {"get": {"responses": {"200": {"description": "Unstructured"}}}},
        },
    }


@pytest.fixture
def baseline(tmp_path: Path) -> Path:
    manifest, _ = generate_manifest(Spec(spec_document()), {})
    # A published name must survive even when it differs from today's name derivation.
    manifest["resources"][0]["name"] = "published_accounts"
    (tmp_path / MANIFEST).parent.mkdir(parents=True)
    (tmp_path / MANIFEST).write_text(json.dumps(manifest))
    return tmp_path


def mock_client(content: bytes, requests: list[httpx.Request]) -> httpx.Client:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=[{"sha": COMMIT}])
        return httpx.Response(200, content=content)

    return httpx.Client(transport=httpx.MockTransport(respond))


def test_refresh_pins_download_preserves_names_and_is_repeatable(baseline: Path, monkeypatch) -> None:
    content = json.dumps(spec_document()).encode()
    requests: list[httpx.Request] = []
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    with mock_client(content, requests) as client:
        report = refresh(baseline, client)
        tracked = {path: (baseline / path).read_bytes() for path in (MANIFEST, LOCK)}
        refresh(baseline, client, locked=True)

    assert all((baseline / path).read_bytes() == value for path, value in tracked.items())
    manifest = json.loads(tracked[MANIFEST])
    assert manifest["resources"][0]["name"] == "published_accounts"
    lock = json.loads(tracked[LOCK])
    assert lock["commit"] == COMMIT
    assert lock["sha256"] == hashlib.sha256(content).hexdigest()
    assert lock["skipped_get_paths"] == ["/unsupported"]
    assert (baseline / "spec/openapi.json").read_bytes() == content
    assert report["changes"] == {"added": [], "removed": [], "changed": [], "repointed": []}
    assert requests[0].url.params["path"] == "openapi.json"
    assert requests[0].headers["Authorization"] == "Bearer test-github-token"
    assert len(requests) == 3  # Locked replay does not resolve main again.
    for request in requests[1:]:
        assert f"/{COMMIT}/openapi.json" in str(request.url)
        assert "Authorization" not in request.headers


def test_preview_writes_reports_without_changing_spec_manifest_or_lock(baseline: Path) -> None:
    original = (baseline / MANIFEST).read_bytes()
    (baseline / LOCK).write_text('{"commit": "old", "skipped_get_paths": []}')
    (baseline / "spec").mkdir()
    (baseline / "spec/openapi.json").write_bytes(b"old spec")
    lock = (baseline / LOCK).read_bytes()
    with mock_client(json.dumps(spec_document()).encode(), []) as client:
        refresh(baseline, client, dry_run=True)
    assert (baseline / MANIFEST).read_bytes() == original
    assert (baseline / LOCK).read_bytes() == lock
    assert (baseline / "spec/openapi.json").read_bytes() == b"old spec"
    assert "GET paths skipped: 1" in (baseline / REPORT).read_text()
    report = json.loads((baseline / REPORT_JSON).read_bytes())
    assert report["newly_skipped_get_paths"] == ["/unsupported"]


@pytest.mark.parametrize(
    "content",
    [
        b"<html>upstream failure</html>",
        b'{"openapi":"3.0.3","paths":{}}',
        b'{"openapi":"3.0.3","paths":{"/bad":{"get":{"responses":{}}}}}',
    ],
)
def test_invalid_download_never_replaces_catalog(baseline: Path, content: bytes) -> None:
    original = (baseline / MANIFEST).read_bytes()
    with mock_client(content, []) as client, pytest.raises(ValueError):
        refresh(baseline, client)
    assert (baseline / MANIFEST).read_bytes() == original
    assert not (baseline / LOCK).exists()
    assert not (baseline / "spec/openapi.json").exists()


def test_http_failure_never_replaces_catalog(baseline: Path) -> None:
    original = (baseline / MANIFEST).read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            refresh(baseline, client, revision=COMMIT)
    assert (baseline / MANIFEST).read_bytes() == original
    assert not (baseline / LOCK).exists()


def test_locked_checksum_mismatch_leaves_existing_files_intact(baseline: Path) -> None:
    with mock_client(json.dumps(spec_document()).encode(), []) as client:
        refresh(baseline, client)
    tracked = {path: (baseline / path).read_bytes() for path in (MANIFEST, LOCK, Path("spec/openapi.json"))}
    with mock_client(b"different download", []) as client, pytest.raises(ValueError, match="checksum"):
        refresh(baseline, client, locked=True)
    assert all((baseline / path).read_bytes() == value for path, value in tracked.items())


def test_report_separates_documentation_from_compatibility_changes() -> None:
    original, _ = generate_manifest(Spec(spec_document()), {})
    resource = original["resources"][0]
    resource["columns"].append({"name": "removed_field", "type": "string"})
    resource["path_params"] = [{"name": "account_id"}, {"name": "id"}]
    resource["query_params"] = [{"name": "limit_", "type": "int64", "le": 100}]
    resource["pagination"] = "page"
    new = copy.deepcopy(original)
    changed = new["resources"][0]
    changed["columns"] = [{"name": "id", "type": "int64"}, {"name": "new_field", "type": "bool"}]
    changed["path_params"].reverse()
    changed["query_params"][0]["le"] = 20
    changed["pagination"] = "cursor"
    details = "\n".join(compare(original, new)["changed"][0]["changes"])
    assert "columns.id.type: 'string' → 'int64'" in details
    assert "columns.removed_field: removed" in details
    assert "columns.new_field: added" in details
    assert "path_params: order changed" in details
    assert "query_params.limit_.le: 100 → 20" in details
    assert "pagination: 'page' → 'cursor'" in details

    docs_only = copy.deepcopy(original)
    docs_only["resources"][0]["doc"] = "Updated documentation."
    docs_only["resources"][0]["columns"][0]["doc"] = "Updated field documentation."
    delta = compare(original, docs_only)
    assert len(delta["changed"]) == 1
    assert delta["changed"][0]["changes"] == []


def test_report_flags_endpoint_removal_addition_and_name_reuse() -> None:
    original, _ = generate_manifest(Spec(spec_document()), {})
    replacement = copy.deepcopy(original)
    replacement["resources"][0]["path"] = "/other/accounts"
    delta = compare(original, replacement)
    assert delta["removed"][0]["path"] == "/accounts"
    assert delta["added"][0]["path"] == "/other/accounts"
    assert delta["repointed"][0]["previous_path"] == "/accounts"


def test_new_colliding_endpoint_cannot_steal_a_published_name() -> None:
    spec = spec_document()
    old, _ = generate_manifest(Spec(spec), {})
    spec["paths"]["/organizations/{organization_id}/accounts"] = copy.deepcopy(spec["paths"]["/accounts"])
    new, _ = generate_manifest(Spec(spec), old)
    by_path = {r["path"]: r for r in new["resources"]}
    assert by_path["/accounts"]["name"] == old["resources"][0]["name"]
    assert by_path["/organizations/{organization_id}/accounts"]["name"] != by_path["/accounts"]["name"]
