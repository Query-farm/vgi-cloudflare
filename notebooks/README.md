# Cloudflare inventory notebook

[01 · Keep scopes and record identity](01_keep_scopes_and_record_identity.ipynb) retains unresolved zone IDs, follows all DNS pages and compares distinct record IDs with distinct DNS names. A count threshold changes only the review queue.

## Run

Requirements: Python 3.13 or 3.14, [uv](https://docs.astral.sh/uv/), Git, and network access for first dependency installation. This directory runs independently of the worker checkout. From this directory:

```bash
uv sync --locked
uv run --locked python execute.py
```

The runner starts a fresh kernel with this directory as cwd, then atomically replaces the notebook only after a successful execution. For an editor kernel:

```bash
uv run --locked python -m ipykernel install --user --name vgi-cloudflare-notebooks --display-name 'Python (vgi-cloudflare notebooks)'
```

Select that kernel, restart and Run All. The saved default contains output tables and figures. Helpers are folded; parameters, connection SQL and analytical SQL are visible. See [STANDARD.md](STANDARD.md).

## Portable launch and compatibility override

The visible connection is:

```sql
ATTACH 'cloudflare' AS cf (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-cloudflare@cd61c287abe0566facfc40f7b7a0844d59f95b85 --overrides worker-overrides.txt --with haybarn==1.5.5rc1 vgi-cloudflare');
```

uv fetches and caches that immutable source revision. No local source package or root `.venv` is discovered. `worker-overrides.txt` explicitly overrides the package's declared SDK range: **vgi-python 0.50.0 speaks protocol 3.2 and fails against Haybarn 2.0.0rc11's protocol 3.3**. The notebook instead uses 0.51.1, with RPC 0.53.1, httpx 0.28.1 and PyArrow 26.0.1. The worker's internal Haybarn filter evaluator remains 1.5.5rc1; it is separate from the notebook SQL engine. Worker source, root dependency declarations and root lockfile are unchanged.

This override was checked with the real pinned Git worker, unit tests, mock SQL tests and credential-free catalog checks. It is an explicit compatibility choice, not a new worker release. To upgrade, choose a remote revision and tested dependency set, then repeat these checks and both notebook executions.

## Synthetic API and credentials

`mock_cloudflare.py` is an unchanged copy of the pinned repository's shipped HTTP test server. The notebook starts its GET handler on an OS-assigned **127.0.0.1** port and sets `CLOUDFLARE_API_BASE` before attaching the worker. An observed-handler subclass records local requests and rejects POST. Real token and token-file environment settings are removed for the notebook kernel. The dummy SQL secret `fixture-only-not-a-token` has no live authority. The server and worker are closed at the end.

All zones, names, records and addresses are synthetic, including the intentionally repeated DNS names across pages. Domain strings are fixture labels; the notebook never resolves them. The fixture is not a simulation of Cloudflare permissions, DNS correctness or network exposure. It is never a fallback for a failing real API. Source and fixture fingerprints and execution UTC timestamps are shown in output.

The source scope is the five explicit input rows. Lookup misses and missing input IDs remain in the audit with NULL counts. DNS pages are fetched only for resolved scopes and materialized once; all charts reuse those rows. The local request audit verifies every resolved zone's two fixture pages. The count threshold is an illustrative prioritization rule, not a product limit or a security recommendation.

For a live account workflow, remove the local fixture setup, use Cloudflare's official API endpoint and provide a suitably scoped read token through the worker's secret interface. Do not save a real token in notebook SQL or outputs. Live account access is not required or exercised by Run All. Account visibility and plan-dependent resources require separate validation against the intended scope.

## One-parameter experiment

```bash
uv run --locked python execute.py --parameters '{"REVIEW_MIN_RECORDS":6}' --output-dir artifacts/threshold-6
```

Compare the review queue and first figure. The source fingerprint, unresolved count, request-page audit and name-versus-ID chart should remain unchanged. Reset to **4** afterward. The runner requires a separate output directory for overrides, rejects a source-directory destination and unknown parameter names, and copies branding, fixture and setup companions to experiments.

## Validation notes

The default and a detached notebooks-only experiment were executed through Haybarn 2.0.0rc11 and all four chart outputs inspected. Source checks cover unit tests, lint and formatting. Wire checks use the published `haybarn-unittest` runner with temporary test copies replacing the legacy `require vgi` line with explicit `INSTALL`/`LOAD`; the original tests are unchanged. Credential-free catalog and local mock tests exercise the compatible runtime. Live account suites remain outside this fixture delivery. Raw logs and extracted charts are ignored; outputs remain embedded in the notebook.

The full metadata audit on the unchanged pinned worker fails: score 45/100, 11 VGI901 errors in example SQL using unquoted reserved function names (`group`, `references`, `default`, `all`), 4,504 warnings and 339 informational findings. The notebook uses none of those failing examples. These upstream findings are recorded separately from the passing notebook, unit, mock and catalog checks; the delivery does not claim a clean metadata audit.
