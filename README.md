<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://raw.githubusercontent.com/Query-farm/vgi-cloudflare/main/docs/vgi-logo.png" alt="Vector Gateway Interface logo" width="320">
  </a>
</p>

<h1 align="center">vgi-cloudflare</h1>

<p align="center">
  The <a href="https://developers.cloudflare.com/api/">Cloudflare API</a> as ordinary DuckDB tables — zones, DNS,<br>
  Zero Trust, Workers, security, traffic analytics, and Radar's view of the Internet.<br>
  A <strong>read-only</strong> <a href="https://query.farm/vgi/">VGI</a> worker, built by <a href="https://query.farm">🚜 Query.Farm</a>
</p>

<p align="center">
  <a href="https://github.com/Query-farm/vgi-cloudflare/actions/workflows/ci.yml"><img src="https://github.com/Query-farm/vgi-cloudflare/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.13%2B-blue.svg" alt="Python 3.13+">
  <a href="https://query.farm/vgi/"><img src="https://img.shields.io/badge/VGI-Vector%20Gateway%20Interface-2f7d32.svg" alt="VGI"></a>
</p>

---

Ask questions of your Cloudflare account in SQL — across every zone and
product at once — and join the answers with anything else DuckDB can read.

```sql
-- Which of my DNS records bypass Cloudflare's proxy, in every zone?
SELECT z.name AS zone, r.name, r.type, r.content
FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r
WHERE r.type IN ('A', 'AAAA', 'CNAME') AND r.proxiable AND NOT r.proxied
ORDER BY zone, r.name;
```

> **Read-only, with your own credentials.** Every query is answered live from
> Cloudflare's REST and GraphQL APIs using the API token in your DuckDB secret;
> nothing is cached or stored by the worker. The HTTP client issues `GET`s and
> GraphQL *queries* only — there is no code path that creates, changes, or
> deletes anything in your account. Values from SQL that land in a URL path are
> percent-encoded, so an id cannot traverse to a different endpoint.

## What people use it for

