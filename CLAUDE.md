# CLAUDE.md — vgi-cloudflare architecture & development

A [VGI](https://query.farm) worker that exposes the **Cloudflare REST + GraphQL
APIs to DuckDB as SQL tables**. DuckDB is the query engine; the worker fetches
from Cloudflare on demand per query and streams Apache Arrow batches back.

## Quick start

```sql
INSTALL vgi FROM community; LOAD vgi;
ATTACH 'cloudflare' AS cf (TYPE vgi, LOCATION 'uv run cloudflare_worker.py');
CREATE SECRET cf (TYPE cloudflare, api_token '<token>');

SELECT id, name, status FROM cf.zones.zones;
SELECT name, type, content FROM cf.dns.records WHERE zone_id = '<zone>';
SELECT * FROM cf.dns.record('<zone>', '<record-id>');       -- lookup function
SELECT z.name, r.name FROM cf.zones.zones z, cf.dns.records_by_zone(z.id) r;  -- fan-out
SELECT date, requests, threats FROM cf.analytics.http_requests_daily
  WHERE zone_id = '<zone>' AND date >= DATE '2024-01-01';
```

> The first `ATTACH` argument **must be `cloudflare`** (the catalog name); the
> `AS cf` is just a local alias. A mismatched name → `Unknown catalog`. Tables
> live in ~24 product schemas, so qualify them three-part: `cf.<schema>.<table>`.

## Commands

```sh
uv sync                             # .venv from PyPI deps + the dev group
make test-unit                      # pytest (mocked HTTP — portable, no network)
make test-stdio                     # SQL sqllogictest, worker as subprocess (authoritative)
make test-mock                      # SQL lateral E2E against tests/mock_cloudflare.py (no token)
make codegen                        # regenerate the OpenAPI manifest (see below)
make lint                           # ruff check + format --check
make deploy                         # build, smoke-test, push, fly deploy

# Live tests against real Cloudflare (skipped without a token). The token comes
# from $CLOUDFLARE_API_TOKEN, else ~/cf-read-token.txt (CLOUDFLARE_API_TOKEN_FILE);
# a "Read all resources" token covers everything. Zone/account ids for the
# analytics tests are looked up from the token (tools/live_test_ids.py).
make test-stdio
```

`make test-stdio` / `make test-mock` need the sqllogictest runner from a build of
the DuckDB `vgi` extension (`$(VGI_BUILD_DIR)/test/unittest`; set `VGI_BUILD_DIR`).
`make test-unit` does not. Ad-hoc SQL works with `uvx haybarn-cli` (a DuckDB CLI
that can `LOAD vgi`).

## Architecture

**Descriptor-driven.** Every Cloudflare endpoint is described by a
`ResourceDescriptor`; one generic runtime turns each into a VGI table function.
Both the hand-written resources and the OpenAPI codegen produce the same
descriptor shape, so there is **no per-endpoint code**.

A descriptor's `kind` and `schema` decide its shape and namespace:
- `kind="list"` → a scannable **table**, plural noun (path params are pushdown
  filter columns). With path params it also gets a **fan-out function**
  `<table>_by_<parent>(ids…)` (`records_by_zone`), the same list once per input row.
- `kind="item"` → a **lookup function**, singular noun (`record`, `zone`; path
  params are args, in URL order); 0 or 1 row per input row.
- Lookups and fan-outs with path params are VGI `RowTransformFunction`s: args are
  per-row input columns, so one registration serves `f('id')`, `FROM t, f(t.id)`
  and `LATERAL`. A batch is fetched concurrently (deduped by key) and emitted once
  with `parent_rows` provenance. Lookups treat 404 as 0 rows; fan-outs don't.
- `schema` → the product family it lands in (`cloudflare.dns`, `cloudflare.access`, …).

```
ResourceDescriptor (descriptor.py)
        │  schema, kind, path, path_params, query_params, columns, pagination
        ├─ kind=list ─► make_resource_function()     ─► table function (+ scannable Table)
        │             make_lateral_list_function() ─► <table>_by_<parent>(ids) fan-out
        └─ kind=item ─► make_item_function()         ─► lookup function (args = path ids)
                        (runtime.py)
        ▼
build_catalog() (catalog.py) ─► Catalog with ~24 Schemas ─► CloudflareWorker
```

### Files

| File | Role |
|------|------|
| `vgi_cloudflare/worker.py` | `CloudflareWorker`: merges resources, builds the catalog, declares the `cloudflare` secret type. `main()` (stdio) / `main_http()` (HTTP) — the `vgi-cloudflare` / `vgi-cloudflare-http` entry points. |
| `cloudflare_worker.py`, `serve.py` | PEP-723 scripts (`uv run …`) wrapping `main` / `main_http`; their dependency headers must match `pyproject.toml`. |
| `vgi_cloudflare/descriptor.py` | `ResourceDescriptor`, `Column`, `PathParam`, `QueryParam`, `Pagination`. The codegen target. |
| `vgi_cloudflare/client.py` | httpx Cloudflare client: Bearer/legacy auth, page/cursor pagination, GraphQL POST, error envelopes. Module-global `httpx.Client` for pooling. |
| `vgi_cloudflare/runtime.py` | `make_resource_function()` (list) + `make_item_function()` (get-by-id, dynamic args dataclass). Pushdown/args → fetch; JSON extraction & Arrow coercion. |
| `vgi_cloudflare/secret.py` | The `cloudflare` DuckDB secret type (`api_token`, or `api_key`+`email`). |
| `vgi_cloudflare/resources.py` | Hand-written core list descriptors (`zones.zones`, `accounts.accounts`, `accounts.members`, `dns.records`). |
| `vgi_cloudflare/items.py` | Hand-written lookup descriptors (`zones.zone`, `dns.record`, …). |
| `vgi_cloudflare/generated.py` | Loads `resources_generated.json` into descriptors. |
| `vgi_cloudflare/resources_generated.json` | **Committed** OpenAPI-generated manifest (1,142 resources). |
| `vgi_cloudflare/graphql.py` | Descriptor-driven GraphQL analytics: `GraphQLDataset` + `make_graphql_function()` + the `DATASETS` list (`http_requests_daily`, `firewall_events`, …). All land in the `analytics` schema. |
| `vgi_cloudflare/catalog.py` | `build_catalog()` + `merge_resources()` (hand-written wins on name collisions; inherits spec docs from its generated twin). Catalog tags, agent-test tasks, executable examples. |
| `vgi_cloudflare/docs.py` | Per-object `doc_llm`/`doc_md`/examples/result schema/categories, generated from descriptors. |
| `vgi_cloudflare/schema_docs.py` | Hand-written catalog and schema docs. |
| `vgi_cloudflare/agent_tasks.json` + `vgi-agent-tests.yaml` | Public agent-test prompts (catalog tag) + their private graders. Written by `tools/agent_tasks.py`. |
| `tools/generate_resources.py` | OpenAPI → manifest codegen. |

### How a query runs

1. `WHERE zone_id = '...'` arrives as a **pushdown filter** at `on_init`.
2. `_resolve_bindings()` turns filters into one or more *bindings*: required
   **path params** (URL `{placeholders}`) come from equality/IN filters (error if
   absent); **query params** matching an output column get pushed onto the API
   query string. DuckDB drops the filters it pushes to a scan, so the worker
   applies every predicate to its output (`Meta.auto_apply_filters`); without it a
   filter on a non-API column is silently ignored.
3. `process()` pops a binding, **paginates** the Cloudflare API, maps each
   result item to the output columns (nested via `Column.source` dotted paths;
   objects/arrays JSON-encoded), and emits one Arrow batch per binding (all pages).

### Pagination

`Pagination.PAGE` (default — `?page/per_page`, `result_info.total_pages`),
`CURSOR` (`?cursor`, `result_info.cursor`), or `NONE` (single response). Each
`process()` call fully paginates one binding.

## OpenAPI codegen

`tools/generate_resources.py` reads `spec/openapi.json` (the Cloudflare spec,
~10MB, **git-ignored** — re-downloaded by the script) and emits the committed
`resources_generated.json` (~1,140 resources). It:

- generates **list** tables (`result` is an array) and **item** get-by-id
  functions (`result` is a single object, or a "wrapperless" body via
  `result_path=""`);
- flattens response schemas through `$ref`/`allOf`/`oneOf`/`anyOf` (so
  polymorphic resources like DNS records get a union of all variant fields);
- assigns each resource a **schema** via `AREA_TO_SCHEMA` (the path's product
  area → one of ~20 curated families; all 122 areas are mapped, no `misc`);
- derives names that are unique **within each schema**, shortened to drop the
  redundant schema prefix (`dns_records` in schema `dns` → `records`;
  `dns_record` → `record`);
- excludes paging/sorting params and dotted operator-variant params
  (`name.exact`, `comment.contains`, …) from filter columns.
- merges **path-item-level `parameters`** into each operation (OpenAPI
  inheritance — many `{account_id}`s are declared only there), and always takes
  path params from the URL template, so every `{placeholder}` is a path param in
  URL order (the runtime fills placeholders positionally).

Item names are the singular from the trailing path param (`zone_id` → `zone`),
distinct from the plural list tables. Lists are named first; an item whose noun is
already a table name becomes `<noun>_by_id` (uncountables like `rules`). Fan-out
names (`<table>_by_<parent>`) are derived at catalog build (`lateral_list_name`):
the last path param minus `_id`/`_name`/…, or the singular of the segment before a
generic `{id}`. Reserved words (`group`, `default`) are fine schema-qualified.

Regenerate after a spec update:
```sh
curl -sL -o spec/openapi.json https://raw.githubusercontent.com/cloudflare/api-schemas/main/openapi.json
.venv/bin/python tools/generate_resources.py --emit   # writes resources_generated.json
make test-unit      # regression guard: every descriptor must build a valid function
```

Hand-written resources in `resources.py` override generated ones of the same
name (`merge_resources`, primary wins), so curated schemas take precedence.

## GraphQL analytics

The Cloudflare GraphQL Analytics API is a separate, regular shape
(`viewer.zones|accounts → <dataset>(filter,orderBy) → dimensions/sum/avg/count/uniq`),
so `graphql.py` models it with its own descriptor (`GraphQLDataset`) and runtime
(`make_graphql_function`), all landing in the `analytics` schema. Each dataset is
scoped by a required `zone_id`/`account_id` filter; the time bucket
(`date`/`datetime`) is read from `WHERE` range bounds (`get_column_bounds`),
defaulting to a trailing window.

**Add a dataset** = one entry in `DATASETS`: the GraphQL `dataset` field, `scope`
(zone/account), `time_field`, `dimensions`, and `metrics` (each tagged with its
section — `sum`/`avg`/`count`/`uniq`/…). `build_query` assembles the query;
`_extract` maps groups back to columns.

### Field-name verification (important)

Unlike the REST OpenAPI spec (a public file we download), Cloudflare's GraphQL
schema is **only reachable with an authenticated token** — introspection included.
So dataset/dimension/metric field names in `DATASETS` are curated by hand, and a
name that isn't in the live schema makes Cloudflare **reject the whole query**.
Current confidence:

| Dataset | Field names |
|---------|-------------|
| all six (`http_requests_daily`/`_hourly`/`_adaptive`, `firewall_events`, `health_check_events`, `workers_invocations`) | **verified** against live introspection (2026-10) |

`firewall_events` and `health_check_events` are plan-gated: on a plan without
them Cloudflare answers "zone … does not have access to the path" (not a field
error). Adaptive datasets can hit the `limit` (10,000 groups) — there's no
GraphQL pagination yet, so a full result is silently truncated.

To verify or regenerate against the real schema, use a token with `Analytics:Read`:

```sh
export CLOUDFLARE_API_TOKEN=<token>

# 1. Introspect the schema (dataset + field names):
curl -s https://api.cloudflare.com/client/v4/graphql \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"{ __type(name:\"ZoneHttpRequests1dGroups\"){ fields{ name } } }"}'

# 2. Run a dataset end-to-end against a real zone (SQL E2E live test):
CLOUDFLARE_API_TOKEN=$CLOUDFLARE_API_TOKEN make test-stdio   # runs test/sql/live.test
```

A 400 with `"unknown field"` in the GraphQL error means a name in that dataset's
descriptor is wrong. The mocked unit tests (`tests/test_graphql.py`) lock in the
query-construction and extraction logic but **cannot** catch wrong field names —
only a live token can.

## Authentication

**Upstream (worker → Cloudflare):** a DuckDB secret of `TYPE cloudflare`. Each
table function declares `Meta.required_secrets = [SecretLookupEntry("cloudflare")]`;
the framework resolves it and the runtime reads
`params.secrets["cloudflare"]["api_token"]`. Prefer `api_token` (Bearer);
`api_key` + `email` (legacy Global Key) is also supported.

**Inbound (clients → worker, HTTP deploy):** configured entirely via `VGI_*`
env vars in `fly.toml` (JWT/OAuth) — no app code. Put real secrets in
`fly secrets`, never in `fly.toml`.

## Testing layers

1. **pytest** (`tests/`, mocked httpx) — fast, portable, no DuckDB. The
   `tests/harness.py` `invoke_resource()` drives the real bind→on_init→process
   lifecycle with `MockOutputCollector`, pushdown filters, and secrets.
2. **SQL sqllogictest** (`test/sql/*.test`) — the **authoritative** wire test;
   real DuckDB `ATTACH`es the worker. `catalog.test` covers schema/error paths
   with no token; `live.test` hits real Cloudflare (gated on `CLOUDFLARE_API_TOKEN`).

Run pytest with `--rootdir=. -o "addopts="` so it ignores any parent pytest config.

## Sharp edges

1. **`require vgi`, not `LOAD vgi`** in `.test` files — the DuckDB test runner
   has vgi linked; `LOAD` looks on disk and fails.
2. **ATTACH name must equal the catalog name** (`cloudflare`). Tables live in
   ~24 schemas — qualify them three-part: `cf.<schema>.<table>` (e.g.
   `cf.dns.records`). Hand-written resources override generated ones only when
   their `(schema, name)` matches, so keep both in sync (e.g. the curated DNS
   record table is `schema="dns", name="records"`).
3. **Path params are required filters.** Set `Table.required_filters`
   and resolve from pushdown in `on_init`; raise a clear error if absent.
4. **Build batches against `params.output_schema`, not `FIXED_SCHEMA`** — projection
   pushdown may drop columns; `from_pydict` against the full schema would mismatch.
5. **Query params only help if they match an output column** — DuckDB can only
   push filters on columns that exist. Non-matching query params are harmless no-ops.
6. **Dependencies resolve from PyPI** — no `[tool.uv.sources]`, so a fresh clone
   works. To develop against local vgi checkouts: `uv pip install -e ../vgi-python -e ../vgi-rpc`.
7. **The worker needs a DuckDB engine** (`vgi-python[haybarn]`): vgi ≥0.37 binds
   Filter v2 pushdown predicates with it. Without it every `WHERE` on a table fails
   with "No DuckDB-compatible engine is installed".
8. **One `out.emit()` per `process()` call** — collect a binding's pages into one
   batch. The test harness enforces this; the framework raises otherwise.
9. **Use `LOCATION 'launch:<cmd>'` for anything that opens many cursors** (vgi-lint,
   simulate): it shares one warm worker instead of cold-starting per connection
   (~2s each to build the catalog). A warm worker keeps serving the code it
   started with — after editing the worker, kill it (`pkill -f cloudflare_worker.py`)
   or it lingers until its idle timeout (300s).
10. **The vgi extension doesn't pass its environment to a stdio worker.** Put env
   vars in the `LOCATION` command (`env CLOUDFLARE_API_BASE=… uv run …`), as
   `make test-mock` does.
11. **vgi-lint** (`uvx --prerelease=allow --from vgi-lint-check vgi-lint lint "launch:uv run cloudflare_worker.py"`)
   passes `--fail-on error`. Example descriptions travel in the `vgi.example_queries`
   tag (`example_tags()`); DuckDB's native examples column is SQL-only. Its
   `setup_sql` runs before ATTACH, so it can't create a `cloudflare` secret —
   `--execute` lints credential-free (the worker refuses promptly, which passes).
   Never put a token in `setup_sql`: vgi-lint echoes failing setup SQL.

## Deployment (Fly.io)

`Dockerfile` installs the package (`.[serve]`, from PyPI) and runs
`vgi-cloudflare-http`. `fly.toml` scales to zero with a `/health` check.
`make deploy` chains build → smoke-test → push → `fly deploy`.
