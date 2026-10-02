"""Hand-written documentation for the catalog and each product schema.

Per-object docs are generated from the OpenAPI spec (see ``docs.py``); these say
what each *product family* is for, how it is scoped, and when to reach for it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SchemaDoc:
    title: str
    comment: str  # one or two sentences (the schema's description)
    llm: str  # when an agent should look here, and how objects are scoped
    keywords: tuple[str, ...]
    md_extra: str = ""  # extra human-facing notes for doc_md


_ACCOUNT = "Most objects are scoped to an account: they are functions of its account_id — pass an id, or take ids per row from cloudflare.accounts.accounts."
_ZONE = "Most objects are scoped to a zone: they are functions of its zone_id — pass an id, or take ids per row from cloudflare.zones.zones."

SCHEMA_DOCS: dict[str, SchemaDoc] = {
    "access": SchemaDoc(
        "Cloudflare Access",
        "Cloudflare Access (Zero Trust application security): protected applications, access policies, identity providers, service tokens, groups, certificates, and authentication logs.",
        "Use for questions about who can reach which internal application and how: Access applications and their policies, identity provider configuration, service tokens for machine access, mTLS certificates, and login/request audit logs. "
        + _ACCOUNT,
        ("zero trust", "access", "sso", "identity provider", "application security", "policies"),
    ),
    "accounts": SchemaDoc(
        "Cloudflare Accounts & Identity",
        "Cloudflare accounts and identity: the accounts a token can see, their members and roles, API tokens, permission groups, organizations, tenants, and the token's own user profile.",
        "Start here to find account ids (cloudflare.accounts.accounts) for every account-scoped table, to audit membership and roles, or to inspect API tokens and permission groups. cloudflare.accounts.user describes the user that owns the current token.",
        ("account", "members", "users", "roles", "api tokens", "permissions", "iam", "organizations"),
    ),
    "ai": SchemaDoc(
        "Cloudflare AI",
        "Cloudflare AI products: AI Gateway (logging, caching, and rate limiting in front of model providers), AI Search / AutoRAG indexes, Workers AI models, and Vectorize vector indexes.",
        "Use for questions about AI traffic and retrieval infrastructure: AI Gateway gateways, their request logs, datasets and evaluations; AI Search (AutoRAG) instances and indexing jobs; available Workers AI models; and Vectorize indexes. "
        + _ACCOUNT,
        ("ai gateway", "llm", "workers ai", "autorag", "ai search", "vectorize", "rag"),
    ),
    "alerting": SchemaDoc(
        "Cloudflare Notifications & Alerts",
        "Cloudflare Notifications: alert policies, delivery destinations (email, PagerDuty, webhooks), silences, and the history of alerts that fired for an account.",
        "Use to audit what Cloudflare alerts are configured, where they are delivered, which are silenced, and what fired recently (alert history). "
        + _ACCOUNT,
        ("notifications", "alerts", "pagerduty", "webhooks", "alert history"),
    ),
    "analytics": SchemaDoc(
        "Cloudflare Analytics (GraphQL)",
        "Cloudflare traffic and security analytics from the GraphQL Analytics API: HTTP requests by day or hour, adaptive request samples, firewall events, health checks, and Workers invocations.",
        "Use for time-series questions — traffic volume, bandwidth, threats, cache ratios, firewall actions, Worker errors. Each is a function of a zone_id (account_id for Workers), a literal or per row of cloudflare.zones.zones, with a required window: since := … and until := …. Large windows are split automatically so results aren't truncated.",
        (
            "analytics",
            "traffic",
            "requests",
            "bandwidth",
            "threats",
            "firewall events",
            "graphql",
            "time series",
        ),
        "Firewall and health-check datasets are only available on Cloudflare plans that include them; on other plans the query fails with an explanation rather than returning empty results.",
    ),
    "api_gateway": SchemaDoc(
        "Cloudflare API Shield",
        "Cloudflare API Shield (API Gateway) for a zone: discovered and managed API operations, endpoint schemas, hostnames, labels, and schema validation configuration.",
        "Use to see which API endpoints Cloudflare has discovered or is protecting on a zone, the uploaded OpenAPI schemas used for validation, and their labels. "
        + _ZONE,
        ("api shield", "api gateway", "api discovery", "schema validation", "endpoints"),
    ),
    "billing": SchemaDoc(
        "Cloudflare Billing",
        "Cloudflare billing for an account: billable usage and pay-as-you-go usage records, for understanding what an account is charged for.",
        "Use for cost and usage questions about an account's Cloudflare subscription. " + _ACCOUNT,
        ("billing", "usage", "cost", "subscription", "invoice"),
    ),
    "browser_rendering": SchemaDoc(
        "Cloudflare Browser Rendering",
        "Cloudflare Browser Rendering: headless browser sessions run on Cloudflare's network, with their DevTools targets and session details for an account.",
        "Use to inspect Browser Rendering sessions (headless Chrome run by Workers or the REST API) and their DevTools targets. "
        + _ACCOUNT,
        ("browser rendering", "headless browser", "puppeteer", "devtools", "sessions"),
    ),
    "dns": SchemaDoc(
        "Cloudflare DNS",
        "Cloudflare DNS: records in each zone, DNSSEC, zone DNS settings, secondary DNS (zone transfers, peers, TSIG keys), DNS Firewall clusters, custom nameservers, and DNS analytics reports.",
        "Use for anything about name resolution: which records exist and where they point (records), whether they are proxied, DNSSEC status, secondary DNS transfer setup, and DNS Firewall. Record functions take a zone_id; DNS Firewall and secondary-DNS peers take an account_id.",
        ("dns", "dns records", "nameservers", "dnssec", "secondary dns", "zone transfer", "dns firewall"),
    ),
    "email": SchemaDoc(
        "Cloudflare Email",
        "Cloudflare email products: Email Routing (addresses, routing rules, destination addresses) and Email Security (message investigation, allow/block policies, trusted domains, impersonation registry).",
        "Use for questions about how mail to a domain is routed, and about Email Security detections: investigating suspicious messages, their action logs, and allow/block configuration. Email Routing settings and rules are per zone (destination addresses are per account); Email Security is account-scoped.",
        ("email routing", "email security", "phishing", "mail", "allow list", "block list"),
    ),
    "load_balancing": SchemaDoc(
        "Cloudflare Load Balancing",
        "Cloudflare Load Balancing: load balancers, origin pools, health monitors, and their health and reference relationships.",
        "Use to see how traffic is distributed across origins: pools and their origins, which monitors check them, and which load balancers reference each pool. Pools and monitors are account-scoped; load balancers are zone-scoped.",
        ("load balancing", "origin pools", "health monitors", "failover", "traffic steering"),
    ),
    "logs": SchemaDoc(
        "Cloudflare Logs",
        "Cloudflare logging: Logpush jobs and their destinations, available log datasets and fields, retention settings, and account audit logs.",
        "Use to audit where logs are pushed (Logpush jobs and destinations), which datasets and fields are available, and to read the account audit log of configuration changes.",
        ("logs", "logpush", "audit log", "log retention", "datasets"),
    ),
    "magic": SchemaDoc(
        "Cloudflare Magic Networking",
        "Cloudflare network services: Magic Transit and Magic WAN (sites, tunnels, routes, connectors), BYOIP address management (prefixes, address maps), and Cloudflare Network Interconnect.",
        "Use for network-layer infrastructure: IP prefixes you bring to Cloudflare, Magic WAN sites and connectors, GRE/IPsec tunnels, static routes, and interconnects. "
        + _ACCOUNT,
        ("magic transit", "magic wan", "byoip", "ip prefixes", "tunnels", "network interconnect", "routes"),
    ),
    "pages": SchemaDoc(
        "Cloudflare Pages",
        "Cloudflare Pages: projects, deployments, deployment logs, and custom domains for static and full-stack sites.",
        "Use to see Pages projects, their deployment history and status, and the domains attached to them. "
        + _ACCOUNT,
        ("pages", "deployments", "static sites", "jamstack", "custom domains"),
    ),
    "radar": SchemaDoc(
        "Cloudflare Radar",
        "Cloudflare Radar: Internet-wide traffic, security, routing, DNS, and adoption trends observed across Cloudflare's network — summaries and time series by protocol, device, OS, location, and more.",
        "Use for Internet-level questions that are not about your own account — e.g. global HTTP/3 adoption, attack trends, BGP route leaks, top domains, DNS query mixes. Radar functions take no account or zone; they return Cloudflare's published aggregates for the default time window.",
        (
            "radar",
            "internet trends",
            "traffic trends",
            "attacks",
            "bgp",
            "http versions",
            "global statistics",
        ),
    ),
    "registrar": SchemaDoc(
        "Cloudflare Registrar",
        "Cloudflare Registrar: domains registered or transferred through Cloudflare for an account, with registration and renewal details.",
        "Use to list domains registered with Cloudflare Registrar and check their expiry, auto-renew, and lock status. "
        + _ACCOUNT,
        ("registrar", "domain registration", "domains", "renewal", "expiry"),
    ),
    "security": SchemaDoc(
        "Cloudflare Security & Threat Intelligence",
        "Cloudflare security and threat intelligence: Cloudforce One threat events and requests, Intel lookups (domains, IPs, ASNs), Security Center insights, URL scanner, brand protection, abuse reports, vulnerability scanner, and bot management.",
        "Use for threat-intelligence and security-posture questions: what Cloudflare knows about a domain or IP, open Security Center insights, URL scan results, brand impersonation alerts, and Cloudforce One threat events. Mostly account-scoped; some bot and fraud settings are per zone.",
        (
            "threat intelligence",
            "cloudforce one",
            "security center",
            "url scanner",
            "brand protection",
            "bots",
            "intel",
        ),
    ),
    "ssl": SchemaDoc(
        "Cloudflare SSL/TLS & Certificates",
        "Cloudflare SSL/TLS: edge certificate packs, custom and keyless certificates, origin and client (mTLS) certificates, certificate authorities, Advanced Certificate Manager, and certificate transparency monitoring.",
        "Use to audit certificates — what is deployed at the edge for a zone, when certificates expire, which client certificates exist for mTLS, and custom CSRs. Edge certificates are zone-scoped, mTLS certificates are account-scoped, and custom CSRs exist at both levels.",
        ("ssl", "tls", "certificates", "mtls", "origin certificates", "certificate expiry", "acm"),
    ),
    "storage": SchemaDoc(
        "Cloudflare Storage & Databases",
        "Cloudflare storage and databases: R2 buckets and the R2 Data Catalog, D1 databases, Hyperdrive configurations, and other account storage resources.",
        "Use to inventory where data lives on Cloudflare: R2 buckets and catalog tables, D1 databases, and Hyperdrive connection configs. "
        + _ACCOUNT,
        ("r2", "object storage", "d1", "database", "hyperdrive", "data catalog", "iceberg"),
    ),
    "stream": SchemaDoc(
        "Cloudflare Stream & Realtime",
        "Cloudflare media: Stream videos, live inputs, captions, watermarks and signing keys; Realtime (RealtimeKit apps, meetings, livestreams) and Calls/TURN services.",
        "Use for video and real-time media questions: uploaded Stream videos and their status, live inputs, watermark profiles, RealtimeKit meetings and sessions, and TURN keys. "
        + _ACCOUNT,
        ("stream", "video", "live streaming", "realtime", "webrtc", "calls", "turn"),
    ),
    "waf": SchemaDoc(
        "Cloudflare Firewall (Legacy WAF)",
        "Cloudflare firewall configuration for a zone: legacy firewall rules, IP access rules, user-agent blocking, and lockdown rules.",
        "Use to audit older firewall-style configuration on a zone — IP access rules, user-agent blocks, and zone lockdowns. "
        + _ZONE,
        ("waf", "firewall", "ip access rules", "user agent blocking", "zone lockdown"),
    ),
    "workers": SchemaDoc(
        "Cloudflare Workers & Developer Platform",
        "Cloudflare developer platform: Workers scripts and their versions, deployments, routes and domains; Workflows, Queues, Pipelines, Containers, Secrets Store, and Workers Builds.",
        "Use for serverless application questions: which Workers exist and how they are deployed, their routes, versions, and settings; Queues and consumers; Workflows and their instances; Pipelines; Containers; and builds. "
        + _ACCOUNT,
        ("workers", "serverless", "scripts", "deployments", "queues", "workflows", "pipelines", "containers"),
    ),
    "zero_trust": SchemaDoc(
        "Cloudflare Zero Trust (Gateway, DLP, Devices)",
        "Cloudflare Zero Trust: Gateway filtering rules and locations, Data Loss Prevention profiles and datasets, device enrollment and posture (WARP), Digital Experience Monitoring, tunnels, and risk scoring.",
        "Use for secure-web-gateway and endpoint questions: Gateway DNS/HTTP/network rules, DLP detection profiles, enrolled devices and their posture, DEX tests and fleet status, Cloudflare Tunnels, and user risk scores. "
        + _ACCOUNT,
        ("zero trust", "gateway", "dlp", "warp", "devices", "device posture", "dex", "tunnels"),
    ),
    "zones": SchemaDoc(
        "Cloudflare Zones",
        "Cloudflare zones (domains) and their per-zone configuration: settings, caching, page and configuration rules, custom pages and hostnames, waiting rooms, Spectrum, Page Shield, speed, and origin configuration.",
        "Start here to find zone ids (cloudflare.zones.zones) for every zone-scoped table, or to audit a zone's configuration: settings, cache rules, custom hostnames (SSL for SaaS), waiting rooms, and Page Shield scripts. "
        + _ZONE,
        ("zones", "domains", "zone settings", "cache", "page rules", "custom hostnames", "waiting rooms"),
    ),
}

CATALOG_COMMENT = (
    "Query the Cloudflare API from SQL: zones, DNS, accounts, security, Workers, Zero Trust, "
    "and traffic analytics as DuckDB tables and functions."
)

CATALOG_DOC_LLM = (
    "Read-only SQL access to a Cloudflare account through the Cloudflare REST and GraphQL APIs, "
    "fetched live on each query with the caller's API token (a DuckDB secret of TYPE cloudflare). "
    "Objects are grouped by product into schemas (dns, zones, accounts, security, workers, zero_trust, "
    "analytics, radar, and more). Most data is scoped to an account or zone: start from "
    "cloudflare.accounts.accounts or cloudflare.zones.zones to get ids. Scoped lists are functions of "
    "those ids — cloudflare.dns.records(zone_id) — taking a literal or, in a lateral join from "
    "cloudflare.zones.zones, every zone at once; their API filters are named arguments. Singular functions such as "
    "cloudflare.dns.record fetch one object by id. Traffic and security time series are in "
    "cloudflare.analytics; Internet-wide trends are in cloudflare.radar."
)

CATALOG_DOC_MD = f"""# Cloudflare API for DuckDB

{CATALOG_DOC_LLM}

## Getting started

Create a DuckDB secret of type `cloudflare` holding an API token (`api_token`), or a legacy global key
(`api_key` plus `email`). A read-only token from the "Read all resources" template covers every schema.
Data is fetched from Cloudflare at query time; nothing is cached or stored.

## Shapes of object

| Shape | Naming | Called how |
| --- | --- | --- |
| Table | plural noun, no ids, e.g. `cloudflare.zones.zones` | scanned like any table |
| List function | plural noun, e.g. `cloudflare.dns.records` | its URL ids as arguments, literal or per row; API filters as named arguments |
| Lookup function | singular noun, e.g. `cloudflare.dns.record` | path ids as arguments; 0 or 1 row |
| Analytics table | `cloudflare.analytics.*` | GraphQL rollups; the time range comes from the filter |

## Notes

- Filters on a table's documented API filter columns are sent to Cloudflare; other filters run in DuckDB.
- Lookup functions return no row for an unknown id instead of failing, so they are safe in joins.
- Requests are retried on rate limiting (HTTP 429). Large analytics windows are split to avoid truncation.
"""

CATALOG_KEYWORDS = ("cloudflare", "dns", "zones", "cdn", "waf", "zero trust", "workers", "analytics", "api")
