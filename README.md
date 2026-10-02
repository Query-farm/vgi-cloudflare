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

```sql
-- Every DNS record in every zone, in one query
SELECT z.name AS zone, r.type, r.name, r.content, r.proxied
FROM cloudflare.zones.zones z, cloudflare.dns.records_by_zone(z.id) r
ORDER BY zone, r.type, r.name;
```

> **Read-only, with your own credentials.** Every query is answered live from
> Cloudflare's REST and GraphQL APIs using the API token in your DuckDB secret;
> nothing is cached or stored by the worker. The HTTP client issues `GET`s and
> GraphQL *queries* only — there is no code path that creates, changes, or
> deletes anything in your account. Values from SQL that land in a URL path are
> percent-encoded, so an id cannot traverse to a different endpoint.

## Run

```sql
INSTALL vgi FROM community; LOAD vgi;

ATTACH 'cloudflare' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-cloudflare vgi-cloudflare');

CREATE SECRET cf (TYPE cloudflare, api_token '<your-api-token>');
```

`uvx` fetches and caches the worker on first use — there is nothing to install,
and the working directory doesn't matter. Pin a tag for a deployment, so the
worker cannot change under you:

```sql
ATTACH 'cloudflare' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-cloudflare@v0.1.0 vgi-cloudflare');
```

The catalog is named `cloudflare`; that is the name you `ATTACH` (an alias with
`AS cf` works too, but the first argument must be `cloudflare`). Objects live in
product schemas, so names are three-part: `cloudflare.<schema>.<object>`.

Prefix the `LOCATION` with `launch:` to share one warm worker across queries,
cursors, and DuckDB processes instead of starting one per connection — the
catalog has ~2,000 objects, so a cold start costs a couple of seconds:

```sql
ATTACH 'cloudflare' (TYPE vgi,
  LOCATION 'launch:uvx --from git+https://github.com/Query-farm/vgi-cloudflare vgi-cloudflare');
```

From a clone, both entry points carry PEP-723 headers pinning their
dependencies, so they run with nothing installed:

```bash
uv run cloudflare_worker.py              # stdio  → LOCATION 'uv run cloudflare_worker.py'
uv run serve.py --port 8000              # HTTP   → LOCATION 'http://localhost:8000'
```

**This package is not published to PyPI** — install it from this repository.
Its dependencies (`vgi-python`, `vgi-rpc`, `httpx`, `pyarrow`) are all published.

### The API token

Create one at <https://dash.cloudflare.com/profile/api-tokens>. The **"Read all
resources"** template covers every schema with no write access; a narrower token
works too — objects it can't reach fail with Cloudflare's own permission error.
The legacy Global API Key is also accepted:
`CREATE SECRET cf (TYPE cloudflare, api_key '<key>', email '<email>')`.

### Developing

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

## A tour

```sql
-- Your zones, with their status and nameservers
SELECT name, status, name_servers FROM cloudflare.zones.zones ORDER BY name;

-- A table needs its scope id as a constant ...
SELECT type, count(*) AS n
FROM cloudflare.dns.records WHERE zone_id = '<zone-id>'
GROUP BY type ORDER BY n DESC;

-- ... or drive it per row from another table with the *_by_<parent> fan-out
SELECT a.name AS account, m.email, m.status
FROM cloudflare.accounts.accounts a, cloudflare.accounts.members_by_account(a.id) m;

-- Lookups fetch one object by id; an unknown id is no row, not an error
SELECT t.zone_id, z.name, z.status
FROM (VALUES ('<zone-id>'), ('not-a-zone')) t(zone_id)
LEFT JOIN LATERAL cloudflare.zones.zone(t.zone_id) z ON true;

-- Daily traffic for a zone; the date range in WHERE becomes the API's time filter
SELECT date, requests, cached_requests, threats
FROM cloudflare.analytics.http_requests_daily
WHERE zone_id = '<zone-id>' AND date BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'
ORDER BY date;

-- Radar: Internet-wide trends, no account needed. Defaults to the last 7 days.
SELECT summary_0 FROM cloudflare.radar.http_versions(date_range := '28d', location := 'US');
```

## Surface

1,471 functions and 553 tables across 24 product schemas, generated from
Cloudflare's OpenAPI description. Every object carries agent- and human-facing
documentation built from that spec: what it returns, which filters it requires,
how to call it, and examples.

### Four shapes

| Shape | Named | Called | Example |
|---|---|---|---|
| **Table** | plural noun | scanned; its scope ids (`zone_id`, `account_id`, …) are required constant filters | `cloudflare.dns.records WHERE zone_id = '…'` |
| **Fan-out** | `<table>_by_<parent>` | ids per input row, so it composes with other tables; 1 → N rows | `cloudflare.dns.records_by_zone(z.id)` |
| **Lookup** | singular noun | path ids as arguments; 0 or 1 row per call | `cloudflare.dns.record(zone_id, id)` |
| **Analytics** | `cloudflare.analytics.*` | GraphQL rollups; the time range comes from `WHERE` | `cloudflare.analytics.http_requests_daily` |

A lookup whose noun is already a table name takes `_by_id`
(`cloudflare.zones.rules_by_id`). Parameterless endpoints — mostly Radar reports
— are both a table and a function, so `SELECT * FROM cloudflare.radar.http_versions`
and `cloudflare.radar.http_versions(date_range := '28d')` both work.