| You want to… | See |
|---|---|
| Inventory and audit DNS across every zone, diff it against a source of truth, back it up | [DNS](#dns-across-every-zone) |
| Catch configuration drift between zones (SSL mode, TLS version, HTTPS redirects, DNSSEC) | [Zone configuration](#catch-configuration-drift-between-zones) |
| See every certificate, who issued it, and what it covers | [Certificates](#certificates) |
| Review who has access: members without 2FA, Access apps and their policies, recent changes | [Access review](#access-and-security-review) |
| Inventory Workers, their custom domains and routes, D1 databases | [Workers & platform](#workers-and-the-developer-platform) |
| Report on traffic, caching, threats, and Worker errors | [Traffic](#traffic-and-performance) |
| Research the Internet itself — protocol adoption, top domains, outages, route leaks | [Radar](#internet-research-with-radar) |
| Keep a history, export to Parquet, or drive it from Python | [Beyond the API](#beyond-the-api-history-exports-and-python) |

## Quick start

You need DuckDB with the `vgi` extension: either **DuckDB 1.5.5** with
`INSTALL vgi FROM community`, or [Haybarn](https://query.farm) (`uvx haybarn-cli`,
or `pip install haybarn` for Python), which ships with it. Community builds can
lag the newest DuckDB release by a little — if `INSTALL` reports a 404, use the
version above.

```sql
INSTALL vgi FROM community; LOAD vgi;     -- Haybarn: just LOAD vgi;

ATTACH 'cloudflare' (TYPE vgi,
  LOCATION 'launch:uvx --from git+https://github.com/Query-farm/vgi-cloudflare vgi-cloudflare');

CREATE SECRET cf (TYPE cloudflare, api_token '<your-api-token>');

SELECT name, status FROM cloudflare.zones.zones;
```

- **`uvx`** fetches and caches the worker on first use; nothing to install. Pin a
  tag for a deployment (`…/vgi-cloudflare@v0.1.0 vgi-cloudflare`) so it can't
  change under you.
- **`launch:`** keeps one warm worker shared by every query, cursor, and DuckDB
  process. Without it each connection starts its own (a couple of seconds to
  build the ~2,000-object catalog).
- **The catalog is named `cloudflare`** — that is the name you `ATTACH` (an alias
  with `AS cf` works too). Objects are `cloudflare.<schema>.<object>`.
- **The token**: create one at <https://dash.cloudflare.com/profile/api-tokens>.
  The **"Read all resources"** template covers nearly everything with no write
  access; objects a token can't reach fail with Cloudflare's own permission
  error. A legacy Global API Key also works:
  `CREATE SECRET cf (TYPE cloudflare, api_key '<key>', email '<email>')`.

From a clone, `uv run cloudflare_worker.py` (stdio) and `uv run serve.py --port 8000`
(HTTP, `LOCATION 'http://localhost:8000'`) run with nothing installed — both carry
PEP-723 headers. This package is not published to PyPI; install it from this
repository.

## How it fits together

Most Cloudflare data lives under an **account** or a **zone**, so most queries
start from `cloudflare.accounts.accounts` or `cloudflare.zones.zones` and fan out:

- **List functions** (plural: `cloudflare.dns.records(zone_id)`) take the ids in
  their URL as arguments — a literal, or each row of another query, so one
  statement covers every zone. The endpoint's own filters are named arguments
  (`records(z.id, type := 'A')`).
- **Tables** (`cloudflare.zones.zones`, `cloudflare.accounts.accounts`) are the
  unscoped lists you start from.
- **Lookup functions** (singular: `cloudflare.dns.record(zone_id, id)`) fetch one
  object by id — an unknown id is no row, not an error.

The recipes below use all three. Every one of them was run against a real
account; the sample outputs are illustrative.

## DNS across every zone

**How many records of each type do I have, and how many are proxied?**

```sql
SELECT r.type, count(*) AS records, count(*) FILTER (WHERE r.proxied) AS proxied
FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r
GROUP BY r.type ORDER BY records DESC;
```

```text
┌───────┬─────────┬─────────┐
│ type  │ records │ proxied │
├───────┼─────────┼─────────┤
│ CNAME │      39 │      20 │
│ AAAA  │      21 │      21 │
│ TXT   │      14 │       0 │
│ MX    │       4 │       0 │
└───────┴─────────┴─────────┘
```

**Where does a hostname live?** Search every zone at once — handy when nobody
remembers which zone owns `www.something`:

```sql
SELECT z.name AS zone, r.name, r.type, r.content, r.ttl
FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r
WHERE r.name LIKE 'www.%'
ORDER BY zone, r.name;
```

**Does DNS match what we think it should be?** Keep the intended records in a
CSV (or a spreadsheet, or another database) and diff them against live DNS:

```sql
CREATE TEMP TABLE expected(name VARCHAR, type VARCHAR, content VARCHAR);
INSERT INTO expected VALUES
  ('example.com',     'CNAME', 'example.pages.dev'),
  ('www.example.com', 'CNAME', 'example.pages.dev'),
  ('api.example.com', 'A',     '192.0.2.10');
-- or: CREATE TEMP TABLE expected AS FROM 'expected_dns.csv';

CREATE TEMP TABLE actual AS
SELECT r.name, r.type, r.content
FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r
WHERE z.name = 'example.com';

SELECT 'missing' AS problem, * FROM (FROM expected EXCEPT FROM actual)
UNION ALL
SELECT 'unexpected', * FROM (
  SELECT * FROM actual WHERE type IN ('A', 'AAAA', 'CNAME') EXCEPT FROM expected)
ORDER BY problem, name;
```

```text
┌────────────┬─────────────────────┬──────┬────────────────┐
│  problem   │        name         │ type │    content     │
├────────────┼─────────────────────┼──────┼────────────────┤
│ missing    │ api.example.com     │ A    │ 192.0.2.10     │
│ unexpected │ old.example.com     │ A    │ 198.51.100.7   │
└────────────┴─────────────────────┴──────┴────────────────┘
```

**Back it all up** — one Parquet file of every record in every zone:

```sql
COPY (
  SELECT z.name AS zone, r.*
  FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r
) TO 'dns-backup.parquet' (FORMAT parquet);
```

## Catch configuration drift between zones

**Are all my zones configured the same way?** One row per zone, one column per
setting:

```sql
SELECT z.name AS zone,
       max(s.value) FILTER (WHERE s.id = 'ssl')              AS ssl_mode,
       max(s.value) FILTER (WHERE s.id = 'min_tls_version')  AS min_tls,
       max(s.value) FILTER (WHERE s.id = 'always_use_https') AS always_https,
       max(s.value) FILTER (WHERE s.id = 'security_level')   AS security_level
FROM cloudflare.zones.zones z, cloudflare.zones.settings(z.id) s
GROUP BY zone ORDER BY zone;
```

```text
┌──────────────────┬──────────┬─────────┬──────────────┬────────────────┐
│       zone       │ ssl_mode │ min_tls │ always_https │ security_level │
├──────────────────┼──────────┼─────────┼──────────────┼────────────────┤
│ example.com      │ full     │ 1.2     │ on           │ medium         │
│ example.net      │ full     │ 1.0     │ off          │ medium         │
│ example.org      │ flexible │ 1.0     │ off          │ high           │
└──────────────────┴──────────┴─────────┴──────────────┴────────────────┘
```

**Which zones don't have DNSSEC on?**

```sql
SELECT z.name AS zone, d.status
FROM cloudflare.zones.zones z, cloudflare.dns.dnssec(z.id) d
WHERE d.status <> 'active'
ORDER BY zone;
```

**What page rules are still in use?**

```sql
SELECT z.name AS zone, p.priority, p.status, p.targets, p.actions
FROM cloudflare.zones.zones z, cloudflare.zones.pagerules(z.id) p
ORDER BY zone, p.priority;
```

## Certificates

**Every certificate pack, who issued it, and which hostnames it covers:**

```sql
SELECT z.name AS zone, c.type, c.status, c.certificate_authority, c.hosts, c.validity_days
FROM cloudflare.zones.zones z, cloudflare.ssl.certificate_packs(z.id) c
ORDER BY zone, c.type;
```

Filter on `c.status <> 'active'` to see anything pending or failing validation,
or unnest `c.hosts` to find which pack covers a given hostname.

## Access and security review

**Who on the account doesn't have two-factor authentication?**

```sql
SELECT m.email, m.status, m.two_factor_enabled
FROM cloudflare.accounts.accounts a, cloudflare.accounts.members(a.id) m
WHERE NOT m.two_factor_enabled
ORDER BY m.email;
```

**What do my Zero Trust Access applications let through?** Fan out twice —
accounts → apps → each app's policies:

```sql
SELECT app.name AS app, app.domain, p.name AS policy, p.decision
FROM cloudflare.accounts.accounts a,
     cloudflare.access.apps(a.id) app,
     cloudflare.access.apps_policies(a.id, app.id) p
ORDER BY app, policy;
```

**Are any Gateway rules disabled?**

```sql
SELECT r.name, r.action, r.enabled, r.precedence
FROM cloudflare.accounts.accounts a, cloudflare.zero_trust.rules(a.id) r
WHERE NOT r.enabled
ORDER BY r.precedence;
```

**What changed recently, and how?** The account audit log, summarized by day:

```sql
SELECT l."when"::DATE AS day, l.action ->> 'type' AS action, count(*) AS changes
FROM cloudflare.accounts.accounts a, cloudflare.logs.audit_logs(a.id) l
GROUP BY ALL ORDER BY day DESC, changes DESC;
```

```text
┌────────────┬─────────────────┬─────────┐
│    day     │     action      │ changes │
├────────────┼─────────────────┼─────────┤
│ 2026-10-01 │ script_deploy   │      18 │
│ 2026-10-01 │ patch_settings  │      18 │
│ 2026-10-01 │ tail_logs_start │       6 │
└────────────┴─────────────────┴─────────┘
```

## Workers and the developer platform

**Which Workers changed most recently, and which are getting stale?**

```sql
SELECT s.id AS script, s.modified_on::DATE AS modified, s.compatibility_date, s.has_assets
FROM cloudflare.accounts.accounts a, cloudflare.workers.scripts(a.id) s
ORDER BY s.modified_on DESC;
```

**Which hostname serves which Worker?** Custom domains, and zone routes:

```sql
SELECT d.hostname, d.service, d.environment
FROM cloudflare.accounts.accounts a, cloudflare.workers.domains(a.id) d
ORDER BY d.hostname;

SELECT z.name AS zone, rt.pattern, rt.script
FROM cloudflare.zones.zones z, cloudflare.workers.routes(z.id) rt
ORDER BY zone, rt.pattern;
```

**What D1 databases exist?**

```sql
SELECT d.name, d.version, d.created_at::DATE AS created
FROM cloudflare.accounts.accounts a, cloudflare.storage.database(a.id) d
ORDER BY d.name;
```

## Traffic and performance

The analytics functions read Cloudflare's GraphQL Analytics API. Each takes a
`zone_id` (or `account_id`) and a **required** window — `since` and `until`,
inclusive and in UTC — so a query can't silently get less than it asked for.
Like every function here, the id can come from another table to cover every zone.

**Daily traffic, cache hit rate, and threats for a zone:**

```sql
SELECT date, requests,
       round(100.0 * cached_requests / requests, 1) AS cache_hit_pct,
       threats, unique_visitors
FROM cloudflare.analytics.http_requests_daily('<zone-id>',
       since := DATE '2026-09-01', until := DATE '2026-09-07')
ORDER BY date;
```

**Which zone got the most traffic last week?** One call per zone:

```sql
SELECT z.name AS zone, sum(d.requests) AS requests, sum(d.threats) AS threats
FROM cloudflare.zones.zones z,
     cloudflare.analytics.http_requests_daily(z.id,
       since := DATE '2026-09-24', until := DATE '2026-09-30') d
GROUP BY zone ORDER BY requests DESC;
```

**The busiest hours of a day:**

```sql
SELECT datetime, requests
FROM cloudflare.analytics.http_requests_hourly('<zone-id>',
       since := TIMESTAMPTZ '2026-10-01 00:00:00+00', until := TIMESTAMPTZ '2026-10-01 23:59:59+00')
ORDER BY requests DESC LIMIT 5;
```

**Where is traffic coming from?** Sampled requests by country:

```sql
SELECT client_country, sum(count) AS requests
FROM cloudflare.analytics.http_requests_adaptive('<zone-id>',
       since := TIMESTAMPTZ '2026-10-01 12:00:00+00', until := TIMESTAMPTZ '2026-10-01 18:00:00+00')
GROUP BY client_country ORDER BY requests DESC LIMIT 5;
```

**Which Workers are throwing errors?**

```sql
SELECT w.script_name, sum(w.requests) AS requests, sum(w.errors) AS errors
FROM cloudflare.accounts.accounts a,
     cloudflare.analytics.workers_invocations(a.id,
       since := TIMESTAMPTZ '2026-10-01 12:00:00+00', until := TIMESTAMPTZ '2026-10-01 13:00:00+00') w
GROUP BY w.script_name ORDER BY errors DESC, requests DESC;
```

The window must be written as constants (`DATE '…'`, `TIMESTAMPTZ '…'`) rather
than expressions like `current_date - 7` when the id comes from another table;
a `WHERE` on the time column still narrows the rows that come back.

## Internet research with Radar

Radar describes the Internet, not your account — no ids needed (a token is).
Reports default to the last 7 days; `date_range`, `location`, `asn`, and each
report's other parameters are named arguments. Results come back as JSON,
which DuckDB unpacks.

**Where is HTTP/3 adoption highest?**

```sql
SELECT t.value ->> 'clientCountryName' AS country,
       round((t.value ->> 'value')::DOUBLE, 1) AS http3_pct
FROM cloudflare.radar.http_version_http_version('HTTPv3', date_range := '28d') v,
     unnest(json_extract(v.top_0, '$[*]')) AS t(value)
ORDER BY http3_pct DESC LIMIT 5;
```

**What are the most popular domains in the US?**

```sql
SELECT (d.value ->> 'rank')::INT AS rank, d.value ->> 'domain' AS domain
FROM cloudflare.radar.top(location := 'US', limit_ := 10) r,
     unnest(json_extract(r.top_0, '$[*]')) AS d(value)
ORDER BY rank;
```

**What Internet outages has Radar seen this month?**

```sql
SELECT o.value ->> 'startDate' AS started, o.value ->> 'description' AS what
FROM cloudflare.radar.outages(date_range := '28d') r,
     unnest(json_extract(r.annotations, '$[*]')) AS o(value)
ORDER BY started DESC LIMIT 5;
```

**Any BGP route leaks in the last week?**

```sql
SELECT e.value ->> 'detected_ts' AS detected, e.value ->> 'leak_asn' AS leak_asn,
       e.value ->> 'leak_count' AS prefixes
FROM cloudflare.radar.leaks_events(date_range := '7d') r,
     unnest(json_extract(r.events, '$[*]')) AS e(value)
ORDER BY detected DESC LIMIT 5;
```

## Beyond the API: history, exports, and Python

The API only knows *now*. DuckDB can remember. **Snapshot**, and later ask what
changed:

```sql
ATTACH 'cloudflare-history.duckdb' AS history;
CREATE TABLE IF NOT EXISTS history.dns AS
SELECT current_date AS taken, z.name AS zone, r.name, r.type, r.content, r.proxied
FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r
LIMIT 0;

INSERT INTO history.dns
SELECT current_date, z.name, r.name, r.type, r.content, r.proxied
FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r;

-- records that differ between the two most recent snapshots
WITH days AS (SELECT DISTINCT taken FROM history.dns ORDER BY taken DESC LIMIT 2),
     cur AS (SELECT * EXCLUDE (taken) FROM history.dns WHERE taken = (SELECT max(taken) FROM days)),
     prev AS (SELECT * EXCLUDE (taken) FROM history.dns WHERE taken = (SELECT min(taken) FROM days))
SELECT 'added' AS change, * FROM (FROM cur EXCEPT FROM prev)
UNION ALL
SELECT 'removed', * FROM (FROM prev EXCEPT FROM cur);
```

Run the `INSERT` from cron and you have DNS history Cloudflare doesn't keep for
you. The same pattern works for settings, certificates, members, or Workers.

**From Python** (pandas, notebooks, scheduled jobs) — with Haybarn's Python
package:

```python
import os
import haybarn

con = haybarn.connect()
con.sql("LOAD vgi")
con.sql("""ATTACH 'cloudflare' (TYPE vgi,
  LOCATION 'launch:uvx --from git+https://github.com/Query-farm/vgi-cloudflare vgi-cloudflare')""")
con.sql(f"CREATE SECRET cf (TYPE cloudflare, api_token '{os.environ['CLOUDFLARE_API_TOKEN']}')")

df = con.sql("""
  SELECT z.name AS zone, r.type, count(*) AS records
  FROM cloudflare.zones.zones z, cloudflare.dns.records(z.id) r
  GROUP BY ALL ORDER BY zone, records DESC
""").df()
```

**With an AI assistant.** Every table and function documents itself — what it
returns, which filters it needs, how to call it, with examples — in the catalog
metadata agents read (`vgi.doc_llm`). An assistant connected to DuckDB can go
from *"which of my zones still allow TLS 1.0?"* to the settings query above on
its own; the [agent-acceptance suite](#catalog-metadata) measures exactly that.

## Developing

```bash
git clone https://github.com/Query-farm/vgi-cloudflare
cd vgi-cloudflare

uv sync                  # dependencies from PyPI, plus the dev group
make test-unit           # offline tests (mocked HTTP)
make lint                # ruff check + format --check
```

To develop against local checkouts of `vgi-python` / `vgi-rpc`, install them
over the top rather than editing `pyproject.toml` (a path source would break the
clone for everyone else): `uv pip install -e ../vgi-python -e ../vgi-rpc`.

## Surface

1,148 functions and 230 tables across 24 product schemas, generated from
Cloudflare's OpenAPI description. Every object carries agent- and human-facing
documentation built from that spec: what it returns, which filters it requires,
how to call it, and examples.

### Four shapes

| Shape | Named | Called | Example |
|---|---|---|---|
| **Table** | plural noun, no ids | scanned like any table | `cloudflare.zones.zones` |
| **List function** | plural noun | its URL ids as arguments, literal or per row; 1 → N rows; the endpoint's API filters as named arguments | `cloudflare.dns.records(z.id, type := 'A')` |
| **Lookup** | singular noun | path ids as arguments; 0 or 1 row per call | `cloudflare.dns.record(zone_id, id)` |
| **Analytics** | `cloudflare.analytics.*` | GraphQL rollups: the zone or account id, plus a required `since`/`until` window | `cloudflare.analytics.http_requests_daily(z.id, since := …, until := …)` |

A lookup whose noun is already a list's name takes `_by_id`
(`cloudflare.zones.rules_by_id`). Parameterless endpoints — mostly Radar reports
— are both a table and a function, so `SELECT * FROM cloudflare.radar.http_versions`
and `cloudflare.radar.http_versions(date_range := '28d')` both work.

### Schemas

| Schema | Covers | Tables | Functions |
|---|---|---:|---:|
| `zones` | Zones (domains) and per-zone configuration: settings, cache, rules, custom hostnames, waiting rooms | 1 | 84 |
| `dns` | DNS records, DNSSEC, secondary DNS, DNS Firewall, DNS analytics | 0 | 28 |
| `accounts` | Accounts, members, roles, API tokens, organizations, the token's user | 16 | 68 |
| `access` | Cloudflare Access: applications, policies, identity providers, service tokens | 0 | 64 |
| `zero_trust` | Gateway rules, DLP, devices and posture, DEX, tunnels, risk scoring | 0 | 110 |
| `security` | Threat intelligence, Security Center, URL scanner, brand protection, bots | 0 | 102 |
| `ssl` | Edge, custom, origin, and mTLS certificates; certificate authorities | 1 | 33 |
| `waf` | Legacy firewall: IP access rules, user-agent blocks, lockdowns | 0 | 4 |
| `api_gateway` | API Shield: discovered operations, schemas, validation | 0 | 15 |
| `load_balancing` | Load balancers, pools, monitors | 0 | 9 |
| `workers` | Workers scripts and deployments, Workflows, Queues, Pipelines, Containers | 0 | 84 |
| `storage` | R2 buckets and Data Catalog, D1, Hyperdrive | 0 | 13 |
| `ai` | AI Gateway, AI Search (AutoRAG), Workers AI models, Vectorize | 0 | 56 |
| `pages` | Pages projects, deployments, domains | 0 | 7 |
| `stream` | Stream video, live inputs, RealtimeKit, Calls/TURN | 0 | 35 |
| `email` | Email Routing and Email Security | 0 | 50 |
| `magic` | Magic Transit/WAN, BYOIP prefixes, interconnects | 1 | 69 |
| `logs` | Logpush jobs, datasets, audit logs | 0 | 19 |
| `alerting` | Notification policies, destinations, alert history | 0 | 12 |
| `billing` | Billable and pay-as-you-go usage | 0 | 2 |
| `registrar` | Domains registered with Cloudflare Registrar | 0 | 1 |
| `browser_rendering` | Headless browser sessions | 0 | 7 |
| `analytics` | GraphQL traffic, firewall, health-check, and Workers rollups | 6 | 6 |
| `radar` | Cloudflare Radar: Internet-wide traffic, attack, routing, and adoption trends | 205 | 270 |

`DESCRIBE cloudflare.dns.records` shows any object's columns; the
`vgi.doc_llm` / `vgi.doc_md` tags in `duckdb_tables()` / `duckdb_functions()`
explain it in prose.

## How filters work

**Ids are arguments.** An endpoint whose URL contains ids —
`/zones/{zone_id}/dns_records` — is a function of them: `cloudflare.dns.records(zone_id)`.
Pass a literal, or columns of another query (`FROM zones z, records(z.id)`), which
is a lateral join, so one statement covers every zone.

**Some filters run at Cloudflare.** An endpoint's own query filters are named
arguments — `records(z.id, type := 'A')` asks Cloudflare for A records only. On
the unscoped tables, equality filters on such columns (`status` on zones, …) are
sent upstream automatically. Every `WHERE` predicate is also applied in DuckDB to
the rows that come back, so results are correct either way; the API-side filter
just fetches less.

**Lateral calls are batched.** A list function or lookup receives a whole chunk of
input rows at once, fetches them concurrently (up to 8 requests in flight),
fetches a repeated id only once, and maps each output row back to the row that
produced it. A NULL id produces no rows; a lookup's 404 is "no row", while a
list's 404 is an error (a missing parent is not an empty list).

## Analytics (GraphQL)

| Function | Scope | Grain | What |
|---|---|---|---|
| `http_requests_daily` | zone | day | requests, bytes, cache, encryption, page views, threats, uniques |
| `http_requests_hourly` | zone | hour | the same, hourly |
| `http_requests_adaptive` | zone | event time | sampled requests by country, host, method, status |
| `firewall_events` | zone | event time | WAF/firewall events by action, source, country, rule |
| `health_check_events` | zone | event time | origin health checks by hostname, region, origin, status |
| `workers_invocations` | account | event time | Worker requests, errors, subrequests by script and status |

Each function takes the zone or account id (a literal, or per row of another
query) and a required window, `since` and `until`. There is no default window:
one that silently defaulted would quietly drop data a query asked for. The
GraphQL API caps a response at 10,000 groups and has no cursor, so a window
that comes back full is split in half and re-queried until each piece fits;
results are complete rather than silently truncated. `firewall_events` and
`health_check_events` exist only on plans that include them; elsewhere the
query fails with an error saying so, instead of returning an empty result that
reads like "no events".

Every dataset, dimension, and metric name was verified against Cloudflare's live
GraphQL schema by introspection.

## Radar

Radar reports describe the Internet, not your account, so they take no ids.
Cloudflare requires a time window on them; the worker sends `dateRange=7d`
unless you choose another. Each report's own query parameters are named
arguments — `date_range`, `location`, `asn`, `continent`, and so on, with the
allowed values declared — and results arrive as JSON columns
(`summary_0`, `serie_0`, `top_0`, `meta`) to unpack with DuckDB's JSON
functions:

```sql
SELECT key AS version, round((summary_0 ->> key)::DOUBLE, 1) AS pct
FROM cloudflare.radar.http_versions(date_range := '28d'),
     unnest(json_keys(summary_0)) AS k(key)
ORDER BY pct DESC;
```

## Design notes

**Descriptor-driven.** Each endpoint is a `ResourceDescriptor` — path, path and
query parameters, columns, pagination. One generic runtime turns a descriptor
into a table, a list function, or a lookup, so there is no per-endpoint code. A few
core resources (`zones`, `accounts`, `members`, DNS `records`, and their
lookups) are hand-curated; the rest are generated.

**Generated from the OpenAPI spec.** `tools/generate_resources.py` reads
Cloudflare's [published OpenAPI description](https://github.com/cloudflare/api-schemas)
and writes `vgi_cloudflare/resources_generated.json`. It follows `$ref` /
`allOf` / `oneOf` to the real field descriptions (shared components carry them,
not the referencing property), keeps each operation's long-form description,
inherits path-level parameters, declares enums and regex patterns as argument
constraints, and drops spec "descriptions" that merely restate a field's name.
Columns the spec doesn't document are left undocumented rather than invented.

**Docs are generated from facts.** `vgi_cloudflare/docs.py` writes each object's
`vgi.doc_llm`, `vgi.doc_md`, examples, result schema, and navigation category
from its descriptor — the endpoint, what one row is, which filters are required
or sent upstream, and how to call it. Schema and catalog docs are hand-written
(`vgi_cloudflare/schema_docs.py`). Runnable SQL lives only in
`vgi.example_queries`, never inline in prose.

**Pagination** follows the endpoint: page-numbered, cursor, or none. A scan
fetches every page of a binding before emitting it as one batch.

**Rate limits.** A `429` is retried with backoff, honouring `Retry-After`.

## Authentication

**Upstream (worker → Cloudflare)** uses the `cloudflare` DuckDB secret: an
`api_token` (Bearer), or `api_key` + `email`. Each query carries the caller's
secret, so one worker process can serve many users.

For tooling that cannot create a secret — `vgi-lint simulate` runs its setup
SQL before `ATTACH`, when the `cloudflare` secret type doesn't exist yet — the
worker falls back to a token file named by `VGI_CLOUDFLARE_TOKEN_FILE`, and only
when a query brings no secret. **Never set it on a shared deployment**: every
caller would act with that token.

**Inbound (clients → an HTTP deployment)** is configured with `VGI_*`
environment variables (JWT / OAuth) — see `fly.toml`. Keep real secrets in
`fly secrets`, not in the file.

## Catalog metadata

The worker is held to [`vgi-lint`](https://github.com/Query-farm/vgi-lint-check)'s
strict profile — `vgi-lint lint --fail-on error` passes in both the structural
and execution tiers. Three size rules are waived in `vgi-lint.toml` with their
reasons on record (the catalog mirrors an API with well over 500 endpoints); no
documentation rule is waived. The remaining warnings are mostly columns
Cloudflare's own spec leaves undocumented.

An agent-acceptance suite (`vgi.agent_test_tasks`, graders in
`vgi-agent-tests.yaml`) gives an LLM analyst real questions — *"how many DNS
records of each type exist across my zones?"* — and grades its SQL against a
reference by result. It covers the objects that return data for the account it
was written against; run it against your own with:

```bash
vgi-lint simulate "launch:env VGI_CLOUDFLARE_TOKEN_FILE=$HOME/cf-read-token.txt uv run cloudflare_worker.py"
```

## Tests

| Command | What | Needs |
|---|---|---|
| `make test-unit` | pytest with mocked HTTP: lifecycle, pagination, pushdown, lateral batching, provenance, splitting, naming | nothing |
| `make test-mock` | real DuckDB `ATTACH` against a local fake Cloudflare: lateral, `LEFT JOIN LATERAL`, a list across pages | the `vgi` sqllogictest runner |
| `make test-stdio` | real DuckDB against real Cloudflare (`test/sql/live*.test`) | a token in `$CLOUDFLARE_API_TOKEN` or `~/cf-read-token.txt` |
| `vgi-lint simulate` | the agent-acceptance suite | a token file, and Claude |

The live tests skip cleanly without a token. They assert shapes and invariants
(every list row carries the zone that produced it; a named-argument filter
agrees with filtering in SQL), not account-specific values, so they pass for any read token.

## Deployment

`Dockerfile` installs the package from PyPI dependencies and runs
`vgi-cloudflare-http`; `fly.toml` scales to zero with a `/health` check.
`make deploy` builds, smoke-tests the image, pushes it, and runs `fly deploy`.

## License

Copyright © 2026 [Query Farm LLC](https://query.farm)

Released under the **MIT License** — see [LICENSE](LICENSE).

The data this worker returns belongs to your Cloudflare account (or, for Radar,
to Cloudflare), is not covered by that license, and is subject to
[Cloudflare's terms](https://www.cloudflare.com/terms/). The generated endpoint
manifest derives from Cloudflare's BSD-3-Clause
[api-schemas](https://github.com/cloudflare/api-schemas). See [NOTICE](NOTICE).
This project is not affiliated with or endorsed by Cloudflare, Inc.

---

<p align="center">
  Built with <a href="https://query.farm/vgi/">VGI — the Vector Gateway Interface</a><br>
  by <a href="https://query.farm">🚜 Query.Farm</a>
</p>
