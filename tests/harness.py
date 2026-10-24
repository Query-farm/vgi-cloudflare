"""In-process driver for the VGI table-function lifecycle (no subprocess, no DuckDB).

Runs a resource function through bind -> global_init -> process with mocked HTTP,
capturing emitted batches into a pa.Table for assertions. Mirrors the lifecycle
the real DuckDB ``vgi`` extension drives, including continued table-in-out
output: a reply that says ``more`` is followed by a ``resume()`` turn (the
client's continuation tick), with the state round-tripped through its codec
between turns as the HTTP state token carries it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
from vgi.arguments import Arguments
from vgi.filter_v2_builder import build_filter_batch, value_payload
from vgi.function_storage import BoundStorage, FunctionStorageSqlite
from vgi.invocation import FunctionType
from vgi.protocol import BindRequest, InitRequest, _is_state_codec

#: Turns after which a continued exchange is assumed to be stuck.
MAX_TURNS = 10_000


class MockOutputCollector:
    """Captures emitted batches for assertions."""

    def __init__(self, output_schema: pa.Schema) -> None:
        self.output_schema = output_schema
        self.batches: list[pa.RecordBatch] = []
        self.parent_rows: list[int] = []
        self.logs: list[tuple[str, str]] = []
        self.more = False  # whether the last emit said more output follows
        self._finished = False

    def emit(
        self,
        batch: pa.RecordBatch,
        partition_values: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
        parent_rows: list[int] | None = None,
        more: bool = False,
    ) -> None:
        if parent_rows is not None:
            assert len(parent_rows) == batch.num_rows, "parent_rows must align with emitted rows"
            self.parent_rows.extend(parent_rows)
        self.batches.append(batch)
        self.more = more

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
    max_calls: int | None = None,
) -> pa.Table:
    """Run a resource table function end-to-end and return the emitted rows.

    ``max_calls`` stops after that many ``process()`` calls, as a LIMIT does.
    """
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
        n_calls = 0
        while not out.finished and (max_calls is None or n_calls < max_calls):
            n_calls += 1
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


@dataclass
class Reply:
    """One reply of a table-in-out exchange turn."""

    batch: pa.RecordBatch
    parent_rows: list[int]
    more: bool
    #: Upstream requests made during this turn (when the test passes ``calls``).
    requests: int = 0


@dataclass
class LateralRun:
    """Every reply to every input batch of one exchange stream."""

    schema: pa.Schema
    #: ``replies[b]`` = the replies to input batch ``b``, in order.
    replies: list[list[Reply]] = field(default_factory=list)
    logs: list[tuple[str, str]] = field(default_factory=list)

    def table(self, batch: int = 0) -> pa.Table:
        return pa.Table.from_batches([r.batch for r in self.replies[batch]], schema=self.schema)

    def parents(self, batch: int = 0) -> list[int]:
        return [p for r in self.replies[batch] for p in r.parent_rows]


def run_lateral(
    func_cls: type,
    inputs: dict[str, list[Any]] | list[dict[str, list[Any]]],
    *,
    named: dict[str, Any] | None = None,
    secrets: dict[str, dict[str, pa.Scalar]] | None = None,
    calls: list[Any] | None = None,
    max_replies: int | None = None,
) -> LateralRun:
    """Drive a ``RowTransformFunction`` over one exchange stream, as the client does.

    Each input batch goes to ``process()``; while the last reply said ``more``, a
    tick goes to ``resume()``. Every turn must emit exactly one batch, and between
    turns the state is serialized and restored through its codec (the HTTP path,
    where it rides the state token), so state that isn't serializable fails here.
    ``max_replies`` stops ticking after that many replies to the last batch, like a
    LIMIT; ``calls`` (a list a mock appends to) counts requests per reply.
    """
    from vgi.table_function import ProcessParams

    batches = [inputs] if isinstance(inputs, dict) else inputs
    input_batches = [
        pa.RecordBatch.from_pydict({k: pa.array(v, type=pa.string()) for k, v in rows.items()})
        for rows in batches
    ]
    arguments = Arguments(
        positional=(),
        named={k: v if isinstance(v, pa.Scalar) else pa.scalar(v) for k, v in (named or {}).items()},
    )
    bind_req = BindRequest(
        function_name=func_cls.Meta.name,
        arguments=arguments,
        function_type=FunctionType.TABLE,
        input_schema=input_batches[0].schema,
    )
    out_schema = func_cls.bind(bind_req).output_schema
    run = LateralRun(schema=out_schema)

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
        state = func_cls.initial_state(params)

        def turn(batch: pa.RecordBatch | None) -> Reply:
            nonlocal state
            out = MockOutputCollector(out_schema)
            before = len(calls) if calls is not None else 0
            if batch is None:
                func_cls.resume(params, state, out)
            else:
                func_cls.process(params, state, batch, out)
            assert len(out.batches) == 1, f"a turn must emit exactly one batch, got {len(out.batches)}"
            run.logs.extend(out.logs)
            if out.more:
                assert func_cls.has_resume_override(), "emit(more=True) without a resume()"
            if state is not None and _is_state_codec(type(state)):
                state = type(state).deserialize_from_bytes(state.serialize_to_bytes())
            elif out.more:
                raise AssertionError("continued output needs a serializable state (it is dropped over HTTP)")
            return Reply(
                batch=out.batches[0],
                parent_rows=list(out.parent_rows) if out.parent_rows else [],
                more=out.more,
                requests=(len(calls) - before) if calls is not None else 0,
            )

        for n, batch in enumerate(input_batches):
            replies = [turn(batch)]
            last = n == len(input_batches) - 1
            while replies[-1].more:
                if last and max_replies is not None and len(replies) >= max_replies:
                    break  # a LIMIT: the client stops ticking and closes the input
                assert len(replies) < MAX_TURNS, "continued output never ended"
                replies.append(turn(None))
            for r in replies:
                assert all(0 <= p < batch.num_rows for p in r.parent_rows), "parent_rows index the last input"
            run.replies.append(replies)
    finally:
        if original_storage is None:
            del func_cls.storage
        else:
            func_cls.storage = original_storage
    return run


def invoke_lateral(
    func_cls: type,
    rows: dict[str, list[Any]],
    *,
    named: dict[str, Any] | None = None,
    secrets: dict[str, dict[str, pa.Scalar]] | None = None,
) -> tuple[pa.Table, list[int]]:
    """Drive a ``RowTransformFunction`` with one input batch of per-row path params.

    Returns the rows of every reply and their ``parent_rows`` (input-row index per
    output row); :func:`run_lateral` exposes the replies themselves.
    """
    run = run_lateral(func_cls, rows, named=named, secrets=secrets)
    return run.table(), run.parents()


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
