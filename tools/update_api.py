"""Refresh Cloudflare's pinned OpenAPI spec, catalog, and review report.

Run ``python -m tools.update_api`` from the repository root. Downloads and
generation finish before any tracked file is replaced. No Cloudflare token is
needed; GITHUB_TOKEN, when supplied, is used only for GitHub's commit lookup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from tools.generate_resources import ROOT, Spec, generate_manifest

REPOSITORY = "cloudflare/api-schemas"
MANIFEST = Path("vgi_cloudflare/resources_generated.json")
LOCK = Path("cloudflare-api.lock.json")
REPORT = Path("spec/api-update.md")
REPORT_JSON = Path("spec/api-update.json")


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def encoded(value: Any, *, indent: int = 2) -> bytes:
    return json.dumps(value, indent=indent, sort_keys=True).encode()


def resource_name(resource: dict[str, Any]) -> str:
    return f"{resource['schema']}.{resource['name']}"


def describe_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Describe SQL interface and runtime changes separately from documentation."""
    changes = []
    defaults: dict[str, Any] = {"per_page": 100, "result_path": "result", "default_query": {}}
    for key in ("schema", "name", "kind", "pagination", "per_page", "result_path", "default_query"):
        old, new = before.get(key, defaults.get(key)), after.get(key, defaults.get(key))
        if old != new:
            changes.append(f"{key}: {old!r} → {new!r}")
    for key in ("columns", "path_params", "query_params"):
        old_fields = {f["name"]: f for f in before.get(key, [])}
        new_fields = {f["name"]: f for f in after.get(key, [])}
        for name in sorted(old_fields.keys() - new_fields.keys()):
            changes.append(f"{key}.{name}: removed")
        for name in sorted(new_fields.keys() - old_fields.keys()):
            changes.append(f"{key}.{name}: added ({new_fields[name].get('type', 'string')})")
        for name in sorted(old_fields.keys() & new_fields.keys()):
            old, new = old_fields[name], new_fields[name]
            for attribute in sorted((old.keys() | new.keys()) - {"doc"}):
                if old.get(attribute) != new.get(attribute):
                    changes.append(
                        f"{key}.{name}.{attribute}: {old.get(attribute)!r} → {new.get(attribute)!r}"
                    )
        # Order matters for positional path arguments and SELECT * result columns.
        if (
            key != "query_params"
            and old_fields.keys() == new_fields.keys()
            and list(old_fields) != list(new_fields)
        ):
            changes.append(f"{key}: order changed from {list(old_fields)!r} to {list(new_fields)!r}")
    return changes


