"""The durable lease queue over `sqlite.task`.

The central idea of the whole pipeline lives here: every outbound Fyers request is one row in the
`task` table, and that row is simultaneously the work queue entry, the retry record and the request
provenance record. There is no separate checkpoint file, no in-memory job graph and no queue
broker. Checkpoint granularity therefore equals request granularity, so a 62,000 request backfill
resumes at the exact request after a crash, a closed laptop lid or a token expiry.

Four properties this module exists to guarantee:

1. Claiming is one statement. `lease` is a single `UPDATE ... RETURNING` over `idx_task_dispatch`,
   so two workers can never read the same pending row and both decide to run it. A read then write
   claim would need a transaction the SQLite pool does not naturally give us, and would still race
   with the reclaim loop.

2. A lost lease is not a failure. `reclaim_expired_leases` returns lapsed rows to `pending` without
   touching `attempt`. A worker killed mid task, or one starved behind a long rate limit wait, has
   not proved anything about the task, so burning one of its four attempts would turn an operator
   restart into permanent data loss.

3. Every completion is owner scoped. `ack` and the three `nack` variants match on
   `state = 'leased' AND lease_owner = :owner`, so a worker that finishes after its lease was
   reclaimed writes nothing and the row that was already handed to somebody else stays coherent.
   This is what makes a crash mid task replay exactly one chunk rather than two writers racing on
   the same coverage row.

4. Auth and rate limit outcomes never consume an attempt. That rule is stated in PIPELINE.md
   section 4 and is encoded twice over: `errors.Classification.consumes_attempt` decides it, and
   `nack_auth` and `nack_rate` physically do not touch the column.

Timestamps are stored as fixed width UTC ISO 8601 strings, because `not_before <= :now` is a
lexicographic comparison in SQLite and a variable width format would compare wrongly. Everything
that writes `not_before` should go through `utc_now_iso` or `iso_at`.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from expirymanager.brokers.fyers.errors import MAX_ATTEMPTS, backoff_seconds

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "TASK_PENDING",
    "TASK_LEASED",
    "TASK_DONE",
    "TASK_EMPTY",
    "TASK_FAILED",
    "TASK_SKIPPED",
    "TASK_CANCELLED",
    "TASK_STATES",
    "OPEN_STATES",
    "TERMINAL_STATES",
    "TASK_KINDS",
    "JOB_LEASABLE_STATUSES",
    "utc_now",
    "utc_now_iso",
    "iso_at",
    "LeasedTask",
    "TaskOutcome",
    "JobAggregate",
    "LeaseQueue",
]

log = logging.getLogger(__name__)

# PIPELINE.md section 3.1. Comfortably longer than the client's 30 second request timeout, so a
# healthy in flight request can never have its lease reclaimed underneath it, and short enough that
# a hard kill returns its work within a couple of reclaim ticks.
DEFAULT_LEASE_SECONDS = 120

TASK_PENDING = "pending"
TASK_LEASED = "leased"
TASK_DONE = "done"
TASK_EMPTY = "empty"
TASK_FAILED = "failed"
TASK_SKIPPED = "skipped"
TASK_CANCELLED = "cancelled"

TASK_STATES: tuple[str, ...] = (
    TASK_PENDING,
    TASK_LEASED,
    TASK_DONE,
    TASK_EMPTY,
    TASK_FAILED,
    TASK_SKIPPED,
    TASK_CANCELLED,
)

# Work that still owes the job an outcome. A job is terminal exactly when this set is empty.
OPEN_STATES: tuple[str, ...] = (TASK_PENDING, TASK_LEASED)
TERMINAL_STATES: tuple[str, ...] = (
    TASK_DONE,
    TASK_EMPTY,
    TASK_FAILED,
    TASK_SKIPPED,
    TASK_CANCELLED,
)

# Mirrors ck_task_kind in db/models.py. Kept here so the handler registry can refuse a typo at
# registration time rather than at the first lease.
TASK_KINDS: tuple[str, ...] = (
    "expiry_dates",
    "underlying_symbols",
    "candle_chunk",
    "spot_chunk",
    "symbol_master",
    "chain_snapshot",
)

# A task is only leasable while its job is live. `paused`, `blocked_auth`, `blocked_rate`,
# `deferred_budget` and every terminal status deliberately stop the dispatcher without touching a
# single task row, which is what lets a pause be instant and lossless.
JOB_LEASABLE_STATUSES: tuple[str, ...] = ("queued", "running")

_LEASED_COLUMNS = (
    "task_id",
    "job_id",
    "seq",
    "kind",
    "state",
    "priority",
    "underlying_id",
    "contract_id",
    "fyers_symbol",
    "expiry_date",
    "resolution",
    "range_from",
    "range_to",
    "include_oi",
    "request_params_json",
    "parent_task_id",
    "attempt",
    "max_attempts",
    "not_before",
    "lease_owner",
    "lease_expires_at",
)

_RETURNING = ", ".join(_LEASED_COLUMNS)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_at(moment: datetime) -> str:
    """Fixed width UTC ISO 8601.

    `datetime.isoformat` drops the microseconds when they happen to be zero, which produces two
    string widths for the same instant and breaks the lexicographic `not_before <= :now` compare
    at exactly the moments a scheduler is most likely to produce. Formatting explicitly removes
    that class of bug entirely.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def utc_now_iso() -> str:
    return iso_at(utc_now())


