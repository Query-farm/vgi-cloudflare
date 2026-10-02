#!/usr/bin/env python
"""Generate Cloudflare resource descriptors from the OpenAPI spec.

Reads spec/openapi.json and emits a compact JSON manifest to
vgi_cloudflare/resources_generated.json. Two resource shapes are produced:

  * list tables  — GET endpoints whose ``result`` is an array
  * item funcs   — get-one-by-id GETs whose ``result`` is a single object
                   (or a "wrapperless" body), exposed as singular-noun lookup functions

Each resource is assigned a schema (product family) via AREA_TO_SCHEMA, and a
name unique within that schema (shortened to drop the redundant schema prefix).
The runtime loader (generated.py) turns the manifest into VGI table functions
via the same descriptor pipeline as the hand-written resources.

Usage:
    python tools/generate_resources.py            # print stats only (dry run)
    python tools/generate_resources.py --emit     # write the JSON manifest
"""

from __future__ import annotations

import json
import keyword
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "spec" / "openapi.json"
OUT_PATH = ROOT / "vgi_cloudflare" / "resources_generated.json"

# Query params that control paging/sorting, not filtering — never become columns.
CONTROL_PARAMS = {
    "page",
    "per_page",
    "cursor",
    "order",
    "direction",
    "match",
    "tag_match",
    "search",
    "offset",
    "limit",
}
# Dotted query-param suffixes that are operator variants of a base filter
# (name.exact, comment.contains, ...) — skip; the base param already covers it.
OPERATOR_SUFFIXES = {
    "exact",
    "contains",
    "startswith",
    "endswith",
    "present",
    "absent",
    "geo",
}

MAX_COLUMNS = 64
MAX_DEPTH = 10