### Schemas

| Schema | Covers | Tables | Functions |
|---|---|---:|---:|
| `zones` | Zones (domains) and per-zone configuration: settings, cache, rules, custom hostnames, waiting rooms | 30 | 113 |
| `dns` | DNS records, DNSSEC, secondary DNS, DNS Firewall, DNS analytics | 9 | 37 |
| `accounts` | Accounts, members, roles, API tokens, organizations, the token's user | 42 | 94 |
| `access` | Cloudflare Access: applications, policies, identity providers, service tokens | 34 | 98 |
| `zero_trust` | Gateway rules, DLP, devices and posture, DEX, tunnels, risk scoring | 41 | 151 |
| `security` | Threat intelligence, Security Center, URL scanner, brand protection, bots | 17 | 119 |
| `ssl` | Edge, custom, origin, and mTLS certificates; certificate authorities | 14 | 46 |
| `waf` | Legacy firewall: IP access rules, user-agent blocks, lockdowns | 3 | 7 |
| `api_gateway` | API Shield: discovered operations, schemas, validation | 7 | 22 |
| `load_balancing` | Load balancers, pools, monitors | 7 | 16 |
| `workers` | Workers scripts and deployments, Workflows, Queues, Pipelines, Containers | 34 | 118 |
| `storage` | R2 buckets and Data Catalog, D1, Hyperdrive | 5 | 18 |
| `ai` | AI Gateway, AI Search (AutoRAG), Workers AI models, Vectorize | 26 | 82 |
| `pages` | Pages projects, deployments, domains | 3 | 10 |
| `stream` | Stream video, live inputs, RealtimeKit, Calls/TURN | 8 | 43 |
| `email` | Email Routing and Email Security | 23 | 73 |
| `magic` | Magic Transit/WAN, BYOIP prefixes, interconnects | 33 | 101 |
| `logs` | Logpush jobs, datasets, audit logs | 12 | 31 |
| `alerting` | Notification policies, destinations, alert history | 6 | 18 |
| `billing` | Billable and pay-as-you-go usage | 2 | 4 |
| `registrar` | Domains registered with Cloudflare Registrar | 1 | 2 |
| `browser_rendering` | Headless browser sessions | 0 | 7 |
| `analytics` | GraphQL traffic, firewall, health-check, and Workers rollups | 6 | 6 |
| `radar` | Cloudflare Radar: Internet-wide traffic, attack, routing, and adoption trends | 190 | 255 |

`DESCRIBE cloudflare.dns.records` shows any object's columns; the
`vgi.doc_llm` / `vgi.doc_md` tags in `duckdb_tables()` / `duckdb_functions()`
explain it in prose.

## How filters work

**Scope ids are required, and must be constants.** A table's URL contains ids —
`/zones/{zone_id}/dns_records` — so a scan needs `zone_id = '…'` or
`zone_id IN (…)` in its `WHERE`. A subquery or a join can't supply them: DuckDB
plans the API scan before the other side's values exist, so the worker would
have nothing to put in the URL, and the query is rejected with an error naming
the missing column. That is what the fan-out functions are for — they take ids
*per row*, so `FROM zones z, records_by_zone(z.id)` covers every zone.

**Some filters run at Cloudflare.** Where the API accepts a filter on a column
(`type` and `name` on DNS records, `status` on zones, …), an equality on it is
sent on the query string. Every other predicate runs in DuckDB on the rows that
come back. The table docs list which columns are sent.

**Lateral calls are batched.** A fan-out or lookup receives a whole chunk of
input rows at once, fetches them concurrently (up to 8 requests in flight),
fetches a repeated id only once, and maps each output row back to the row that
produced it. A NULL id produces no rows; a lookup's 404 is "no row", while a
fan-out's 404 is an error (a missing parent is not an empty list).

## Analytics (GraphQL)

| Table | Scope | Grain | What |
|---|---|---|---|
| `http_requests_daily` | zone | day | requests, bytes, cache, encryption, page views, threats, uniques |
| `http_requests_hourly` | zone | hour | the same, hourly |
| `http_requests_adaptive` | zone | event time | sampled requests by country, host, method, status |
| `firewall_events` | zone | event time | WAF/firewall events by action, source, country, rule |
| `health_check_events` | zone | event time | origin health checks by hostname, region, origin, status |
| `workers_invocations` | account | event time | Worker requests, errors, subrequests by script and status |

The window comes from a range on the time column — `date BETWEEN …` or
`datetime >= …` — and defaults to a trailing window when there is none. The
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
into a table, a fan-out, or a lookup, so there is no per-endpoint code. A few
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
| `make test-mock` | real DuckDB `ATTACH` against a local fake Cloudflare: lateral, `LEFT JOIN LATERAL`, fan-out across pages | the `vgi` sqllogictest runner |
| `make test-stdio` | real DuckDB against real Cloudflare (`test/sql/live*.test`) | a token in `$CLOUDFLARE_API_TOKEN` or `~/cf-read-token.txt` |
| `vgi-lint simulate` | the agent-acceptance suite | a token file, and Claude |

The live tests skip cleanly without a token. They assert shapes and invariants
(every fan-out row carries the zone that produced it; a fan-out and the table
agree), not account-specific values, so they pass for any read token.

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
