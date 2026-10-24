"""Descriptor-driven Cloudflare GraphQL Analytics tables.

The Analytics API is one GraphQL endpoint with a very regular shape::

    viewer {
      zones(filter: {zoneTag: $tag}) {          # or accounts(filter:{accountTag})
        <dataset>(limit, filter:{<t>_geq,<t>_leq}, orderBy:[<t>_ASC]) {
          dimensions { <time> <dims...> }
          count
          sum { <metrics...> }
          avg { <metrics...> }
          uniq { uniques }
        }
      }
    }

So one :class:`GraphQLDataset` descriptor + one generic runtime cover every
``*Groups`` dataset. Each becomes a function in the ``analytics`` schema of its
``zone_id`` / ``account_id`` (per input row, so it works laterally) with a required
``since`` / ``until`` window. A window past the API's group cap is paged by time,
one reply per page round, continued with ``more=True`` / ``resume()``.

    SELECT date, requests, threats FROM cloudflare.analytics.http_requests_daily(
      '<zone>', since := DATE '2024-01-01', until := DATE '2024-01-31');

    SELECT z.name, sum(f.count) FROM cloudflare.zones.zones z,
      cloudflare.analytics.firewall_events(z.id, since := TIMESTAMPTZ '2024-01-01 00:00:00+00',
                                           until := TIMESTAMPTZ '2024-01-02 00:00:00+00') f
    GROUP BY 1;
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg, SecretLookupEntry
from vgi.metadata import FunctionExample
from vgi.protocol import BindResponse
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
)
from vgi.table_in_out_function import RowTransformFunction
from vgi_rpc.log import Level

from .client import CloudflareError, graphql_post
from .descriptor import Example, comment_field
from .docs import result_columns_schema
from .runtime import FanOutState, _auth_from_secrets, emit_round, example_tags
from .secret import CLOUDFLARE_SECRET_TYPE

ANALYTICS_SCHEMA = "analytics"

#: Concurrent GraphQL queries per reply. Analytics queries are heavier than REST
#: reads and have their own, smaller quota (~300 queries per 5 minutes).
GRAPHQL_CONCURRENCY = 4

#: Scope ids per call (``Meta.input_rows_per_call``): a reply runs one time page of
#: each, so a turn is at most two waves of ``GRAPHQL_CONCURRENCY`` queries, and a
#: lateral over many zones can't spend the quota in one turn before replying.
GRAPHQL_ROWS_PER_CALL = 8
_TS = pa.timestamp("us", tz="UTC")


# ---------------------------------------------------------------------------
# Descriptor model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dimension:
    """A GraphQL ``dimensions`` field exposed as a (group-by) column."""

    name: str
    type: pa.DataType = pa.string()
    doc: str = ""
    field: str | None = None  # GraphQL field; defaults to ``name``

    @property
    def gql(self) -> str:
        return self.field or self.name


@dataclass(frozen=True, slots=True)
class Metric:
    """An aggregated metric. ``section`` is ``sum``/``avg``/``min``/``max``/``uniq``/``count``.

    ``count`` is a scalar at the group level (``field`` is ignored); the others
    are nested under their section (``sum { requests }``).
    """

    name: str
    section: str
    field: str | None = None  # GraphQL field within the section; defaults to ``name``
    type: pa.DataType = pa.int64()
    doc: str = ""

    @property
    def gql(self) -> str:
        return self.field or self.name


@dataclass(frozen=True, slots=True)
class GraphQLDataset:
    """A Cloudflare GraphQL ``*Groups`` analytics dataset."""

    name: str  # SQL table name
    dataset: str  # GraphQL dataset field, e.g. "firewallEventsAdaptiveGroups"
    scope: str  # "zone" | "account"
    time_field: str  # "date" | "datetime"
    metrics: tuple[Metric, ...]
    dimensions: tuple[Dimension, ...] = ()
    description: str = ""
    default_range_days: int = 7
    limit: int = 10_000
    cardinality_estimate: int = 100
    cardinality_max: int = 100_000

    @property
    def scope_id(self) -> str:
        return "zone_id" if self.scope == "zone" else "account_id"

    @property
    def time_type(self) -> pa.DataType:
        return pa.date32() if self.time_field == "date" else _TS


# ---------------------------------------------------------------------------
# Query building + row extraction
# ---------------------------------------------------------------------------


def build_query(desc: GraphQLDataset) -> str:
    """Build the GraphQL query string for a dataset descriptor."""
    plural = "zones" if desc.scope == "zone" else "accounts"
    tag_field = "zoneTag" if desc.scope == "zone" else "accountTag"
    gql_time_type = "Date" if desc.time_field == "date" else "Time"

    sections: dict[str, list[str]] = {}
    has_count = False
    for m in desc.metrics:
        if m.section == "count":
            has_count = True
        else:
            sections.setdefault(m.section, []).append(m.gql)

    dim_fields = " ".join([desc.time_field] + [d.gql for d in desc.dimensions])
    parts = [f"dimensions {{ {dim_fields} }}"]
    if has_count:
        parts.append("count")
    parts += [f"{section} {{ {' '.join(fields)} }}" for section, fields in sections.items()]
    body = "\n          ".join(parts)

    return (
        f"query ($tag: String!, $since: {gql_time_type}!, $until: {gql_time_type}!) {{\n"
        f"  viewer {{\n"
        f"    {plural}(filter: {{{tag_field}: $tag}}) {{\n"
        f"      {desc.dataset}(\n"
        f"        limit: {desc.limit},\n"
        f"        filter: {{{desc.time_field}_geq: $since, {desc.time_field}_leq: $until}},\n"
        f"        orderBy: [{desc.time_field}_ASC]\n"
        f"      ) {{\n"
        f"        {body}\n"
        f"      }}\n"
        f"    }}\n"
        f"  }}\n"
        f"}}"
    )


def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _output_schema(desc: GraphQLDataset) -> pa.Schema:
    fields = [
        comment_field(desc.scope_id, pa.string(), f"{desc.scope} id (required filter).", nullable=False),
        comment_field(desc.time_field, desc.time_type, "Time bucket of the rollup.", nullable=False),
    ]
    fields += [comment_field(d.name, d.type, d.doc) for d in desc.dimensions]
    fields += [comment_field(m.name, m.type, m.doc) for m in desc.metrics]
    return pa.schema(fields)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class _NoArgs:
    pass


def _window_type(desc: GraphQLDataset) -> type:
    return date if desc.time_field == "date" else datetime


def _dataset_args_class(desc: GraphQLDataset) -> type:
    """The scope id per row (positional), and the time window as required named args."""
    bucket = "day" if desc.time_field == "date" else "time"
    kind = _window_type(desc)
    arrow = pa.date32() if desc.time_field == "date" else _TS
    return dataclasses.make_dataclass(
        f"{_camel(desc.name)}Args",
        [
            (
                desc.scope_id,
                Annotated[str, Arg(0, doc=f"The {desc.scope} to report on.", arrow_type=pa.string())],
            ),
            (
                "since",
                Annotated[
                    kind, Arg("since", doc=f"First {bucket} of the window (inclusive).", arrow_type=arrow)
                ],
            ),
            (
                "until",
                Annotated[
                    kind, Arg("until", doc=f"Last {bucket} of the window (inclusive).", arrow_type=arrow)
                ],
            ),
        ],
        frozen=True,
        slots=True,
    )


def _fetch_group_page(
    auth: Any, query: str, desc: GraphQLDataset, tag: str, since: str, until: str, out: OutputCollector
) -> tuple[list[dict[str, Any]], str | None]:
    """One time page of groups for a scope id, and where the next page starts.

    The API returns at most ``desc.limit`` groups and has no cursor, but it orders
    them by time. So a full page is complete for every bucket before its last:
    those are kept, and the next page starts at that last bucket. A single bucket
    that fills a page can't be paged this way: it is kept as returned, with a
    warning, and paging resumes after it. ``None`` means the window is done.
    """
    plural = "zones" if desc.scope == "zone" else "accounts"
    data = _post(auth, query, {"tag": tag, "since": since, "until": until}, desc)
    scoped = (data.get("viewer") or {}).get(plural) or []
    got = scoped[0].get(desc.dataset, []) if scoped else []
    if len(got) < desc.limit:
        return got, None
    last = _bucket(got[-1], desc)
    resume = _fmt(last, desc.time_field)
    complete = [g for g in got if _bucket(g, desc) != last]
    if complete and resume > since:
        return complete, resume
    out.client_log(
        Level.WARN,
        f"{desc.name}: {resume} alone returned the {desc.limit}-group limit; results for it may be truncated",
    )
    resume = _after(resume, desc.time_field)
    return got, (None if resume > until else resume)


def _bucket(group: dict[str, Any], desc: GraphQLDataset) -> Any:
    return (group.get("dimensions") or {}).get(desc.time_field)


def _after(bucket: str, time_field: str) -> str:
    """The smallest window start past ``bucket`` (a day, or a second, later)."""
    if time_field == "date":
        return (date.fromisoformat(bucket) + timedelta(days=1)).isoformat()
    return _fmt(datetime.fromisoformat(bucket.replace("Z", "+00:00")) + timedelta(seconds=1), time_field)


def make_graphql_function(desc: GraphQLDataset) -> type[RowTransformFunction]:
    """Build a lateral function for one GraphQL analytics dataset.

    ``http_requests_daily(zone_id, since := DATE '…', until := DATE '…')``: the scope
    id comes per input row (a literal, or columns of another relation), and the time
    window is required — a window defaulted silently would truncate results that a
    ``WHERE`` range asked for.
    """
    output_schema = _output_schema(desc)
    query = build_query(desc)
    dim_by_name = {d.name: d for d in desc.dimensions}
    metric_by_name = {m.name: m for m in desc.metrics}
    args_class = _dataset_args_class(desc)

    def fan_out(params: ProcessParams[Any], state: FanOutState, out: OutputCollector) -> None:
        auth = _auth_from_secrets(params.secrets)
        until = _fmt(params.args.until, desc.time_field)
        out_cols = list(params.output_schema.names)

        def fetch(key: tuple[str, ...], since: str) -> tuple[list[Any], str | None]:
            return _fetch_group_page(auth, query, desc, key[0], since, until, out)

        def row(key: tuple[str, ...], g: Any) -> list[Any]:
            dims = g.get("dimensions") or {}
            return [_extract(c, g, dims, key[0], desc, dim_by_name, metric_by_name) for c in out_cols]

        emit_round(state, out, params.output_schema, fetch, row, concurrency=GRAPHQL_CONCURRENCY)

    class _Dataset(RowTransformFunction[args_class, FanOutState]):
        DATASET: ClassVar[GraphQLDataset] = desc
        FIXED_SCHEMA: ClassVar[pa.Schema] = output_schema

        class Meta:
            name = desc.name
            description = desc.description or f"Cloudflare {desc.dataset} analytics."
            categories = ["cloudflare", "analytics", desc.scope]
            required_secrets = [SecretLookupEntry(secret_type=CLOUDFLARE_SECRET_TYPE)]
            examples = [FunctionExample(sql=e.sql, description=e.description) for e in dataset_examples(desc)]
            tags = {**example_tags(examples), **dataset_tags(desc)}
            input_rows_per_call = GRAPHQL_ROWS_PER_CALL

        @classmethod
        def on_bind(cls, params: BindParams[Any]) -> BindResponse:
            return BindResponse(output_schema=output_schema)

        @classmethod
        def initial_state(cls, params: ProcessParams[Any]) -> FanOutState:
            return FanOutState()

        @classmethod
        def process(
            cls, params: ProcessParams[Any], state: FanOutState, batch: pa.RecordBatch, out: OutputCollector
        ) -> None:
            keys = [None if v is None else (str(v),) for v in batch.column(desc.scope_id).to_pylist()]
            state.start(keys, _fmt(params.args.since, desc.time_field))
            fan_out(params, state, out)

        @classmethod
        def resume(cls, params: ProcessParams[Any], state: FanOutState, out: OutputCollector) -> None:
            fan_out(params, state, out)

    _Dataset.__name__ = f"{_camel(desc.name)}Dataset"
    _Dataset.__qualname__ = _Dataset.__name__
    return _Dataset


def _post(auth: Any, query: str, binding: dict[str, Any], desc: GraphQLDataset) -> dict[str, Any]:
    variables = {"tag": binding["tag"], "since": binding["since"], "until": binding["until"]}
    try:
        return graphql_post(auth, query, variables)
    except CloudflareError as exc:
        if "does not have access to the path" in str(exc):
            raise CloudflareError(
                f"{desc.name}: Cloudflare GraphQL dataset '{desc.dataset}' is not available to "
                f"{desc.scope} '{binding['tag']}' — it is usually limited to certain Cloudflare plans "
                f"(or the token lacks Analytics read). Details: {exc}"
            ) from exc
        raise


def _camel(name: str) -> str:
    return "".join(p.capitalize() for p in name.split("_"))


def _fmt(value: Any, time_field: str) -> str:
    if time_field == "date":
        d = _to_date(value)
        return (d or date.today()).isoformat()
    dt = _to_datetime(value) or datetime.now(UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract(
    col: str,
    group: dict[str, Any],
    dims: dict[str, Any],
    tag: str,
    desc: GraphQLDataset,
    dim_by_name: dict[str, Dimension],
    metric_by_name: dict[str, Metric],
) -> Any:
    if col == desc.scope_id:
        return tag
    if col == desc.time_field:
        return _to_date(dims.get(col)) if desc.time_field == "date" else _to_datetime(dims.get(col))
    if col in dim_by_name:
        return dims.get(dim_by_name[col].gql)
    m = metric_by_name[col]
    if m.section == "count":
        return group.get("count")
    section = group.get(m.section) or {}
    return section.get(m.gql)


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

_I64 = pa.int64()
_F64 = pa.float64()

# Shared HTTP-request sum metrics (httpRequests1dGroups / httpRequests1hGroups).
_HTTP_METRICS: tuple[Metric, ...] = (
    Metric("requests", "sum", doc="Total HTTP requests."),
    Metric("bytes", "sum", doc="Total bytes served."),
    Metric("cached_requests", "sum", "cachedRequests", doc="Requests served from cache."),
    Metric("cached_bytes", "sum", "cachedBytes", doc="Bytes served from cache."),
    Metric("encrypted_requests", "sum", "encryptedRequests", doc="HTTPS requests."),
    Metric("encrypted_bytes", "sum", "encryptedBytes", doc="HTTPS bytes."),
    Metric(
        "page_views", "sum", "pageViews", doc="Requests Cloudflare counts as page views (HTML responses)."
    ),
    Metric("threats", "sum", doc="Requests classified as threats."),
    Metric("unique_visitors", "uniq", "uniques", doc="Estimated unique visitors."),
)

DATASETS: tuple[GraphQLDataset, ...] = (
    GraphQLDataset(
        name="http_requests_daily",
        dataset="httpRequests1dGroups",
        scope="zone",
        time_field="date",
        metrics=_HTTP_METRICS,
        description="Daily HTTP request totals per zone.",
        default_range_days=30,
        cardinality_estimate=30,
    ),
    GraphQLDataset(
        name="http_requests_hourly",
        dataset="httpRequests1hGroups",
        scope="zone",
        time_field="datetime",
        metrics=_HTTP_METRICS,
        description="Hourly HTTP request totals per zone.",
        default_range_days=3,
        cardinality_estimate=72,
    ),
    GraphQLDataset(
        name="http_requests_adaptive",
        dataset="httpRequestsAdaptiveGroups",
        scope="zone",
        time_field="datetime",
        dimensions=(
            Dimension("client_country", field="clientCountryName", doc="Visitor country (ISO-2)."),
            Dimension("host", field="clientRequestHTTPHost", doc="Requested host."),
            Dimension("method", field="clientRequestHTTPMethodName", doc="HTTP method."),
            Dimension("status", pa.int64(), "Edge response status code.", field="edgeResponseStatus"),
        ),
        metrics=(
            Metric("count", "count", doc="Sampled request count."),
            Metric("response_bytes", "sum", "edgeResponseBytes", doc="Edge response bytes."),
        ),
        description="HTTP requests broken down by country, host, method, and status.",
        default_range_days=1,
        cardinality_estimate=500,
        cardinality_max=1_000_000,
    ),
    GraphQLDataset(
        name="firewall_events",
        dataset="firewallEventsAdaptiveGroups",
        scope="zone",
        time_field="datetime",
        dimensions=(
            Dimension("action", doc="Mitigation action (block, challenge, allow, ...)."),
            Dimension("source", doc="Firewall product that fired (waf, firewallrules, ...)."),
            Dimension("client_country", field="clientCountryName", doc="Client country (ISO-2)."),
            Dimension("host", field="clientRequestHTTPHost", doc="Requested host."),
            Dimension("rule_id", field="ruleId", doc="Matched rule id."),
        ),
        metrics=(Metric("count", "count", doc="Number of firewall events."),),
        description="Firewall / WAF events grouped by action, source, country, and rule.",
        default_range_days=1,
        cardinality_estimate=500,
        cardinality_max=1_000_000,
    ),
    GraphQLDataset(
        name="health_check_events",
        dataset="healthCheckEventsAdaptiveGroups",
        scope="zone",
        time_field="datetime",
        dimensions=(
            Dimension("fqdn", doc="Monitored hostname."),
            Dimension("region", doc="Probing region."),
            Dimension("health_status", field="healthStatus", doc="Reported health status."),
            Dimension("origin_ip", field="originIP", doc="Origin IP probed."),
        ),
        metrics=(
            Metric("count", "count", doc="Number of health-check events."),
            Metric("avg_rtt_ms", "avg", "rttMs", type=_F64, doc="Average round-trip time (ms)."),
        ),
        description="Load-balancer health-check events grouped by host, region, and status.",
        default_range_days=1,
        cardinality_estimate=200,
    ),
    GraphQLDataset(
        name="workers_invocations",
        dataset="workersInvocationsAdaptive",
        scope="account",
        time_field="datetime",
        dimensions=(
            Dimension("script_name", field="scriptName", doc="Worker script name."),
            Dimension("status", doc="Invocation status (success, scriptThrewException, ...)."),
        ),
        metrics=(
            Metric("requests", "sum", doc="Worker requests."),
            Metric("errors", "sum", doc="Worker errors."),
            Metric("subrequests", "sum", doc="Subrequests issued."),
        ),
        description="Workers invocations per script and status (account-scoped).",
        default_range_days=1,
        cardinality_estimate=200,
    ),
)


#: Navigation sections of the analytics schema: dataset name -> category slug.
_CATEGORY = {
    "http_requests_daily": "http-traffic",
    "http_requests_hourly": "http-traffic",
    "http_requests_adaptive": "http-traffic",
    "firewall_events": "security-events",
    "health_check_events": "health-checks",
    "workers_invocations": "workers",
}
CATEGORIES = [
    {
        "name": "http-traffic",
        "title": "HTTP Traffic",
        "description": "Request, bandwidth, threat, and cache rollups for a zone by day, hour, or dimension.",
    },
    {
        "name": "security-events",
        "title": "Security Events",
        "description": "WAF and firewall events for a zone, by action, source, country, and rule.",
    },
    {
        "name": "health-checks",
        "title": "Health Checks",
        "description": "Health-check probes of a zone's origins by hostname, region, origin IP, and status.",
    },
    {
        "name": "workers",
        "title": "Workers",
        "description": "Worker requests, errors, and subrequests for an account by script and status.",
    },
]
_PLAN_GATED = {"firewall_events", "health_check_events"}


def _window_sql(desc: GraphQLDataset, recent: bool) -> str:
    """A literal window: named arguments must be constants (no ``current_date``) in a lateral call."""
    if desc.time_field == "date":
        return (
            "since := DATE '2026-09-24', until := DATE '2026-09-30'"
            if recent
            else "since := DATE '2026-09-01', until := DATE '2026-09-07'"
        )
    if recent:
        return "since := TIMESTAMPTZ '2026-09-30 18:00:00+00', until := TIMESTAMPTZ '2026-10-01 00:00:00+00'"
    return "since := TIMESTAMPTZ '2026-09-01 00:00:00+00', until := TIMESTAMPTZ '2026-09-01 06:00:00+00'"


def dataset_examples(desc: GraphQLDataset) -> list[Example]:
    metric = desc.metrics[0].name
    dims = ", ".join(d.name for d in desc.dimensions[:2])
    t = desc.time_field
    group = f", {dims}" if dims else ""
    parent = "zones.zones" if desc.scope == "zone" else "accounts.accounts"
    return [
        Example(
            f"SELECT {t}{group}, {metric} FROM cloudflare.analytics.{desc.name}("
            f"'<{desc.scope_id}>', {_window_sql(desc, False)}) ORDER BY {t}",
            f"{metric} per {t}{' and ' + dims if dims else ''} for one {desc.scope} over a fixed window.",
        ),
        Example(
            f"SELECT p.name AS {desc.scope}, sum(x.{metric}) AS {metric} FROM cloudflare.{parent} p, "
            f"cloudflare.analytics.{desc.name}(p.id, {_window_sql(desc, True)}) x "
            f"GROUP BY ALL ORDER BY {metric} DESC",
            f"Total {metric} for every {desc.scope} over one window, one API call per {desc.scope}.",
        ),
    ]


def dataset_tags(desc: GraphQLDataset) -> dict[str, str]:
    """Agent/human docs, navigation, and search tags for one analytics dataset."""
    bucket = "day" if desc.time_field == "date" else "time bucket"
    llm = (
        f"{desc.description.rstrip('.')}, from Cloudflare's GraphQL Analytics API ({desc.dataset}). "
        f"Call it with a {desc.scope_id} — a literal, or per row of another query for a lateral join — and "
        f"a required window, since := … and until := … (inclusive {bucket}s, UTC; constants, not "
        f"expressions like current_date, in a lateral call). One row per {bucket}"
        + (f" and combination of {', '.join(d.name for d in desc.dimensions)}" if desc.dimensions else "")
        + f", with metrics {', '.join(m.name for m in desc.metrics)}. Large windows are paged over several "
        "API calls so results aren't truncated."
        + (" Only available on plans that include this dataset." if desc.name in _PLAN_GATED else "")
    )
    fields = [f"- `{desc.time_field}` — the {bucket} (UTC)."]
    fields += [f"- `{d.name}` — {d.doc or d.gql}" for d in desc.dimensions]
    fields += [f"- `{m.name}` — {m.doc or m.gql} (`{m.section}` in GraphQL)" for m in desc.metrics]
    md = f"# cloudflare.analytics.{desc.name}\n\n{llm}\n\n## Columns\n\n" + "\n".join(fields) + "\n"
    return {
        "vgi.doc_llm": llm,
        "vgi.doc_md": md,
        "vgi.result_columns_schema": result_columns_schema(_output_schema(desc)),
        "vgi.category": _CATEGORY.get(desc.name, "http-traffic"),
    }


def graphql_catalog_items() -> dict[str, list]:
    """Functions for the ``analytics`` schema (for build_catalog ``extra``)."""
    functions = [make_graphql_function(d) for d in DATASETS]
    examples = [dataset_examples(d)[0] for d in DATASETS[:3]]
    return {"functions": functions, "tables": [], "categories": CATEGORIES, "examples": examples}