class Spec:
    """OpenAPI spec with a cached $ref resolver and schema-flattening helpers."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec

    def resolve(self, ref: str) -> dict[str, Any]:
        cur: Any = self.spec
        for part in ref.lstrip("#/").split("/"):
            cur = cur[part]
        return cur

    def deref(self, sch: dict[str, Any], seen: frozenset[str], depth: int) -> dict[str, Any] | None:
        if depth > MAX_DEPTH or not isinstance(sch, dict):
            return None
        if "$ref" in sch:
            ref = sch["$ref"]
            if ref in seen:
                return None
            return self.deref(self.resolve(ref), seen | {ref}, depth + 1)
        return sch

    def describe(self, sch: Any, seen: frozenset[str] = frozenset(), depth: int = 0) -> str:
        """Best documentation for a property/parameter schema.

        Follows ``$ref`` / ``allOf`` / ``oneOf`` / ``anyOf`` / array ``items`` to the
        first ``description`` (shared components carry it, not the referencing
        property), then adds enum values or an example when they say something
        the description doesn't. Empty when the spec documents nothing.
        """
        text = " ".join((self._first_description(sch, seen, depth) or "").split())
        text = re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)  # "Certificate.." -> "Certificate."
        if text and text[-1] not in ".!?:)`":
            text += "."
        hints = self._hints(sch, seen, depth)
        enum = hints.get("enum")
        if enum and not any(str(v) in text for v in enum[:3]):
            shown = ", ".join(f"`{v}`" for v in enum[:12]) + (", …" if len(enum) > 12 else "")
            text = f"{text} One of: {shown}." if text else f"One of: {shown}."
        elif not text and hints.get("example") not in (None, "", [], {}):
            ex = hints["example"]
            if not isinstance(ex, (dict, list)):
                text = f"Example: `{ex}`."
        return " ".join(text.split())

    def _first_description(self, sch: Any, seen: frozenset[str], depth: int) -> str | None:
        if depth > MAX_DEPTH or not isinstance(sch, dict):
            return None
        if sch.get("description"):
            return str(sch["description"])
        if "$ref" in sch and sch["$ref"] not in seen:
            return self._first_description(self.resolve(sch["$ref"]), seen | {sch["$ref"]}, depth + 1)
        for key in ("allOf", "oneOf", "anyOf"):
            for sub in sch.get(key, []):
                d = self._first_description(sub, seen, depth + 1)
                if d:
                    return d
        if sch.get("type") == "array" and "items" in sch:
            return self._first_description(sch["items"], seen, depth + 1)
        return None

    def _hints(self, sch: Any, seen: frozenset[str], depth: int) -> dict[str, Any]:
        if depth > MAX_DEPTH or not isinstance(sch, dict):
            return {}
        if "enum" in sch or "example" in sch:
            return {k: sch[k] for k in ("enum", "example") if k in sch}
        if "$ref" in sch and sch["$ref"] not in seen:
            return self._hints(self.resolve(sch["$ref"]), seen | {sch["$ref"]}, depth + 1)
        for key in ("allOf", "oneOf", "anyOf"):
            for sub in sch.get(key, []):
                h = self._hints(sub, seen, depth + 1)
                if h:
                    return h
        return {}

    def leaf_type(
        self, sch: dict[str, Any], seen: frozenset[str] = frozenset(), depth: int = 0
    ) -> tuple[str | None, str | None]:
        """Resolve a property schema to (openapi_type, format), following refs/allOf/oneOf."""
        sch = self.deref(sch, seen, depth) or {}
        if "type" in sch:
            return sch["type"], sch.get("format")
        if "enum" in sch:
            return "string", None
        for key in ("allOf", "oneOf", "anyOf"):
            for sub in sch.get(key, []):
                t, f = self.leaf_type(sub, seen, depth + 1)
                if t:
                    return t, f
        if "properties" in sch:
            return "object", None
        return None, None

    def flatten_props(
        self,
        sch: dict[str, Any],
        seen: frozenset[str] = frozenset(),
        acc: dict[str, dict[str, Any]] | None = None,
        depth: int = 0,
    ) -> dict[str, dict[str, Any]]:
        """Union of top-level properties across refs/allOf/oneOf/anyOf composition."""
        if acc is None:
            acc = {}
        if depth > MAX_DEPTH or not isinstance(sch, dict):
            return acc
        if "$ref" in sch:
            ref = sch["$ref"]
            if ref in seen:
                return acc
            return self.flatten_props(self.resolve(ref), seen | {ref}, acc, depth + 1)
        for key in ("allOf", "oneOf", "anyOf"):
            for sub in sch.get(key, []):
                self.flatten_props(sub, seen, acc, depth + 1)
        for name, prop in sch.get("properties", {}).items():
            acc.setdefault(name, prop)
        return acc

    def result_items(
        self, sch: dict[str, Any], seen: frozenset[str] = frozenset(), depth: int = 0
    ) -> dict[str, Any] | None:
        """Return the schema of one item in the ``result`` array, or None if not a list."""
        if depth > MAX_DEPTH or not isinstance(sch, dict):
            return None
        if "$ref" in sch:
            ref = sch["$ref"]
            if ref in seen:
                return None
            return self.result_items(self.resolve(ref), seen | {ref}, depth + 1)
        for key in ("allOf", "oneOf", "anyOf"):
            for sub in sch.get(key, []):
                items = self.result_items(sub, seen, depth + 1)
                if items is not None:
                    return items
        result = sch.get("properties", {}).get("result")
        if result is None:
            return None
        if "$ref" in result:
            result = self.resolve(result["$ref"])
        if result.get("type") == "array":
            return result.get("items", {})
        return None


def arrow_type(otype: str | None, fmt: str | None) -> str:
    if otype in ("object", "array"):
        return "string"  # JSON-encoded
    if otype == "integer":
        return "int64"
    if otype == "number":
        return "float64"
    if otype == "boolean":
        return "bool"
    if otype == "string" and fmt in ("date-time", "date"):
        return "timestamp"
    return "string"


def sanitize(name: str) -> str:
    return name.replace(".", "_").replace("-", "_")


_PLACEHOLDER_DOCS = {
    "",
    "identifier",
    "uuid",
    "id",
    "the identifier",
    "unique identifier",
    "identifier of the",
}
_SCOPE_SOURCES = {
    "zone_id": "Zone id: the id column of cloudflare.zones.zones.",
    "account_id": "Account id: the id column of cloudflare.accounts.accounts.",
}


def path_param(spec: Spec, raw_name: str, pr: dict[str, Any] | None) -> dict[str, Any]:
    """A path parameter: its doc (made specific when the spec's is a placeholder) and choices."""
    name = sanitize(raw_name)
    doc = (spec.describe(pr) or spec.describe(pr.get("schema"))) if pr else ""
    if name in _SCOPE_SOURCES:
        doc = _SCOPE_SOURCES[name]  # says where valid ids come from, unlike the spec's "Account ID."
    elif re.sub(r"[^a-z ]+", "", doc.lower()).strip() in _PLACEHOLDER_DOCS or re.sub(
        r"[^a-z0-9]", "", doc.lower()
    ) == re.sub(r"[^a-z0-9]", "", name.lower()):
        noun = name
        for suf in ("_identifier", "_id", "_tag", "_name"):
            noun = noun.removesuffix(suf)
        doc = _SCOPE_SOURCES.get(name) or f"Identifier of the {noun.replace('_', ' ')}."
    out: dict[str, Any] = {"name": name, "doc": truncate(doc)}
    enum = spec._hints(pr.get("schema") if pr else None, frozenset(), 0).get("enum") if pr else None
    if enum and all(isinstance(v, str) for v in enum):
        out["choices"] = list(enum)
    schema = spec.deref(pr.get("schema") or {}, frozenset(), 0) if pr else None
    if schema and isinstance(schema.get("pattern"), str):
        out["pattern"] = schema["pattern"]
    return out


def column_doc(spec: Spec, prop: Any, col: str, type_name: str) -> str:
    """A column's doc from the spec — dropped when it only restates the column name."""
    doc = spec.describe(prop)
    if re.sub(r"[^a-z0-9]", "", doc.lower()) == re.sub(r"[^a-z0-9]", "", col.lower()):
        return ""
    if type_name == "bool":
        # Dashboard checkbox wording ("Select to prevent ...") -> what the boolean means.
        doc = re.sub(r"^Select (to|for) ", r"True \1 ", doc)
    return truncate(doc, 400)


#: Query parameters a lookup never exposes: paging, and ``format`` (CSV would not parse).
_ITEM_SKIP_QUERY = CONTROL_PARAMS | {"format"}


def item_query_params(spec: Spec, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Optional query parameters of a get endpoint, exposed as named function arguments."""
    out = []
    for pr in params:
        nm = pr.get("name")
        if pr.get("in") != "query" or not nm or nm in _ITEM_SKIP_QUERY:
            continue
        if "." in nm and nm.rsplit(".", 1)[1] in OPERATOR_SUFFIXES:
            continue
        arg = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", sanitize(nm)).lower()
        if keyword.iskeyword(arg) or not arg.isidentifier():
            arg += "_"
        schema = spec.deref(pr.get("schema") or {}, frozenset(), 0) or {}
        is_array = schema.get("type") == "array"
        leaf = (spec.deref(schema.get("items") or {}, frozenset(), 0) or {}) if is_array else schema
        ot, _fmt = spec.leaf_type(leaf)
        q: dict[str, Any] = {
            "name": arg,
            "type": "int64"
            if ot == "integer" and not is_array
            else "float64"
            if ot == "number" and not is_array
            else "string",
            "doc": truncate(spec.describe(pr) or spec.describe(pr.get("schema")), 300),
            "api_name": nm if arg != nm else None,
        }
        # Constraints the spec declares become machine-readable argument constraints. An
        # enumerated array parameter takes one value per call.
        enum = spec._hints(leaf, frozenset(), 0).get("enum")
        if enum and all(isinstance(v, str) for v in enum):
            q["choices"] = list(enum)
        if q["type"] != "string":
            for key, bound in (("ge", "minimum"), ("le", "maximum")):
                if isinstance(leaf.get(bound), (int, float)):
                    q[key] = leaf[bound]
        if isinstance(leaf.get("pattern"), str):
            q["pattern"] = leaf["pattern"]
        out.append(q)
    return out


def operation_doc(op: dict[str, Any]) -> str:
    """The operation's long-form ``description`` (Markdown kept), when it adds to the summary."""
    text = (op.get("description") or "").strip()
    if not text or text == (op.get("summary") or "").strip():
        return ""
    return text if len(text) <= 2000 else text[:1999] + "…"


def truncate(text: str | None, n: int = 200) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text[: n - 1] + "…" if len(text) > n else text


def derive_segments(path: str) -> list[str]:
    return [sanitize(s) for s in path.strip("/").split("/") if not (s.startswith("{") and s.endswith("}"))]


def detect_pagination(param_names: set[str]) -> str:
    if "cursor" in param_names:
        return "cursor"
    if "page" in param_names:
        return "page"
    return "none"


# Curated mapping of OpenAPI product areas (the path segment after the zone/account
# id) into ~20 schema families. Unmapped areas fall through to "misc".
AREA_TO_SCHEMA: dict[str, str] = {
    # DNS
    "dns_records": "dns",
    "dns_settings": "dns",
    "dns_firewall": "dns",
    "dnssec": "dns",
    "secondary_dns": "dns",
    "dns_analytics": "dns",
    "custom_ns": "dns",
    # Zones & zone-level config
    "zones": "zones",
    "settings": "zones",
    "custom_hostnames": "zones",
    "hostnames": "zones",
    "pagerules": "zones",
    "rules": "zones",
    "cache": "zones",
    "argo": "zones",
    "origin": "zones",
    "custom_pages": "zones",
    "page_shield": "zones",
    "waiting_rooms": "zones",
    "flagship": "zones",
    "cloud_connector": "zones",
    "speed_api": "zones",
    "rum": "zones",
    "analytics": "zones",
    "diagnostics": "zones",
    "spectrum": "zones",
    # Accounts, org, IAM
    "accounts": "accounts",
    "members": "accounts",
    "roles": "accounts",
    "iam": "accounts",
    "tokens": "accounts",
    "invites": "accounts",
    "memberships": "accounts",
    "user": "accounts",
    "organizations": "accounts",
    "tenants": "accounts",
    "sso_connectors": "accounts",
    "oauth_clients": "accounts",
    "oauth": "accounts",
    "tags": "accounts",
    "profile": "accounts",
    "system": "accounts",
    "resource-library": "accounts",
    "subscriptions": "accounts",
    # SSL / TLS / certificates
    "ssl": "ssl",
    "certificates": "ssl",
    "certificate_authorities": "ssl",
    "ct": "ssl",
    "custom_certificates": "ssl",
    "client_certificates": "ssl",
    "keyless_certificates": "ssl",
    "mtls_certificates": "ssl",
    "origin_tls_client_auth": "ssl",
    "custom_csrs": "ssl",
    "acm": "ssl",
    "dcv_delegation": "ssl",
    # Zero Trust Access
    "access": "access",
    # Zero Trust gateway / WARP / device posture
    "gateway": "zero_trust",
    "dlp": "zero_trust",
    "dex": "zero_trust",
    "devices": "zero_trust",
    "dls": "zero_trust",
    "zt_risk_scoring": "zero_trust",
    "connectivity": "zero_trust",
    "warp_connector": "zero_trust",
    "cfd_tunnel": "zero_trust",
    "infrastructure": "zero_trust",
    # Threat intelligence / security products
    "cloudforce-one": "security",
    "intel": "security",
    "security-center": "security",
    "vuln_scanner": "security",
    "brand-protection": "security",
    "abuse-reports": "security",
    "bot_management": "security",
    "fraud_detection": "security",
    "botnet_feed": "security",
    "urlscanner": "security",
    "pay-per-crawl": "security",
    "challenges": "security",
    # WAF / firewall
    "firewall": "waf",
    # Workers platform
    "workers": "workers",
    "workflows": "workers",
    "pipelines": "workers",
    "queues": "workers",
    "builds": "workers",
    "containers": "workers",
    "secrets_store": "workers",
    "environments": "workers",
    "slurper": "workers",
    # AI
    "ai": "ai",
    "ai-gateway": "ai",
    "ai-search": "ai",
    "autorag": "ai",
    "vectorize": "ai",
    # Storage / data
    "r2": "storage",
    "r2-catalog": "storage",
    "d1": "storage",
    "hyperdrive": "storage",
    "storage": "storage",
    # Magic / network
    "magic": "magic",
    "addressing": "magic",
    "cni": "magic",
    "ips": "magic",
    # Email
    "email": "email",
    "email-security": "email",
    # Media / realtime
    "stream": "stream",
    "calls": "stream",
    "realtime": "stream",
    "moq": "stream",
    # Load balancing
    "load_balancers": "load_balancing",
    "load_balancing_analytics": "load_balancing",
    # Radar
    "radar": "radar",
    # Logs
    "logs": "logs",
    "logpush": "logs",
    "audit_logs": "logs",
    # Alerting
    "alerting": "alerting",
    "event_subscriptions": "alerting",
    # Billing
    "billing": "billing",
    "billable": "billing",
    "paygo-usage": "billing",
    # Standalone families
    "api_gateway": "api_gateway",
    "pages": "pages",
    "registrar": "registrar",
    "browser-rendering": "browser_rendering",
    "stream_live": "stream",
}

# Areas arrive sanitized (``-`` → ``_``), so normalize the mapping keys to match.
AREA_TO_SCHEMA = {sanitize(k): v for k, v in AREA_TO_SCHEMA.items()}

_GROUPING_PARENTS = ("zones", "accounts")


def classify(path: str) -> tuple[str, str, int, list[str]]:
    """Return (schema, area, area_index, non_param_segments) for an endpoint path."""
    raw = path.strip("/").split("/")
    nonparam = [sanitize(s) for s in raw if not (s.startswith("{") and s.endswith("}"))]
    if not nonparam:
        return "misc", "misc", 0, []
    area_idx = 1 if (nonparam[0] in _GROUPING_PARENTS and len(nonparam) > 1) else 0
    area = nonparam[area_idx]
    return AREA_TO_SCHEMA.get(area, "misc"), area, area_idx, nonparam


def shorten(name: str, schema: str) -> str:
    """Drop a redundant leading ``<schema>_``."""
    if name.startswith(schema + "_") and len(name) > len(schema) + 1:
        return name[len(schema) + 1 :]
    return name


def with_path_item_params(spec: Spec, path_item: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    """Merge path-item-level ``parameters`` into the operation's (OpenAPI 3 inheritance).

    The spec declares many shared params (``{account_id}``, paging) once on the path
    item; an operation-level param with the same ``(in, name)`` overrides it.
    """
    inherited = path_item.get("parameters") or []
    if not inherited:
        return op

    def key(pr: dict[str, Any]) -> tuple[str, str]:
        resolved = spec.resolve(pr["$ref"]) if "$ref" in pr else pr
        return (resolved.get("in", ""), resolved.get("name", ""))

    own = op.get("parameters") or []
    own_keys = {key(pr) for pr in own}
    return {**op, "parameters": [pr for pr in inherited if key(pr) not in own_keys] + list(own)}


def build_descriptor(spec: Spec, path: str, op: dict[str, Any]) -> dict[str, Any] | None:
    resp = op.get("responses", {}).get("200", {})
    schema = resp.get("content", {}).get("application/json", {}).get("schema")
    if not schema:
        return None
    item = spec.result_items(schema)
    if item is None:
        return None  # not a list endpoint

    # Parameters
    raw_path_params: dict[str, dict[str, Any]] = {}
    query_params: list[dict[str, Any]] = []
    raw_param_names: set[str] = set()
    for pr in op.get("parameters", []):
        if "$ref" in pr:
            pr = spec.resolve(pr["$ref"])
        loc, nm = pr.get("in"), pr.get("name")
        if not nm:
            continue
        raw_param_names.add(nm)
        if loc == "path":
            raw_path_params[nm] = pr
        elif loc == "query":
            if nm in CONTROL_PARAMS:
                continue
            if "." in nm and nm.rsplit(".", 1)[1] in OPERATOR_SUFFIXES:
                continue
            ot, fmt = spec.leaf_type(pr.get("schema", {}))
            query_params.append(
                {
                    "name": sanitize(nm),
                    "type": arrow_type(ot, fmt),
                    "doc": truncate(spec.describe(pr) or spec.describe(pr.get("schema")), 300),
                    "api_name": nm if sanitize(nm) != nm else None,
                }
            )

    # Path params in URL order; every {placeholder} must be one, documented or not.
    path_params = [path_param(spec, p, raw_path_params.get(p)) for p in _URL_PARAM_RE.findall(path)]

    # Columns from the flattened item schema
    path_param_names = {p["name"] for p in path_params}
    columns: list[dict[str, Any]] = []
    seen_cols: set[str] = set()
    for name, prop in spec.flatten_props(item).items():
        col = sanitize(name)
        if col in seen_cols or col in path_param_names:
            continue
        ot, fmt = spec.leaf_type(prop)
        columns.append(
            {
                "name": col,
                "type": arrow_type(ot, fmt),
                "doc": column_doc(spec, prop, col, arrow_type(ot, fmt)),
                "source": name if name != col else None,
            }
        )
        seen_cols.add(col)
        if len(columns) >= MAX_COLUMNS:
            break

    if not columns:
        return None

    schema, area, area_idx, nonparam = classify(path)
    tail = nonparam[area_idx + 1 :]
    tags = op.get("tags") or []
    return {
        "schema": schema,
        "_name_parts": tail or [area],
        "path": path,
        "pagination": detect_pagination(raw_param_names),
        "path_params": path_params,
        "query_params": query_params,
        "columns": columns,
        "description": truncate(op.get("summary") or (tags[0] if tags else "")),
        "doc": operation_doc(op),
        "api_tag": tags[0] if tags else "",
        "area": area,
        "categories": ["cloudflare", "generated"]
        + ([sanitize(tags[0].lower().replace(" ", "_"))] if tags else []),
    }


_GENERIC_PARAM = {"id", "identifier", "param", "tag", "key", "name", "slug", "value"}
_STRIP_SUFFIXES = ("_id", "_identifier", "_param", "_tag")
_URL_PARAM_RE = re.compile(r"\{([^}]+)\}")


def _item_name_parts(path: str, area: str, area_idx: int, nonparam: list[str]) -> list[str]:
    """Name fragments (general→specific) for a get-by-id endpoint, relative to its area.

    Fragments exclude the leading area/parent segments (the schema already names
    those). If the path ends in ``{param}`` the singular base comes from the param
    (``zone_id`` → ``zone``); otherwise the trailing segments are the name.
    """
    parts = list(nonparam[area_idx + 1 :])  # segments after the area
    last = path.rstrip("/").split("/")[-1]
    if last.startswith("{") and last.endswith("}"):
        # A segment may hold several placeholders ("{event_t}_{event_n}"): name by the last.
        base = sanitize(_URL_PARAM_RE.findall(last)[-1]).strip("_")
        for suf in _STRIP_SUFFIXES:
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        if base and base not in _GENERIC_PARAM:
            parts = parts + [base]
    return parts or [area]


def assign_names_per_schema(descriptors: list[dict[str, Any]]) -> None:
    """Assign names unique *within each schema*, shortened to drop the schema prefix.

    Lists (tables) are plural nouns and are named first; items (lookups) are the
    singular noun (``record``, ``zone``). An item whose noun is already a table name
    (uncountables like ``settings``) becomes ``<noun>_by_id``; a clash with another
    item takes more of the path (``report_bytime``), then a numeric suffix.
    """
    from collections import defaultdict

    by_schema: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in descriptors:
        by_schema[d["schema"]].append(d)

    for schema, group in by_schema.items():
        used: set[str] = set()
        tables: set[str] = set()
        group.sort(key=lambda d: (d["kind"] == "item", d["path"]))
        for d in group:
            parts = d.pop("_name_parts") or ["item"]
            is_item = d["kind"] == "item"
            chosen: str | None = None
            for n in range(1, len(parts) + 1):
                candidate = shorten("_".join(parts[-n:]), schema)
                candidate = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", candidate).lower()  # snake_case
                for verb in ("list_", "get_"):  # retrieval verbs add nothing (vgi-lint VGI142)
                    if candidate.startswith(verb) and len(candidate) > len(verb):
                        candidate = candidate[len(verb) :]
                if candidate == "list" and n < len(parts):
                    continue  # "list" alone reads as a verb (list_by_id); take more of the path
                if is_item and candidate in tables:
                    candidate += "_by_id"
                if candidate and candidate not in used:
                    chosen = candidate
                    break
            if chosen is None:
                base = shorten("_".join(parts), schema) or "item"
                if is_item and base in tables:
                    base += "_by_id"
                k = 2
                chosen = f"{base}_{k}"
                while chosen in used:
                    k += 1
                    chosen = f"{base}_{k}"
            if is_item and not d["path_params"] and chosen.endswith("version"):
                # Parameterless ``*_version`` endpoints are breakdowns *across* versions
                # (radar ``http/summary/tls_version``); the plural says so and avoids
                # reading as a diagnostic version() function (vgi-lint VGI328).
                chosen += "s"
            used.add(chosen)
            if not is_item:
                tables.add(chosen)
            d["name"] = chosen


def _result_object_node(spec: Spec, schema: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Classify a single-object 200 schema: ('object'|'wrapperless'|'skip', item_node)."""
    merged = spec.flatten_props(schema)
    if "result" not in merged:
        # No envelope — the body itself is the object (if it has properties).
        return ("wrapperless", schema) if spec.flatten_props(schema) else ("skip", None)
    node = merged["result"]
    if "$ref" in node:
        node = spec.resolve(node["$ref"])
    if node.get("type") == "array":
        return ("skip", None)  # actually a list — handled elsewhere
    if node.get("type") == "object" or "properties" in node or "allOf" in node or "oneOf" in node:
        return ("object", node)
    return ("skip", None)  # scalar / unresolved


def build_item_descriptor(spec: Spec, path: str, op: dict[str, Any]) -> dict[str, Any] | None:
    """Build a get-one-by-id ('item') descriptor: path params become required args."""
    schema = (
        op.get("responses", {}).get("200", {}).get("content", {}).get("application/json", {}).get("schema")
    )
    if not schema:
        return None
    kind, item_node = _result_object_node(spec, schema)
    if kind == "skip" or item_node is None:
        return None
    result_path = "result" if kind == "object" else ""

    # Path params in URL order, with docs from the operation parameters.
    raw: dict[str, dict[str, Any]] = {}
    resolved = [spec.resolve(pr["$ref"]) if "$ref" in pr else pr for pr in op.get("parameters", [])]
    for pr in resolved:
        if pr.get("in") == "path" and pr.get("name"):
            raw[pr["name"]] = pr
    path_params = [path_param(spec, p, raw.get(p)) for p in _URL_PARAM_RE.findall(path)]
    query_params = item_query_params(spec, resolved)
    path_param_names = {p["name"] for p in path_params}

    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, prop in spec.flatten_props(item_node).items():
        col = sanitize(name)
        if col in seen or col in path_param_names:
            continue
        ot, fmt = spec.leaf_type(prop)
        columns.append(
            {
                "name": col,
                "type": arrow_type(ot, fmt),
                "doc": column_doc(spec, prop, col, arrow_type(ot, fmt)),
                "source": name if name != col else None,
            }
        )
        seen.add(col)
        if len(columns) >= MAX_COLUMNS:
            break
    if not columns:
        return None

    sch, area, area_idx, nonparam = classify(path)
    tags = op.get("tags") or []
    return {
        "schema": sch,
        "_name_parts": _item_name_parts(path, area, area_idx, nonparam),
        "kind": "item",
        "path": path,
        "pagination": "none",
        "result_path": result_path,
        "path_params": path_params,
        "query_params": query_params,
        # Radar reports reject a request without a time window: default to the last week.
        **(
            {"default_query": {"dateRange": "7d"}}
            if any(q.get("api_name") == "dateRange" for q in query_params)
            else {}
        ),
        "columns": columns,
        "description": truncate(op.get("summary") or (tags[0] if tags else "")),
        "doc": operation_doc(op),
        "api_tag": tags[0] if tags else "",
        "area": area,
        "categories": ["cloudflare", "generated", "item"],
    }


def main() -> int:
    emit = "--emit" in sys.argv
    spec = Spec(json.load(open(SPEC_PATH)))
    paths = spec.spec["paths"]

    descriptors: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    skipped = 0
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        op = item.get("get")
        if not op:
            continue
        op = with_path_item_params(spec, item, op)
        try:
            desc = build_descriptor(spec, path, op)
        except (KeyError, RecursionError):
            desc = None
        if desc is not None:
            desc["kind"] = "list"
            descriptors.append(desc)
            continue
        # Not a list — try a get-one-by-id item.
        try:
            it = build_item_descriptor(spec, path, op)
        except (KeyError, RecursionError):
            it = None
        if it is not None:
            items.append(it)
        else:
            skipped += 1

    descriptors.extend(items)
    assign_names_per_schema(descriptors)  # names unique within each schema

    # Stats
    list_descs = [d for d in descriptors if d["kind"] == "list"]
    item_descs = [d for d in descriptors if d["kind"] == "item"]
    print(f"GET paths considered: {sum(1 for p in paths.values() if isinstance(p, dict) and 'get' in p)}")
    print(f"List (collection) tables:   {len(list_descs)}")
    print(f"Item (get-by-id) functions: {len(item_descs)}")
    print(f"  wrapperless (result_path=''): {sum(1 for d in item_descs if d['result_path'] == '')}")
    print(f"Skipped (no usable schema): {skipped}")
    print(f"TOTAL descriptors: {len(descriptors)}")
    col_counts = [len(d["columns"]) for d in descriptors]
    print(
        f"Columns/resource: min={min(col_counts)} max={max(col_counts)} "
        f"avg={sum(col_counts) // len(col_counts)}"
    )
    from collections import Counter

    schema_counts = Counter(d["schema"] for d in descriptors)
    print(f"Schemas: {len(schema_counts)}")
    for s, c in schema_counts.most_common():
        print(f"   {c:4}  {s}")
    misc = sorted({classify(d["path"])[1] for d in descriptors if d["schema"] == "misc"})
    print(f"Unmapped areas → misc ({len(misc)}): {misc[:30]}")
    print("Sample dns names:", sorted(d["name"] for d in descriptors if d["schema"] == "dns"))

    if emit:
        manifest = {
            "_generated_from": "spec/openapi.json",
            "_spec_version": spec.spec.get("info", {}).get("version"),
            "resources": descriptors,
        }
        OUT_PATH.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        print(f"\nWrote {len(descriptors)} descriptors to {OUT_PATH.relative_to(ROOT)}")
    else:
        print("\n(dry run — pass --emit to write the manifest)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
