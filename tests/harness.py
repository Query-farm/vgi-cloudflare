"""In-process driver for the VGI table-function lifecycle (no subprocess, no DuckDB).

Runs a resource function through bind -> global_init -> process with mocked HTTP,
capturing emitted batches into a pa.Table for assertions. Mirrors the lifecycle
the real DuckDB ``vgi`` extension drives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from vgi.arguments import Arguments
from vgi.filter_v2_builder import build_filter_batch, value_payload
from vgi.function_storage import BoundStorage, FunctionStorageSqlite
from vgi.invocation import FunctionType
from vgi.protocol import BindRequest, InitRequest


class MockOutputCollector:
    """Captures emitted batches for assertions."""

    def __init__(self, output_schema: pa.Schema) -> None:
        self.output_schema = output_schema
        self.batches: list[pa.RecordBatch] = []
        self.parent_rows: list[int] = []
        self.logs: list[tuple[str, str]] = []
        self._finished = False

    def emit(
        self,
        batch: pa.RecordBatch,
        partition_values: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
        parent_rows: list[int] | None = None,
    ) -> None:
        if parent_rows is not None:
            assert len(parent_rows) == batch.num_rows, "parent_rows must align with emitted rows"
            self.parent_rows.extend(parent_rows)
        self.batches.append(batch)

    def emit_pydict(self, data: dict[str, Any], schema: pa.Schema | None = None) -> None:
        self.emit(pa.RecordBatch.from_pydict(data, schema=schema or self.output_schema))

    def finish(self) -> None:
        self._finished = True

    @property
    def finished(self) -> bool:
        return self._finished

    def emit_client_log_message(self, msg: Any) -> None:  # pragma: no cover
        pass

    def client_log(self, level: Any, message: str, **extra: str) -> None:
        self.logs.append((str(level), message))


@dataclass(frozen=True)
class Filters:
    """ANDed ``column <op> value`` comparisons, encoded as Filter v2 once the schema is known.

    v2 column indexes are authoritative against the unprojected bind schema, so the
    batch is built lazily by ``invoke_resource`` after ``bind``.
    """

    comparisons: tuple[tuple[str, str, pa.Scalar], ...]

    def to_batch(self, schema: pa.Schema) -> pa.RecordBatch:
        predicates = []
        payloads = []
        for i, (col, op, value) in enumerate(self.comparisons):
            predicates.append(
                {
                    "id": f"query:{i}",
                    "revision": 0,
                    "mode": "required",
                    "source": "query",
                    "expression": {
                        "node": "comparison",
                        "op": op,
                        "left": {
                            "node": "column_ref",
                            "column_index": schema.get_field_index(col),
                            "column_name": col,
                        },
                        "right": {"node": "literal", "value_ref": i},
                    },
                }
            )
            payloads.append(value_payload(i, value))
        document = {
            "encoding": "vgi.filters.v2",
            "semantics": "vgi.duckdb.standard.v1",
            "kind": "snapshot",
            "predicates": predicates,
        }
        return build_filter_batch(document, payloads)


def comparisons(*items: tuple[str, str, Any]) -> Filters:
    """Build ANDed comparisons: comparisons(("date", "ge", d), ("zone_id", "eq", "z"))."""
    return Filters(tuple((c, op, v if isinstance(v, pa.Scalar) else pa.scalar(v)) for c, op, v in items))


def eq_filters(**columns: str) -> Filters:
    """Build ANDed equality filters: eq_filters(zone_id='z', type='A')."""
    return comparisons(*((col, "eq", val) for col, val in columns.items()))


def token_secrets(api_token: str = "test-token") -> dict[str, dict[str, pa.Scalar]]:
    return {"cloudflare": {"api_token": pa.scalar(api_token)}}


def invoke_resource(
    func_cls: type,
    *,
    pushdown_filters: Filters | None = None,
    join_keys: list[pa.RecordBatch] | None = None,
    output_schema: pa.Schema | None = None,
    secrets: dict[str, dict[str, pa.Scalar]] | None = None,
) -> pa.Table:
    """Run a resource table function end-to-end and return the emitted rows."""
    from vgi.table_function import ProcessParams

    args = Arguments(positional=(), named={})
    bind_req = BindRequest(
        function_name=func_cls.Meta.name,
        arguments=args,
        function_type=FunctionType.TABLE,
    )
    bind_resp = func_cls.bind(bind_req)
    out_schema = output_schema or bind_resp.output_schema
    filter_batch = pushdown_filters.to_batch(bind_resp.output_schema) if pushdown_filters else None

    storage = FunctionStorageSqlite(":memory:")
    original_storage = getattr(func_cls, "storage", None)
    func_cls.storage = storage
    try:
        init_req = InitRequest(
            bind_call=bind_req,
            output_schema=out_schema,
            pushdown_filters=filter_batch,
            join_keys=join_keys,
        )
        init_resp = func_cls.global_init(init_req)

        params = ProcessParams(
            args=func_cls._parse_arguments(func_cls.FunctionArguments, args),
            init_call=init_req,
            init_response=init_resp,
            output_schema=out_schema,
            settings={},
            secrets=secrets if secrets is not None else token_secrets(),
            storage=BoundStorage(storage, init_resp.execution_id),
        )

        state = func_cls.initial_state(params)
        out = MockOutputCollector(out_schema)
        while not out.finished:
            before = len(out.batches)
            func_cls.process(params, state, out)
            # Mirrors the framework: at most one data batch per process() call.
            assert len(out.batches) - before <= 1, "Only one data batch may be emitted per call"
    finally:
        if original_storage is None:
            del func_cls.storage
        else:
            func_cls.storage = original_storage

    return pa.Table.from_batches(out.batches, schema=out_schema)


def invoke_lateral(
    func_cls: type,
    rows: dict[str, list[Any]],
    *,
    secrets: dict[str, dict[str, pa.Scalar]] | None = None,
) -> tuple[pa.Table, list[int]]:
    """Drive a ``RowTransformFunction`` with one input batch of per-row path params.

    Returns the emitted rows and their ``parent_rows`` (input-row index per output row).
    """
    from vgi.table_function import ProcessParams

    input_batch = pa.RecordBatch.from_pydict({k: pa.array(v, type=pa.string()) for k, v in rows.items()})
    arguments = Arguments(positional=(), named={})
    bind_req = BindRequest(
        function_name=func_cls.Meta.name,
        arguments=arguments,
        function_type=FunctionType.TABLE,
        input_schema=input_batch.schema,
    )
    out_schema = func_cls.bind(bind_req).output_schema

    storage = FunctionStorageSqlite(":memory:")
    original_storage = getattr(func_cls, "storage", None)
    func_cls.storage = storage
    try:
        init_req = InitRequest(bind_call=bind_req, output_schema=out_schema)
        init_resp = func_cls.global_init(init_req)
        params = ProcessParams(
            args=func_cls._parse_arguments(func_cls.FunctionArguments, arguments, blended=True),
            init_call=init_req,
            init_response=init_resp,
            output_schema=out_schema,
            settings={},
            secrets=secrets if secrets is not None else token_secrets(),
            storage=BoundStorage(storage, init_resp.execution_id),
        )
        out = MockOutputCollector(out_schema)
        func_cls.process(params, func_cls.initial_state(params), input_batch, out)
    finally:
        if original_storage is None:
            del func_cls.storage
        else:
            func_cls.storage = original_storage

    assert len(out.batches) == 1, "a RowTransformFunction must emit exactly once per input batch"
    return pa.Table.from_batches(out.batches, schema=out_schema), out.parent_rows


def invoke_item(
    func_cls: type,
    *args: str,
    secrets: dict[str, dict[str, pa.Scalar]] | None = None,
) -> pa.Table:
    """Run a get-by-id item function as a literal call: ``record('a', 'b')``."""
    from vgi.table_in_out_function import RowTransformFunction

    if issubclass(func_cls, RowTransformFunction):
        names = [p.name for p in func_cls.DESCRIPTOR.path_params]
        table, _ = invoke_lateral(
            func_cls, {n: [a] for n, a in zip(names, args, strict=True)}, secrets=secrets
        )
        return table
    assert not args, f"{func_cls.Meta.name} takes no arguments"
    return invoke_resource(func_cls, secrets=secrets)