@dataclass(frozen=True, slots=True)
class LeasedTask:
    """One claimed row, exactly as the lease statement returned it.

    Frozen because a worker must not be able to mutate what it thinks it holds: the durable truth
    is the row, and every change to it goes back through this module.
    """

    task_id: int
    job_id: str
    seq: int
    kind: str
    state: str
    priority: int
    underlying_id: int | None
    contract_id: int | None
    fyers_symbol: str | None
    expiry_date: str | None
    resolution: str | None
    range_from: str | None
    range_to: str | None
    include_oi: int
    request_params_json: str | None
    parent_task_id: int | None
    attempt: int
    max_attempts: int
    not_before: str
    lease_owner: str | None
    lease_expires_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LeasedTask":
        return cls(**{name: row[name] for name in _LEASED_COLUMNS})

    @property
    def next_attempt(self) -> int:
        """What `attempt` becomes if this try fails transiently."""
        return self.attempt + 1

    @property
    def attempts_exhausted(self) -> bool:
        return self.next_attempt >= self.max_attempts


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """The provenance a handler writes back onto its own row.

    Every field is optional because the handlers differ: a candle chunk fills the whole record, a
    symbol master fetch has no Fyers status code at all. `state` is the only thing the queue
    insists on, and it must be a terminal success state.
    """

    state: str = TASK_DONE
    http_status: int | None = None
    fyers_s: str | None = None
    fyers_code: int | None = None
    last_error_text: str | None = None
    latency_ms: int | None = None
    response_bytes: int | None = None
    row_count: int | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    columns_json: str | None = None
    schema_version: int | None = None
    payload_sha256: str | None = None
    raw_body_path: str | None = None
    token_fingerprint: str | None = None
    # Rolled onto the job row rather than the task row. `rows_written` differs from `row_count`
    # when the payload hash short circuit skipped the write.
    rows_written: int = 0
    bytes_downloaded: int = 0
    requests_used: int = 1

    def __post_init__(self) -> None:
        if self.state not in (TASK_DONE, TASK_EMPTY, TASK_SKIPPED):
            raise ValueError(f"a handler outcome cannot be {self.state!r}")