def compare(previous: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    old = {r["path"]: r for r in previous["resources"]}
    new = {r["path"]: r for r in candidate["resources"]}

    def endpoint(resource: dict[str, Any]) -> dict[str, str]:
        return {"name": resource_name(resource), "path": resource["path"]}

    changed = []
    for path in sorted(old.keys() & new.keys()):
        if old[path] != new[path]:
            changed.append({**endpoint(new[path]), "changes": describe_changes(old[path], new[path])})
    old_names = {resource_name(r): r["path"] for r in old.values()}
    repointed = [
        {**endpoint(r), "previous_path": old_names[resource_name(r)]}
        for r in new.values()
        if resource_name(r) in old_names and old_names[resource_name(r)] != r["path"]
    ]
    return {
        "added": [endpoint(new[p]) for p in sorted(new.keys() - old.keys())],
        "removed": [endpoint(old[p]) for p in sorted(old.keys() - new.keys())],
        "changed": changed,
        "repointed": repointed,
    }


def markdown_report(report: dict[str, Any]) -> str:
    source = report["source"]
    delta = report["changes"]
    behavioral = [r for r in delta["changed"] if r["changes"]]
    lines = [
        "# Cloudflare API update",
        "",
        f"Upstream: [{source['commit'][:12]}](https://github.com/{REPOSITORY}/commit/{source['commit']})",
        f"Spec SHA-256: `{source['sha256']}`",
        f"Previous upstream commit: `{report['previous_commit'] or 'not recorded'}`",
        "",
        f"Resources: {report['previous_count']} → {report['resource_count']}. "
        f"Added: {len(delta['added'])}; removed: {len(delta['removed'])}; "
        f"interface/runtime changes: {len(behavioral)}; "
        f"documentation/metadata-only changes: {len(delta['changed']) - len(behavioral)}.",
        f"GET paths skipped: {len(source['skipped_get_paths'])}. "
        f"Unmapped product areas: {', '.join(report['unmapped_areas']) or 'none'}.",
        "",
        "This compares generated descriptors. Hand-written overrides in resources.py/items.py "
        "and GraphQL DATASETS need separate review and live validation.",
        "Full details are in the api-update-report workflow artifact (or spec/api-update.json locally).",
    ]

    def section(title: str, entries: list[str]) -> None:
        lines.extend(["", f"## {title}", ""])
        lines.extend(f"- {entry}" for entry in entries[:30])
        if not entries:
            lines.append("None.")
        if len(entries) > 30:
            lines.append(f"- … {len(entries) - 30} more; see the JSON report.")

    def label(resource: dict[str, Any]) -> str:
        return f"`{resource['name']}` — `{resource['path']}`"

    section("Added endpoints", [label(r) for r in delta["added"]])
    section("Removed endpoints — review compatibility", [label(r) for r in delta["removed"]])
    section(
        "Changed interfaces or behavior — review compatibility",
        [f"{label(r)}: {'; '.join(r['changes'])[:900]}" for r in behavioral],
    )
    section(
        "SQL names pointing to a different endpoint — review compatibility",
        [f"{label(r)} (previously `{r['previous_path']}`)" for r in delta["repointed"]],
    )
    title = "Newly skipped GET paths" if report["previous_commit"] else "Skipped GET paths (initial baseline)"
    section(title, [f"`{path}`" for path in report["newly_skipped_get_paths"]])
    lines.extend(
        [
            "",
            "## Review and validation",
            "",
            "The scheduled workflow runs lint, offline tests, entry-point smoke checks, and structural "
            "catalog validation before creating/updating a PR. Check the workflow run for results.",
            "Failed validation produces a draft PR and a failed workflow run "
            "so generator fixes can be reviewed.",
            "Review removals, field types, positional arguments, pagination, skipped endpoints, "
            "and new product areas before merging. This workflow does not merge or deploy changes.",
        ]
    )
    return "\n".join(lines) + "\n"


def refresh(
    root: Path,
    client: httpx.Client,
    *,
    revision: str | None = None,
    locked: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    previous = json.loads((root / MANIFEST).read_bytes())
    old_lock = json.loads((root / LOCK).read_bytes()) if (root / LOCK).exists() else {}
    if locked:
        if not old_lock:
            raise ValueError("No cloudflare-api.lock.json; run make update-api first.")
        revision = old_lock["commit"]
    if revision is None:
        # Resolve the last spec change, not unrelated commits on main.
        token = os.environ.get("GITHUB_TOKEN")
        response = client.get(
            f"https://api.github.com/repos/{REPOSITORY}/commits",
            params={"path": "openapi.json", "per_page": 1},
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        response.raise_for_status()
        revision = response.json()[0]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Revision must be a full, lowercase 40-character commit SHA.")
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{revision}/openapi.json"
    response = client.get(url)
    response.raise_for_status()
    content = response.content
    digest = hashlib.sha256(content).hexdigest()
    if locked and digest != old_lock["sha256"]:
        raise ValueError("Downloaded spec does not match the recorded SHA-256 checksum.")
    data = json.loads(content)
    if not isinstance(data, dict) or not str(data.get("openapi", "")).startswith("3."):
        raise ValueError("Expected an OpenAPI 3 document.")
    if not isinstance(data.get("paths"), dict) or not data["paths"]:
        raise ValueError("Downloaded spec has no paths.")
    manifest, skipped = generate_manifest(Spec(data), previous)
    if not manifest["resources"]:
        raise ValueError("Generation produced no resources; existing catalog was not replaced.")
    source = {
        "repository": REPOSITORY,
        "commit": revision,
        "url": url,
        "sha256": digest,
        "skipped_get_paths": skipped,
    }
    report = {
        "source": source,
        "previous_commit": old_lock.get("commit"),
        "previous_count": len(previous["resources"]),
        "resource_count": len(manifest["resources"]),
        "changes": compare(previous, manifest),
        "unmapped_areas": sorted({r["area"] for r in manifest["resources"] if r["schema"] == "misc"}),
        "newly_skipped_get_paths": sorted(set(skipped) - set(old_lock.get("skipped_get_paths", []))),
    }
    # Finish serialization as well as generation before replacing tracked files.
    manifest_bytes = encoded(manifest, indent=1)
    lock_bytes = encoded(source) + b"\n"
    atomic_write(root / REPORT_JSON, encoded(report) + b"\n")
    atomic_write(root / REPORT, markdown_report(report).encode())
    if not dry_run:
        atomic_write(root / "spec/openapi.json", content)
        atomic_write(root / MANIFEST, manifest_bytes)
        atomic_write(root / LOCK, lock_bytes)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    revision = parser.add_mutually_exclusive_group()
    revision.add_argument("--revision", help="Use a full upstream commit SHA instead of the latest spec")
    revision.add_argument("--locked", action="store_true", help="Use and verify the recorded commit/checksum")
    parser.add_argument(
        "--dry-run", action="store_true", help="Write reports only; leave catalog/spec/lock intact"
    )
    args = parser.parse_args()
    try:
        with httpx.Client(
            timeout=60, follow_redirects=True, headers={"User-Agent": "vgi-cloudflare-codegen"}
        ) as client:
            report = refresh(ROOT, client, revision=args.revision, locked=args.locked, dry_run=args.dry_run)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, OSError) as exc:
        print(f"API update failed: {exc}", file=sys.stderr)
        return 1
    changes = report["changes"]
    print(
        f"Cloudflare {report['source']['commit'][:12]}: {report['resource_count']} resources; "
        f"{len(changes['added'])} added, {len(changes['removed'])} removed, "
        f"{len(changes['changed'])} changed."
    )
    print(
        f"Review {REPORT} and {REPORT_JSON}."
        + (" Dry run: catalog/spec/lock unchanged." if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
