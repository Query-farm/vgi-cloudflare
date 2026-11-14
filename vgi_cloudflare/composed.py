"""Hand-written examples that combine several of the catalog's objects in one query.

The generated examples (``docs.py``) show each object alone, or fanned out from its
parent table. These show the questions people actually ask across objects: a zone's
settings next to its DNSSEC state, DNS records filtered by what Cloudflare could do
with them, traffic beside the DNS footprint it serves.

Each example is defined once and listed both on the object that drives the query
(within its first few examples, which is all an agent's ``describe`` shows) and in
its schema's ``vgi.example_queries``.
"""

from __future__ import annotations

from .descriptor import Example

DNS_ONLY_PROXIABLE = Example(
    "SELECT z.name AS zone, r.type, r.name AS record, r.content "
    "FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r "
    "WHERE r.type IN ('A', 'AAAA', 'CNAME') AND r.proxiable AND NOT r.proxied "
    "ORDER BY zone, record, r.type",
    "Across every zone, the A/AAAA/CNAME records Cloudflare could proxy but that are DNS-only: "
    "their targets are answered straight from DNS instead of sitting behind Cloudflare.",
)

ZONE_SECURITY_POSTURE = Example(
    "SELECT z.name AS zone, ssl.value AS ssl_mode, https.value AS always_use_https, "
    "d.status AS dnssec_status "
    "FROM cloudflare.zones.zones z, cloudflare.zones.setting(z.id, 'ssl') ssl, "
    "cloudflare.zones.setting(z.id, 'always_use_https') https, cloudflare.dns.dnssec(z.id) d "
    "ORDER BY zone",
    "One row per zone with its SSL/TLS encryption mode, its Always Use HTTPS setting and its "
    "DNSSEC status, read from two settings lookups and the DNSSEC function.",
)

TRAFFIC_VS_DNS = Example(
    "WITH dns AS (SELECT z.id AS zone_id, z.name AS zone, count(*) AS dns_records "
    "FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r GROUP BY ALL), "
    "traffic AS (SELECT t.zone_id, sum(t.requests) AS requests, sum(t.cached_requests) AS cached "
    "FROM cloudflare.zones.zones z, cloudflare.analytics.http_requests_daily(z.id, "
    "since := current_date - 7, until := current_date - 1) t GROUP BY ALL) "
    "SELECT d.zone, d.dns_records, coalesce(t.requests, 0) AS requests, "
    "round(100.0 * t.cached / nullif(t.requests, 0), 1) AS cached_pct "
    "FROM dns d LEFT JOIN traffic t USING (zone_id) ORDER BY requests DESC, d.zone",
    "Each zone's DNS record count beside the last 7 full days' requests and cache-hit percentage; a zone "
    "with records but 0 requests is a candidate for cleanup (cached_pct is NULL without traffic).",
)

#: Composed examples for the object that drives each one, keyed by (schema, object name).
BY_OBJECT: dict[tuple[str, str], tuple[Example, ...]] = {
    ("dns", "records"): (DNS_ONLY_PROXIABLE,),
    ("zones", "setting"): (ZONE_SECURITY_POSTURE,),
    ("dns", "dnssec"): (ZONE_SECURITY_POSTURE,),
    ("analytics", "http_requests_daily"): (TRAFFIC_VS_DNS,),
}

#: Composed examples listed first in each schema's ``vgi.example_queries``.
BY_SCHEMA: dict[str, tuple[Example, ...]] = {
    "dns": (DNS_ONLY_PROXIABLE, ZONE_SECURITY_POSTURE),
    "zones": (ZONE_SECURITY_POSTURE,),
    "analytics": (TRAFFIC_VS_DNS,),
}


def with_composed(
    schema: str, name: str, examples: tuple[Example, ...] | list[Example]
) -> tuple[Example, ...]:
    """``examples`` with the object's composed ones placed second (after its simplest call)."""
    extra = BY_OBJECT.get((schema, name), ())
    if not extra:
        return tuple(examples)
    head, rest = tuple(examples[:1]), tuple(examples[1:])
    return (*head, *extra, *rest)