@dataclass(frozen=True, slots=True)
class JobAggregate:
    """A fresh `GROUP BY state` count over one job's durable rows.

    Never an in-memory counter. Progress that is recomputed is correct after a restart and after a
    dropped SSE frame, which is the whole reason section 8 of PIPELINE.md forbids incrementing.
    """

    job_id: str
    status: str
    cancel_requested: bool
    counts: Mapping[str, int]
    requests_used: int
    rows_written: int
    bytes_downloaded: int

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def open(self) -> int:
        return sum(self.counts.get(state, 0) for state in OPEN_STATES)

    @property
    def done(self) -> int:
        return self.counts.get(TASK_DONE, 0)

    @property
    def empty(self) -> int:
        return self.counts.get(TASK_EMPTY, 0)

    @property
    def failed(self) -> int:
        return self.counts.get(TASK_FAILED, 0)

    @property
    def skipped(self) -> int:
        return self.counts.get(TASK_SKIPPED, 0)

    @property
    def cancelled(self) -> int:
        return self.counts.get(TASK_CANCELLED, 0)

    @property
    def pending(self) -> int:
        return self.counts.get(TASK_PENDING, 0)

    @property
    def leased(self) -> int:
        return self.counts.get(TASK_LEASED, 0)

    @property
    def is_finished(self) -> bool:
        return self.open == 0

    def terminal_status(self) -> str:
        """What the job status becomes once no task is still open."""
        if self.cancel_requested:
            return "cancelled"
        if self.failed:
            return "completed_with_errors"
        return "completed"


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# PIPELINE.md section 3.1, with the job join added. The join is not decoration: without it a
# cancelled or paused job's rows keep getting leased and then immediately released, which spins the
# dispatcher and, worse, races the bulk cancel. SQLite scans `t` through idx_task_dispatch and looks
# `j` up by primary key, so the ordering the index provides is preserved.
_LEASE_SQL = f"""
UPDATE task
   SET state = 'leased',
       lease_owner = :owner,
       lease_expires_at = :lease_expires_at,
       started_at = :now
 WHERE task_id IN (
     SELECT t.task_id
       FROM task AS t
       JOIN job AS j ON j.job_id = t.job_id
      WHERE t.state = 'pending'
        AND t.not_before <= :now
        AND j.cancel_requested = 0
        AND j.status IN ('queued', 'running')
      ORDER BY t.priority, t.job_id, t.seq
      LIMIT :limit)
RETURNING {_RETURNING}
"""

# Promotes every job that just had work claimed. One statement rather than a Python loop so the
# promotion is inside the same transaction as the lease and cannot be lost to a crash between them.
_PROMOTE_SQL = """
UPDATE job
   SET status = 'running',
       started_at = COALESCE(started_at, :now)
 WHERE status = 'queued'
   AND job_id IN (SELECT job_id FROM task WHERE task_id IN :task_ids)
RETURNING job_id
"""

_ACK_SQL = """
UPDATE task
   SET state = :state,
       lease_owner = NULL,
       lease_expires_at = NULL,
       finished_at = :now,
       http_status = :http_status,
       fyers_s = :fyers_s,
       fyers_code = :fyers_code,
       last_error_text = :last_error_text,
       latency_ms = :latency_ms,
       response_bytes = :response_bytes,
       row_count = :row_count,
       first_ts = :first_ts,
       last_ts = :last_ts,
       columns_json = :columns_json,
       schema_version = :schema_version,
       payload_sha256 = :payload_sha256,
       raw_body_path = :raw_body_path,
       token_fingerprint = :token_fingerprint
 WHERE task_id = :task_id
   AND state = 'leased'
   AND lease_owner = :owner
"""

# The attempt counter and the resulting state are computed in SQL from the row itself rather than
# from the leased copy, so a stale in-memory attempt can never write a wrong retry budget.
_NACK_TRANSIENT_SQL = """
UPDATE task
   SET state = CASE WHEN attempt + 1 >= max_attempts THEN 'failed' ELSE 'pending' END,
       attempt = attempt + 1,
       not_before = :not_before,
       lease_owner = NULL,
       lease_expires_at = NULL,
       finished_at = CASE WHEN attempt + 1 >= max_attempts THEN :now ELSE NULL END,
       http_status = :http_status,
       fyers_s = :fyers_s,
       fyers_code = :fyers_code,
       last_error_text = :last_error_text,
       latency_ms = :latency_ms
 WHERE task_id = :task_id
   AND state = 'leased'
   AND lease_owner = :owner
RETURNING state, attempt
"""

_NACK_FATAL_SQL = """
UPDATE task
   SET state = 'failed',
       lease_owner = NULL,
       lease_expires_at = NULL,
       finished_at = :now,
       http_status = :http_status,
       fyers_s = :fyers_s,
       fyers_code = :fyers_code,
       last_error_text = :last_error_text,
       latency_ms = :latency_ms
 WHERE task_id = :task_id
   AND state = 'leased'
   AND lease_owner = :owner
"""

