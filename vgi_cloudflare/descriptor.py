"""Declarative model for a Cloudflare list endpoint exposed as a DuckDB table.

A :class:`ResourceDescriptor` is the single source of truth the runtime
(``runtime.py``) interprets to build a VGI ``TableFunctionGenerator``. Both the
hand-written resources (``resources.py``) and the OpenAPI codegen
(``tools/generate_resources.py``) ultimately produce these.

Design notes:
  * ``path`` may contain ``{placeholder}`` segments — each is a *required*
    filter column (a ``PathParam``). e.g. ``/zones/{zone_id}/dns_records``
    must be queried as ``... WHERE zone_id = '...'``. Path params are surfaced
    as string columns in the output so they can be selected and joined.
  * ``query_params`` are *optional* filter columns pushed onto the API query
    string when DuckDB supplies an equality filter. Filter pushdown is
    advisory: anything we can't push, DuckDB re-applies to returned rows.
  * ``columns`` map a result item (one JSON object from the ``result`` array)
    to typed Arrow columns. ``Column.source`` is a dotted path into the item;
    object/array values are JSON-encoded into string columns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pyarrow as pa


class Pagination(StrEnum):
    """How an endpoint paginates."""

    NONE = "none"  # single response, no paging
    PAGE = "page"  # ?page=&per_page=, result_info.total_pages (Cloudflare default)
    CURSOR = "cursor"  # ?cursor=&per_page=, result_info.cursor


def comment_field(name: str, type: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field:
    """Build a pa.Field carrying a ``comment`` metadata key (surfaced by DuckDB DESCRIBE)."""
    return pa.field(name, type, nullable=nullable, metadata={"comment": comment})


@dataclass(frozen=True, slots=True)
class Example:
    """An example query shown in the catalog, with what it demonstrates."""

    sql: str
    description: str


@dataclass(frozen=True, slots=True)
class Column:
    """One output column mapped from a result item."""

    name: str
    type: pa.DataType
    doc: str = ""
    #: Dotted path into the result item JSON; defaults to ``name``.
    source: str | None = None
    nullable: bool = True

    @property
    def json_path(self) -> tuple[str, ...]:
        return tuple((self.source or self.name).split("."))

    def to_field(self) -> pa.Field:
        return comment_field(self.name, self.type, self.doc, nullable=self.nullable)


@dataclass(frozen=True, slots=True)
class PathParam:
    """A required ``{placeholder}`` in the URL path, surfaced as a filter column."""

    name: str
    doc: str = ""
    #: Allowed values, when the spec enumerates them (declared on function arguments).
    choices: tuple[str, ...] = ()
    #: Regex the value must match, when the spec declares one.
    pattern: str | None = None

    def to_field(self) -> pa.Field:
        return comment_field(
            self.name,
            pa.string(),
            self.doc or f"Required filter: {self.name} (URL path parameter).",
            nullable=False,
        )


@dataclass(frozen=True, slots=True)
class QueryParam:
    """An optional filter column pushed onto the API query string."""

    name: str  # SQL column name
    type: pa.DataType
    doc: str = ""
    api_name: str | None = None  # query-string key; defaults to ``name``
    #: Allowed values, when the spec enumerates them (declared on function arguments).
    choices: tuple[str, ...] = ()
    #: Numeric bounds / regex the spec declares (declared on function arguments).
    ge: float | None = None
    le: float | None = None
    pattern: str | None = None

    @property
    def key(self) -> str:
        return self.api_name or self.name

    def to_field(self) -> pa.Field:
        return comment_field(self.name, self.type, self.doc, nullable=True)


@dataclass(frozen=True, slots=True)
class ResourceDescriptor:
    """A Cloudflare endpoint exposed as a DuckDB table or table function.

    ``kind`` selects how path params are supplied and how many rows come back:

    * ``"list"`` (default) — a collection endpoint. Path params are *filter
      columns* (resolved from `WHERE` pushdown); the endpoint is paginated and
      yields many rows. Registered as a scannable ``Table``.
    * ``"item"`` — a get-one-by-id endpoint. Path params become *required
      function arguments* (``cf.dns_record(zone_id, dns_record_id)``); the
      response is a single object yielding **0 or 1 row** (0 when the API
      returns ``result: null`` / 404). Registered as a function only — ideal for
      correlated/lateral joins to enrich a set of ids. Use ``result_path=""``
      for "wrapperless" endpoints whose body *is* the object (no ``result`` key).
    """

    name: str  # SQL table name, e.g. "records"
    path: str  # URL template, e.g. "/zones/{zone_id}/dns_records"
    columns: tuple[Column, ...]
    schema: str = "main"  # DuckDB schema (product family), e.g. "dns"
    kind: str = "list"  # "list" | "item"
    path_params: tuple[PathParam, ...] = ()
    query_params: tuple[QueryParam, ...] = ()
    pagination: Pagination = Pagination.PAGE
    result_path: str = "result"  # JSON key holding the result; "" = whole body is the row
    per_page: int = 100
    description: str = ""
    #: Long-form endpoint documentation (Markdown) from the OpenAPI operation.
    doc: str = ""
    #: The OpenAPI operation tag (Cloudflare's product grouping), e.g. "DNS Records for a Zone".
    api_tag: str = ""
    #: The API product area (URL segment) — the navigation category within the schema.
    area: str = ""
    #: Query parameters always sent unless overridden (e.g. Radar's ``dateRange=7d``).
    default_query: tuple[tuple[str, str], ...] = ()
    categories: tuple[str, ...] = ()
    cardinality_estimate: int = 100
    cardinality_max: int = 1_000_000
    examples: tuple[Example, ...] = ()

    def output_fields(self) -> list[pa.Field]:
        """Path-param columns first (join keys), then mapped result columns."""
        return [p.to_field() for p in self.path_params] + [c.to_field() for c in self.columns]

    def output_schema(self) -> pa.Schema:
        return pa.schema(self.output_fields())

    def column_index(self) -> dict[str, Column]:
        return {c.name: c for c in self.columns}
