"""The single DuckDB writer.

One asyncio task consumes a queue of write operations, so exactly one thing in the process ever
writes to DuckDB. Every other component enqueues and awaits. That gives serialisation with no
locks to get wrong, natural backpressure, and one place to put transaction discipline.

The alternative was verified and rejected: two cursors updating overlapping rows raise
``TransactionException: Conflict on update!`` on the second statement, and DuckDB does not retry.
The queue makes that structurally impossible.

The idempotent candle write is delete then insert, scoped to the exact half-open IST window of
the request that produced the rows, with the coverage row and the bounds update in the same
transaction. Measured 3 ms against 11 ms for ``MERGE INTO`` on a 5,000,000 row table, and unlike
MERGE the cost is bounded by the range rather than by the size of the table. It is also the only
variant that is semantically correct: when Fyers returns 2,900 candles where it previously
returned 3,000, the stale 100 are removed, which MERGE would silently leave behind.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

import pyarrow as pa
import duckdb
from starlette.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from expirymanager.db.duck import DuckStore

logger = logging.getLogger(__name__)

WRITE_QUEUE_MAXSIZE = 256

COVERAGE_COLUMNS = (
    "contract_id",
    "res_id",
    "range_from",
    "range_to",
    "status",
    "row_count",
    "first_ts",
    "last_ts",
    "include_oi",
    "columns_json",
    "schema_version",
    "payload_sha256",
    "http_status",
    "fyers_code",
    "latency_ms",
    "response_bytes",
    "token_fingerprint",
    "task_id",
    "run_id",
    "fetched_at",
)

_COVERAGE_INSERT = (
    "INSERT OR REPLACE INTO candle_coverage ("
    + ", ".join(COVERAGE_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in COVERAGE_COLUMNS)
    + ")"
)


class WriterError(RuntimeError):
    """The writer task is not in a state that can accept work."""


@runtime_checkable
class WriteOp(Protocol):
    """Anything the writer can run. ``apply`` owns its own transaction."""

    label: str

    def apply(self, cur: duckdb.DuckDBPyConnection) -> Any: ...


@dataclass(frozen=True, slots=True)
class CallableWrite:
    """Escape hatch for a write that does not fit a dedicated op type.

    ``fn`` receives the writer cursor and must leave no transaction open.
    """

    label: str
    fn: Callable[[duckdb.DuckDBPyConnection], Any]

    def apply(self, cur: duckdb.DuckDBPyConnection) -> Any:
        return self.fn(cur)


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """The provenance of one fetch chunk, written beside the rows it produced."""

    status: str
    include_oi: bool
    columns_json: str
    task_id: int
    schema_version: int | None = None
    payload_sha256: str | None = None
    http_status: int | None = None
    fyers_code: int | None = None
    latency_ms: int | None = None
    response_bytes: int | None = None
    token_fingerprint: str | None = None
    run_id: int | None = None
    fetched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CandleChunkResult:
    """What one chunk write actually did."""

    rows_written: int
    rows_deleted: int
    skipped: bool
    first_ts: datetime | None
    last_ts: datetime | None


def window_bounds(range_from: date, range_to: date) -> tuple[datetime, datetime]:
    """The half-open IST window a Fyers request covers.

    Fyers range_to is inclusive of that date, so the exclusive right edge is range_to plus one
    day at 00:00:00. Getting this off by one leaves a duplicated or a missing day at the seam
    between two chunks, which is the classic bug in chunked backfills.
    """
    if range_to < range_from:
        raise ValueError(f"range_to {range_to} is before range_from {range_from}")
    start = datetime(range_from.year, range_from.month, range_from.day)
    end_date = range_to + timedelta(days=1)
    end = datetime(end_date.year, end_date.month, end_date.day)
    return (start, end)


@dataclass(frozen=True, slots=True)
class CandleChunkWrite:
    """One Fyers historical-data response, made durable atomically.

    ``batch`` is a pyarrow RecordBatch on the pinned CANDLE_SCHEMA, or None for a no_data
    response. ``range_to`` is inclusive, exactly as it was sent to Fyers.
    """

    contract_id: int
    res_id: int
    range_from: date
    range_to: date
    coverage: CoverageRow
    batch: pa.RecordBatch | pa.Table | None = None
    # Skip the whole write when the coverage row already records this payload. A settled expiry
    # re-fetched nightly is byte identical, and rewriting it only creates row versions that
    # fragment a file which never shrinks.
    honour_payload_hash: bool = True
    label: str = field(default="candle_chunk", init=False)

    def apply(self, cur: duckdb.DuckDBPyConnection) -> CandleChunkResult:
        window_start, window_end = window_bounds(self.range_from, self.range_to)
        arrow_batch = self.batch
        row_count = 0 if arrow_batch is None else arrow_batch.num_rows
        first_ts, last_ts = _batch_bounds(arrow_batch)
        fetched_at = self.coverage.fetched_at or datetime.now()

        cur.execute("BEGIN TRANSACTION")
        try:
            if self.honour_payload_hash and self._payload_unchanged(cur):
                cur.execute("COMMIT")
                return CandleChunkResult(
                    rows_written=0,
                    rows_deleted=0,
                    skipped=True,
                    first_ts=first_ts,
                    last_ts=last_ts,
                )

            # The delete predicate is literally the request that produced the data, so the
            # operation converges after any crash and correctly removes surplus rows when a
            # re-fetch returns fewer candles than the previous one.
            deleted = cur.execute(
                "DELETE FROM candles "
                "WHERE contract_id = ? AND res_id = ? AND ts >= ? AND ts < ?",
                [self.contract_id, self.res_id, window_start, window_end],
            ).fetchone()
            rows_deleted = int(deleted[0]) if deleted else 0

            if row_count:
                # arrow_batch is a local name, so DuckDB's replacement scan resolves it from
                # this frame. Registering on the parent connection would not be visible here.
                cur.execute("INSERT INTO candles SELECT * FROM arrow_batch")

            cur.execute(
                _COVERAGE_INSERT,
                [
                    self.contract_id,
                    self.res_id,
                    self.range_from,
                    self.range_to,
                    self.coverage.status,
                    row_count,
                    first_ts,
                    last_ts,
                    self.coverage.include_oi,
                    self.coverage.columns_json,
                    self.coverage.schema_version,
                    self.coverage.payload_sha256,
                    self.coverage.http_status,
                    self.coverage.fyers_code,
                    self.coverage.latency_ms,
                    self.coverage.response_bytes,
                    self.coverage.token_fingerprint,
                    self.coverage.task_id,
                    self.coverage.run_id,
                    fetched_at,
                ],
            )
            _refresh_bounds(cur, self.contract_id, self.res_id)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return CandleChunkResult(
            rows_written=row_count,
            rows_deleted=rows_deleted,
            skipped=False,
            first_ts=first_ts,
            last_ts=last_ts,
        )

    def _payload_unchanged(self, cur: duckdb.DuckDBPyConnection) -> bool:
        digest = self.coverage.payload_sha256
        if not digest:
            return False
        existing = cur.execute(
            "SELECT payload_sha256, status FROM candle_coverage "
            "WHERE contract_id = ? AND res_id = ? AND range_from = ? AND range_to = ?",
            [self.contract_id, self.res_id, self.range_from, self.range_to],
        ).fetchone()
        if existing is None:
            return False
        return existing[0] == digest and existing[1] == self.coverage.status


def _batch_bounds(batch: pa.RecordBatch | pa.Table | None) -> tuple[Any, Any]:
    if batch is None or batch.num_rows == 0:
        return (None, None)
    ts = batch.column(batch.schema.get_field_index("ts"))
    return (ts[0].as_py(), ts[batch.num_rows - 1].as_py())


def _refresh_bounds(cur: duckdb.DuckDBPyConnection, contract_id: int, res_id: int) -> None:
    """Recompute contract_bounds from the rows that are now present.

    Derived rather than accumulated, so it converges after a shrinking correction the same way
    the candles themselves do. The aggregate leads with contract_id, the leading sort key, so it
    prunes to a handful of row groups.
    """
    row = cur.execute(
        "SELECT count(*), min(ts), max(ts) FROM candles WHERE contract_id = ? AND res_id = ?",
        [contract_id, res_id],
    ).fetchone()
    total = int(row[0]) if row else 0
    cur.execute(
        "DELETE FROM contract_bounds WHERE contract_id = ? AND res_id = ?",
        [contract_id, res_id],
    )
    if total == 0:
        # No row at all, so v_data_health can still report a contract that has no bars.
        return
    cur.execute(
        "INSERT INTO contract_bounds "
        "(contract_id, res_id, first_ts, last_ts, row_count, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [contract_id, res_id, row[1], row[2], total, datetime.now()],
    )


class DuckWriter:
    """The one writer task.

    Start it in the application lifespan, stop it on shutdown. ``submit`` is the only way in.
    """

    def __init__(self, store: DuckStore, *, maxsize: int = WRITE_QUEUE_MAXSIZE) -> None:
        self._store = store
        self._maxsize = maxsize
        # The queue is created in start(), not here: an asyncio.Queue binds to the loop that
        # first awaits it, and this object is built lazily off DuckStore before any loop exists.
        self._queue: asyncio.Queue[tuple[WriteOp, asyncio.Future[Any]] | None] | None = None
        self._cursor: duckdb.DuckDBPyConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def depth(self) -> int:
        """Queue depth, exposed for the diagnostics panel."""
        return 0 if self._queue is None else self._queue.qsize()

    async def start(self) -> None:
        if self._running:
            return
        self._queue = asyncio.Queue(maxsize=self._maxsize)
        self._cursor = self._store.cursor()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="duck-writer")

    async def stop(self) -> None:
        if not self._running or self._queue is None:
            return
        self._running = False
        await self._queue.put(None)
        if self._task is not None:
            await self._task
            self._task = None
        self._queue = None
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None

    async def submit(self, op: WriteOp) -> Any:
        """Enqueue one operation and wait for its transaction to commit."""
        if not self._running or self._queue is None:
            raise WriterError("The DuckDB writer is not running.")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((op, future))
        return await future

    async def _loop(self) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                op, future = item
                try:
                    # DuckDB releases the GIL, so running this on the event loop thread would
                    # stall every request for the duration of the transaction.
                    result = await run_in_threadpool(op.apply, self._cursor)
                except Exception as exc:
                    logger.exception("duck write failed", extra={"op": op.label})
                    if not future.cancelled():
                        future.set_exception(exc)
                else:
                    if not future.cancelled():
                        future.set_result(result)
            finally:
                queue.task_done()