# Auth and rate limit. `attempt` is absent from the SET list on purpose and must stay absent.
_NACK_PARK_SQL = """
UPDATE task
   SET state = 'pending',
       lease_owner = NULL,
       lease_expires_at = NULL,
       http_status = :http_status,
       fyers_s = :fyers_s,
       fyers_code = :fyers_code,
       last_error_text = :last_error_text
 WHERE task_id = :task_id
   AND state = 'leased'
   AND lease_owner = :owner
"""

# Used when a worker gives a lease back untouched: a shutdown, a closed auth gate noticed after the
# claim, or a dispatcher drain. Nothing about the task is recorded because nothing happened to it.
_RELEASE_SQL = """
UPDATE task
   SET state = 'pending',
       lease_owner = NULL,
       lease_expires_at = NULL
 WHERE state = 'leased'
   AND lease_owner = :owner
   AND task_id IN :task_ids
"""

_RECLAIM_SQL = """
UPDATE task
   SET state = 'pending',
       lease_owner = NULL,
       lease_expires_at = NULL
 WHERE state = 'leased'
   AND lease_expires_at < :now
"""

_CANCEL_TASKS_SQL = """
UPDATE task
   SET state = 'cancelled',
       lease_owner = NULL,
       lease_expires_at = NULL,
       finished_at = :now
 WHERE job_id = :job_id
   AND state = 'pending'
"""

_COUNTS_SQL = "SELECT state, count(*) AS n FROM task WHERE job_id = :job_id GROUP BY state"

_JOB_ROW_SQL = """
SELECT status, cancel_requested, requests_used, rows_written, bytes_downloaded
  FROM job WHERE job_id = :job_id
"""

_SYNC_COUNTERS_SQL = """
UPDATE job
   SET total_tasks = :total,
       done_tasks = :done,
       empty_tasks = :empty,
       failed_tasks = :failed,
       skipped_tasks = :skipped
 WHERE job_id = :job_id
"""

_ROLL_USAGE_SQL = """
UPDATE job
   SET requests_used = requests_used + :requests_used,
       rows_written = rows_written + :rows_written,
       bytes_downloaded = bytes_downloaded + :bytes_downloaded
 WHERE job_id = :job_id
"""


class LeaseQueue:
    """Synchronous SQLite access to the task ledger.

    Every method is blocking, because SQLAlchemy over pysqlite is. The dispatcher and the workers
    call them through a thread runner so the event loop is never held by a write. Keeping this
    class synchronous rather than wrapping each statement is deliberate: it stays directly testable
    without a loop, and there is exactly one place that decides how blocking work reaches a thread.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = utc_now,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        rng: random.Random | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._rng = rng
        # Set by every `lease` call to the jobs that moved from queued to running in that same
        # transaction. The dispatcher reads it immediately after the call so it can publish
        # job_started, which is a discrete frame and must not wait for the progress tick.
        self.last_promoted: tuple[str, ...] = ()

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    def now(self) -> datetime:
        return self._clock()

    # -- claiming -----------------------------------------------------------

    def lease(self, *, owner: str, limit: int = 1) -> list[LeasedTask]:
        """Claim up to `limit` runnable tasks in dispatch order. One statement, no race.

        Also promotes any `queued` job that just had work claimed to `running`, inside the same
        transaction, so the two facts cannot disagree after a crash.
        """
        self.last_promoted = ()
        if limit <= 0:
            return []
        now = self._clock()
        params = {
            "owner": owner,
            "now": iso_at(now),
            "lease_expires_at": iso_at(now + timedelta(seconds=self._lease_seconds)),
            "limit": limit,
        }
        with self._engine.begin() as connection:
            rows = connection.execute(text(_LEASE_SQL), params).mappings().all()
            if not rows:
                return []
            # RETURNING answers in the order the rows happen to sit in the file, not in the order
            # the subquery selected them. The selection is what the priority rule needs and it is
            # already correct; sorting the batch here only makes the buffer hand work to the
            # workers in the same order, so a high priority nightly job is not merely claimed first
            # but also run first.
            leased = sorted(
                (LeasedTask.from_row(row) for row in rows),
                key=lambda task: (task.priority, task.job_id, task.seq),
            )
            statement = text(_PROMOTE_SQL).bindparams(
                *_expanding("task_ids", [task.task_id for task in leased])
            )
            started = connection.execute(
                statement, {"now": params["now"], "task_ids": [t.task_id for t in leased]}
            ).scalars().all()
        self.last_promoted = tuple(str(value) for value in started)
        if self.last_promoted:
            log.info("job started", extra={"job_ids": list(self.last_promoted)})
        return leased

    # -- completion ---------------------------------------------------------

    def ack(self, task: LeasedTask, outcome: TaskOutcome, *, owner: str | None = None) -> bool:
        """Record a successful outcome. Returns False when the lease had already been reclaimed.

        A False here is not an error. It means this worker's lease lapsed and somebody else now
        owns the row, so writing the outcome would overwrite a newer attempt. The DuckDB write that
        preceded it is idempotent by construction, so the replay costs one request and nothing else.
        """
        holder = owner or task.lease_owner
        now = iso_at(self._clock())
        params = {
            "task_id": task.task_id,
            "owner": holder,
            "now": now,
            "state": outcome.state,
            "http_status": outcome.http_status,
            "fyers_s": outcome.fyers_s,
            "fyers_code": outcome.fyers_code,
            "last_error_text": outcome.last_error_text,
            "latency_ms": outcome.latency_ms,
            "response_bytes": outcome.response_bytes,
            "row_count": outcome.row_count,
            "first_ts": outcome.first_ts,
            "last_ts": outcome.last_ts,
            "columns_json": outcome.columns_json,
            "schema_version": outcome.schema_version,
            "payload_sha256": outcome.payload_sha256,
            "raw_body_path": outcome.raw_body_path,
            "token_fingerprint": outcome.token_fingerprint,
        }
        with self._engine.begin() as connection:
            applied = connection.execute(text(_ACK_SQL), params).rowcount == 1
            if applied and (
                outcome.requests_used or outcome.rows_written or outcome.bytes_downloaded
            ):
                connection.execute(
                    text(_ROLL_USAGE_SQL),
                    {
                        "job_id": task.job_id,
                        "requests_used": outcome.requests_used,
                        "rows_written": outcome.rows_written,
                        "bytes_downloaded": outcome.bytes_downloaded,
                    },
                )
        if not applied:
            log.warning(
                "ack ignored, the lease was already reclaimed",
                extra={"task_id": task.task_id, "job_id": task.job_id},
            )
        return applied

    def nack_transient(
        self,
        task: LeasedTask,
        *,
        owner: str | None = None,
        error_text: str | None = None,
        http_status: int | None = None,
        fyers_s: str | None = None,
        fyers_code: int | None = None,
        latency_ms: int | None = None,
        delay_seconds: float | None = None,
    ) -> tuple[bool, str, int]:
        """Consume one attempt and schedule a retry, or fail the task when attempts run out.

        Returns (applied, resulting state, attempt). The delay defaults to the jittered exponential
        backoff from `errors.backoff_seconds`, computed against the attempt this failure produces.
        """
        holder = owner or task.lease_owner
        now = self._clock()
        delay = (
            delay_seconds
            if delay_seconds is not None
            else backoff_seconds(task.next_attempt, rng=self._rng)
        )
        params = {
            "task_id": task.task_id,
            "owner": holder,
            "now": iso_at(now),
            "not_before": iso_at(now + timedelta(seconds=delay)),
            "http_status": http_status,
            "fyers_s": fyers_s,
            "fyers_code": fyers_code,
            "last_error_text": error_text,
            "latency_ms": latency_ms,
        }
        with self._engine.begin() as connection:
            row = connection.execute(text(_NACK_TRANSIENT_SQL), params).mappings().first()
            if row is not None:
                connection.execute(
                    text(_ROLL_USAGE_SQL),
                    {
                        "job_id": task.job_id,
                        "requests_used": 1,
                        "rows_written": 0,
                        "bytes_downloaded": 0,
                    },
                )
        if row is None:
            return False, task.state, task.attempt
        return True, str(row["state"]), int(row["attempt"])

    def nack_fatal(
        self,
        task: LeasedTask,
        *,
        owner: str | None = None,
        error_text: str | None = None,
        http_status: int | None = None,
        fyers_s: str | None = None,
        fyers_code: int | None = None,
        latency_ms: int | None = None,
    ) -> bool:
        """Fail the task outright. No retry, because the answer would be identical."""
        holder = owner or task.lease_owner
        params = {
            "task_id": task.task_id,
            "owner": holder,
            "now": iso_at(self._clock()),
            "http_status": http_status,
            "fyers_s": fyers_s,
            "fyers_code": fyers_code,
            "last_error_text": error_text,
            "latency_ms": latency_ms,
        }
        with self._engine.begin() as connection:
            applied = connection.execute(text(_NACK_FATAL_SQL), params).rowcount == 1
            if applied:
                connection.execute(
                    text(_ROLL_USAGE_SQL),
                    {
                        "job_id": task.job_id,
                        "requests_used": 1,
                        "rows_written": 0,
                        "bytes_downloaded": 0,
                    },
                )
        return applied

    def nack_auth(
        self,
        task: LeasedTask,
        *,
        owner: str | None = None,
        error_text: str | None = None,
        http_status: int | None = None,
        fyers_s: str | None = None,
        fyers_code: int | None = None,
    ) -> bool:
        """Park the task. Back to pending, lease released, attempt untouched.

        The attempt counter is deliberately not incremented here. A token that expired at 03:00 is
        not evidence that four contracts' worth of requests were malformed.
        """
        return self._park(
            task,
            owner=owner,
            error_text=error_text,
            http_status=http_status,
            fyers_s=fyers_s,
            fyers_code=fyers_code,
        )

    def nack_rate(
        self,
        task: LeasedTask,
        *,
        owner: str | None = None,
        error_text: str | None = None,
        http_status: int | None = None,
        fyers_s: str | None = None,
        fyers_code: int | None = None,
    ) -> bool:
        """Park the task after a rate limit. Attempt untouched, for the same reason as auth."""
        return self._park(
            task,
            owner=owner,
            error_text=error_text,
            http_status=http_status,
            fyers_s=fyers_s,
            fyers_code=fyers_code,
        )

    def _park(
        self,
        task: LeasedTask,
        *,
        owner: str | None,
        error_text: str | None,
        http_status: int | None,
        fyers_s: str | None,
        fyers_code: int | None,
    ) -> bool:
        params = {
            "task_id": task.task_id,
            "owner": owner or task.lease_owner,
            "http_status": http_status,
            "fyers_s": fyers_s,
            "fyers_code": fyers_code,
            "last_error_text": error_text,
        }
        with self._engine.begin() as connection:
            applied = connection.execute(text(_NACK_PARK_SQL), params).rowcount == 1
            if applied:
                # The request was spent even though the task is going back to pending, so the job's
                # request ledger has to count it. Only the attempt counter is protected.
                connection.execute(
                    text(_ROLL_USAGE_SQL),
                    {
                        "job_id": task.job_id,
                        "requests_used": 1,
                        "rows_written": 0,
                        "bytes_downloaded": 0,
                    },
                )
        return applied

    def release(self, task_ids: Iterable[int], *, owner: str) -> int:
        """Hand leases back untouched. Used by a drain, a shutdown and a closed gate."""
        ids = [int(value) for value in task_ids]
        if not ids:
            return 0
        statement = text(_RELEASE_SQL).bindparams(*_expanding("task_ids", ids))
        with self._engine.begin() as connection:
            return connection.execute(statement, {"owner": owner, "task_ids": ids}).rowcount

    def release_all(self, owner: str) -> int:
        """Hand back every lease this owner still holds.

        Called once after the worker pool has stopped. A worker cancelled mid task cannot reliably
        issue its own release, because the statement it would await is itself cancellable, so the
        sweep is done from outside the pool where it is guaranteed to run.
        """
        with self._engine.begin() as connection:
            return connection.execute(
                text(
                    "UPDATE task SET state = 'pending', lease_owner = NULL,"
                    " lease_expires_at = NULL"
                    " WHERE state = 'leased' AND lease_owner = :owner"
                ),
                {"owner": owner},
            ).rowcount

    # -- recovery -----------------------------------------------------------

    def reclaim_expired_leases(self) -> int:
        """Return every lapsed lease to pending WITHOUT burning an attempt.

        Runs at startup and on a timer. A lost lease is our fault, not the task's, so charging it
        an attempt would let a restart loop exhaust the retry budget of work that never ran.
        """
        with self._engine.begin() as connection:
            reclaimed = connection.execute(
                text(_RECLAIM_SQL), {"now": iso_at(self._clock())}
            ).rowcount
        if reclaimed:
            log.info("reclaimed expired leases", extra={"reclaimed": reclaimed})
        return reclaimed

    def cancel_pending(self, job_id: str) -> int:
        """Bulk cancel a job's pending tasks in one statement.

        Leased tasks are left alone on purpose: PIPELINE.md section 6 wants in flight work to
        finish and write its data, so a cancelled job leaves valid partial data plus an accurate
        ledger of exactly what it did and did not fetch.
        """
        with self._engine.begin() as connection:
            return connection.execute(
                text(_CANCEL_TASKS_SQL), {"job_id": job_id, "now": iso_at(self._clock())}
            ).rowcount

    # -- reading ------------------------------------------------------------

    def counts_by_state(self, job_id: str) -> dict[str, int]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(_COUNTS_SQL), {"job_id": job_id}).all()
        return {str(state): int(count) for state, count in rows}

    def aggregate(self, job_id: str, *, sync_counters: bool = False) -> JobAggregate | None:
        """A fresh `GROUP BY state` aggregate, optionally written back onto the job row.

        `sync_counters` keeps `GET /api/v1/jobs/{id}` identical to the SSE frame without either
        side maintaining its own tally.
        """
        with self._engine.begin() as connection:
            job = connection.execute(text(_JOB_ROW_SQL), {"job_id": job_id}).mappings().first()
            if job is None:
                return None
            counts = {
                str(state): int(count)
                for state, count in connection.execute(
                    text(_COUNTS_SQL), {"job_id": job_id}
                ).all()
            }
            aggregate = JobAggregate(
                job_id=job_id,
                status=str(job["status"]),
                cancel_requested=bool(job["cancel_requested"]),
                counts=counts,
                requests_used=int(job["requests_used"]),
                rows_written=int(job["rows_written"]),
                bytes_downloaded=int(job["bytes_downloaded"]),
            )
            if sync_counters:
                connection.execute(
                    text(_SYNC_COUNTERS_SQL),
                    {
                        "job_id": job_id,
                        "total": aggregate.total,
                        "done": aggregate.done,
                        "empty": aggregate.empty,
                        "failed": aggregate.failed,
                        "skipped": aggregate.skipped,
                    },
                )
        return aggregate

    def active_job_ids(self) -> tuple[str, ...]:
        """Jobs the dispatcher may lease from, in dispatch order."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT job_id FROM job WHERE status IN ('queued', 'running')"
                    " ORDER BY priority, created_at"
                )
            ).scalars().all()
        return tuple(str(value) for value in rows)

    def ready_count(self) -> int:
        """How many tasks could be leased right now. Used by the dispatcher's idle decision."""
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT count(*) FROM task AS t JOIN job AS j ON j.job_id = t.job_id"
                    " WHERE t.state = 'pending' AND t.not_before <= :now"
                    "   AND j.cancel_requested = 0 AND j.status IN ('queued', 'running')"
                ),
                {"now": iso_at(self._clock())},
            ).scalar_one()
        return int(row)

    def load_task(self, task_id: int) -> Mapping[str, Any] | None:
        with self._engine.connect() as connection:
            return connection.execute(
                text("SELECT * FROM task WHERE task_id = :task_id"), {"task_id": task_id}
            ).mappings().first()


def _expanding(name: str, values: Sequence[Any]) -> list[Any]:
    """Bind an IN list. Split out only so the two call sites read the same."""
    from sqlalchemy import bindparam

    return [bindparam(name, expanding=True, value=list(values))]


# Re-exported so a caller that already imported the queue does not also have to import errors just
# to learn the retry ceiling this module enforces.
DEFAULT_MAX_ATTEMPTS = MAX_ATTEMPTS
