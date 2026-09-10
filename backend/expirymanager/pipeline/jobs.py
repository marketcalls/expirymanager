"""JobService: commit a plan, and run a job's lifecycle.

The one invariant this module exists to hold is atomicity. A job row and every task row it owns
are written in ONE SQLite transaction, so a job is never half created. A partially created job
would be the worst possible failure mode here: the dispatcher would happily run the tasks that
made it in, the progress aggregate would report a total that was never true, and the user would
be billed real requests against a plan they never saw.

Three gates sit in front of that write, in this order:

1. `confirm_requests`. A plan the user actually saw priced carries its request count back on the
   commit, and a mismatch is a 409. This is what stops a stale preview committing a job nobody
   priced: contracts get discovered and coverage lands between the plan call and the commit, so
   the number genuinely moves. An estimate the user did not see, a schedule fire for instance,
   passes `None` and is not gated, because there is nothing to compare against.

2. The budget. A plan that needs more requests than remain today is refused with the reason,
   unless the caller asked to defer, in which case the job is committed as `deferred_budget` and
   the 00:01 IST reset moves it to `queued`. A scheduled sweep is additionally held to
   `budget_reserve_fraction` of the daily cap, so a nightly backfill can never consume the share
   of the day held back for downloads the user starts by hand.

3. The pipeline mode. A `stopped_fatal` pipeline takes no new work.

Everything after the commit is a status transition on the job row plus a nudge to the supervisor.
Task rows are never rewritten by this module: pause, resume and cancel deliberately move the job
and leave the ledger alone, which is what makes a pause instant and lossless.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from expirymanager.api.schemas.downloads import (
    DownloadAccepted,
    DownloadRequest,
    JobActionResult,
    PlanPreview,
    PlanRequest,
    RetryFailedAccepted,
)
from expirymanager.pipeline.planner import (
    PipelineRequestError,
    Plan,
    PlannedTask,
    Planner,
    summarise,
)
from expirymanager.pipeline.queue import (
    DEFAULT_MAX_ATTEMPTS,
    OPEN_STATES,
    TASK_FAILED,
    TASK_PENDING,
    LeaseQueue,
    iso_at,
    utc_now,
)

__all__ = [
    "ACTIVE_JOB_STATUSES",
    "RESUMABLE_JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "TASK_INSERT_BATCH",
    "JobServiceError",
    "JobService",
    "build_planner",
    "build_job_service",
    "roll_deferred_budget_jobs",
    "restore_pipeline_mode",
    "recover_jobs",
    "install",
]

log = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# PIPELINE.md section 9.1.
TERMINAL_JOB_STATUSES: tuple[str, ...] = (
    "completed",
    "completed_with_errors",
    "cancelled",
    "failed",
)
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("queued", "running")
RESUMABLE_JOB_STATUSES: tuple[str, ...] = (
    "paused",
    "blocked_auth",
    "blocked_rate",
    "deferred_budget",
)

# Rows per executemany inside the single transaction. Bounded so a 62,000 task backfill does not
# build one enormous parameter list, and irrelevant to atomicity: every batch is inside the same
# BEGIN and a failure on the last one rolls back the first.
TASK_INSERT_BATCH = 500

_INSERT_JOB = """
INSERT INTO job (job_id, kind, status, params_json, parent_job_id, schedule_id, priority,
                 est_requests, total_tasks, created_by, created_at)
VALUES (:job_id, :kind, :status, :params_json, :parent_job_id, :schedule_id, :priority,
        :est_requests, :total_tasks, :created_by, :created_at)
"""

_INSERT_TASK = """
INSERT INTO task (job_id, seq, kind, state, priority, underlying_id, contract_id, fyers_symbol,
                  expiry_date, resolution, range_from, range_to, include_oi,
                  request_params_json, parent_task_id, attempt, max_attempts, not_before,
                  created_at)
VALUES (:job_id, :seq, :kind, :state, :priority, :underlying_id, :contract_id, :fyers_symbol,
        :expiry_date, :resolution, :range_from, :range_to, :include_oi,
        :request_params_json, :parent_task_id, :attempt, :max_attempts, :not_before,
        :created_at)
"""

_JOB_COLUMNS = (
    "job_id, kind, status, params_json, parent_job_id, schedule_id, priority, est_requests,"
    " total_tasks, done_tasks, empty_tasks, failed_tasks, skipped_tasks, requests_used,"
    " rows_written, bytes_downloaded, cancel_requested, block_reason, error_text, created_by,"
    " created_at, started_at, finished_at"
)


class JobServiceError(PipelineRequestError):
    """A commit or a lifecycle command that is refused, with the documented code."""


class JobService:
    """Create and steer jobs. The planner prices them; this writes and moves them."""

    def __init__(
        self,
        *,
        engine: Engine,
        planner: Planner,
        supervisor: Any = None,
        governor: Any = None,
        queue: LeaseQueue | None = None,
        clock: Any = utc_now,
        id_factory: Any = None,
    ) -> None:
        self._engine = engine
        self._planner = planner
        self._supervisor = supervisor
        self._governor = governor
        self._queue = queue or LeaseQueue(engine, clock=clock)
        self._clock = clock
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    # -- planning -----------------------------------------------------------

    async def estimate(
        self,
        request: PlanRequest,
        *,
        sweep: bool = False,
        probe_backward: bool = True,
        priority: int = 100,
    ) -> PlanPreview:
        """Price a sheet. Costs zero Fyers requests, by construction."""
        plan = await self._planner.plan(
            request, sweep=sweep, probe_backward=probe_backward, priority=priority
        )
        return plan.preview

    # -- committing ---------------------------------------------------------

    async def create(
        self,
        request: DownloadRequest,
        *,
        created_by: str | None = None,
        schedule_id: str | None = None,
        sweep: bool | None = None,
        probe_backward: bool = True,
        job_kind: str | None = None,
    ) -> DownloadAccepted:
        """Plan, gate and commit. The job row and every task row land together or not at all."""
        is_sweep = schedule_id is not None if sweep is None else sweep
        priority = request.priority or 100
        plan = await self._planner.plan(
            request, sweep=is_sweep, probe_backward=probe_backward, priority=priority
        )
        preview = plan.preview

        self._check_confirm(request, preview)
        self._check_pipeline_mode()

        if not plan.tasks:
            # Refused rather than committed as an empty completed job. A nightly sweep that found
            # nothing to do would otherwise leave a junk row in the job list every night, and the
            # caller needs to distinguish "nothing to do" from "something was queued" anyway.
            raise JobServiceError(
                "nothing_to_download",
                "Everything on this sheet is already downloaded, sealed or outside the window "
                "in which the data exists.",
                status_code=409,
                detail={
                    "chunks_skipped_covered": preview.chunks_skipped_covered,
                    "contracts_sealed_skipped": preview.contracts_sealed_skipped,
                    "warnings": preview.warnings,
                },
            )

        status = "queued"
        deferred = False
        if preview.exceeds_budget:
            if not request.defer_to_tomorrow:
                raise JobServiceError(
                    "exceeds_budget",
                    f"This download needs {preview.requests_estimated} requests and only "
                    f"{preview.budget_allowance} remain "
                    + (
                        "inside today's sweep reserve."
                        if preview.reserve_applied
                        else "in today's budget."
                    ),
                    status_code=409,
                    detail={
                        "requests_estimated": preview.requests_estimated,
                        "budget_allowance": preview.budget_allowance,
                        "budget_remaining_today": preview.budget_remaining_today,
                        "reserve_applied": preview.reserve_applied,
                    },
                )
            status = "deferred_budget"
            deferred = True

        job_id = self._id_factory()
        self._commit(
            job_id=job_id,
            kind=job_kind or plan.job_kind,
            status=status,
            plan=plan,
            request=request,
            priority=priority,
            created_by=created_by,
            schedule_id=schedule_id,
        )
        log.info(
            "job created",
            extra={
                "job_id": job_id,
                "job_kind": job_kind or plan.job_kind,
                "job_status": status,
                "total_tasks": len(plan.tasks),
                "est_requests": preview.requests_estimated,
                "task_kinds": summarise(plan.tasks),
            },
        )
        if status == "queued":
            self._notify(job_id)
        return DownloadAccepted(
            job_id=job_id,
            status=status,
            total_tasks=len(plan.tasks),
            est_requests=preview.requests_estimated,
            deferred=deferred,
            preview=preview,
        )

    def _check_confirm(self, request: DownloadRequest, preview: PlanPreview) -> None:
        """The equality gate. Only a plan the user was shown carries a count to compare."""
        if request.confirm_requests is None:
            return
        if request.confirm_requests == preview.requests_estimated:
            return
        raise JobServiceError(
            "plan_changed",
            "The plan has changed since it was priced. Review the new estimate before starting "
            "the download.",
            status_code=409,
            detail={
                "confirm_requests": request.confirm_requests,
                "requests_estimated": preview.requests_estimated,
            },
        )

    def _check_pipeline_mode(self) -> None:
        governor = self._governor
        if governor is None:
            return
        mode = getattr(governor, "mode", None)
        if mode is None:
            return
        if str(getattr(mode, "value", mode)) == "stopped_fatal":
            raise JobServiceError(
                "pipeline_stopped",
                "The download pipeline is stopped and needs manual intervention before it can "
                "take new work.",
                status_code=503,
                detail={"reason": getattr(governor, "reason", None)},
            )

    def _commit(
        self,
        *,
        job_id: str,
        kind: str,
        status: str,
        plan: Plan,
        request: DownloadRequest,
        priority: int,
        created_by: str | None,
        schedule_id: str | None,
        parent_job_id: str | None = None,
    ) -> None:
        """One transaction. Either the whole job exists or none of it does."""
        now = iso_at(self._clock())
        params = request.scope_key()
        params["job_kind"] = kind
        params["preview"] = plan.preview.model_dump(mode="json")
        job_row = {
            "job_id": job_id,
            "kind": kind,
            "status": status,
            "params_json": json.dumps(params, separators=(",", ":"), sort_keys=True),
            "parent_job_id": parent_job_id,
            "schedule_id": schedule_id,
            "priority": priority,
            "est_requests": plan.preview.requests_estimated,
            "total_tasks": len(plan.tasks),
            "created_by": created_by,
            "created_at": now,
        }
        rows = [self._task_row(job_id, task, now) for task in plan.tasks]
        with self._engine.begin() as connection:
            connection.execute(text(_INSERT_JOB), job_row)
            self._insert_tasks(connection, rows)

    def _insert_tasks(self, connection: Any, rows: Sequence[Mapping[str, Any]]) -> None:
        """Write the task rows. A seam, so a test can fail it partway and prove the rollback."""
        for start in range(0, len(rows), TASK_INSERT_BATCH):
            batch = rows[start : start + TASK_INSERT_BATCH]
            if batch:
                connection.execute(text(_INSERT_TASK), list(batch))

    @staticmethod
    def _task_row(job_id: str, task: PlannedTask, now: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "seq": task.seq,
            "kind": task.kind,
            "state": TASK_PENDING,
            "priority": task.priority,
            "underlying_id": task.underlying_id,
            "contract_id": task.contract_id,
            "fyers_symbol": task.fyers_symbol,
            "expiry_date": _iso(task.expiry_date),
            "resolution": task.resolution,
            "range_from": _iso(task.range_from),
            "range_to": _iso(task.range_to),
            "include_oi": 1 if task.include_oi else 0,
            "request_params_json": (
                None
                if task.request_params is None
                else json.dumps(
                    dict(task.request_params), separators=(",", ":"), sort_keys=True
                )
            ),
            "parent_task_id": None,
            "attempt": 0,
            "max_attempts": DEFAULT_MAX_ATTEMPTS,
            # `not_before` is compared lexicographically in SQLite, so it always goes through the
            # fixed width formatter. A bare isoformat() drops microseconds when they are zero and
            # the two widths compare wrongly.
            "not_before": now,
            "created_at": now,
        }

    # -- lifecycle ----------------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any]:
        """The job row plus a fresh `GROUP BY state` aggregate over its own tasks."""
        row = self._job_row(job_id)
        if row is None:
            raise JobServiceError(
                "not_found", f"No job {job_id}.", status_code=404
            )
        aggregate = self._queue.aggregate(job_id)
        counts = dict(aggregate.counts) if aggregate else {}
        record = dict(row)
        record["counts"] = counts
        record["open_tasks"] = sum(counts.get(state, 0) for state in OPEN_STATES)
        record["children"] = self._child_ids(job_id)
        return record

    def list_jobs(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Newest first, keyset paged on `created_at` so a running job cannot shift a page."""
        where: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200)) + 1}
        if status:
            where.append("status = :status")
            params["status"] = status
        if kind:
            where.append("kind = :kind")
            params["kind"] = kind
        if since:
            where.append("created_at >= :since")
            params["since"] = since
        if cursor:
            where.append("created_at < :cursor")
            params["cursor"] = cursor
        sql = f"SELECT {_JOB_COLUMNS} FROM job"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, job_id DESC LIMIT :limit"
        with self._engine.connect() as connection:
            rows = [dict(item) for item in connection.execute(text(sql), params).mappings()]
        next_cursor = None
        if len(rows) > params["limit"] - 1:
            rows = rows[: params["limit"] - 1]
            next_cursor = str(rows[-1]["created_at"])
        return rows, next_cursor

    def pause(self, job_id: str, *, reason: str = "paused by the user") -> JobActionResult:
        """Stop leasing without touching one task row.

        The lease statement joins `job`, so a paused job simply stops being a source of work.
        Nothing pending is rewritten, which is what makes resume free.
        """
        row = self._require_live(job_id)
        if row["status"] == "paused":
            return self._action(job_id)
        if row["status"] not in ACTIVE_JOB_STATUSES:
            raise JobServiceError(
                "not_pausable",
                f"A job in status {row['status']} cannot be paused.",
                status_code=409,
                detail={"status": row["status"]},
            )
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE job SET status = 'paused', block_reason = :reason"
                    " WHERE job_id = :job_id AND status IN ('queued', 'running')"
                ),
                {"job_id": job_id, "reason": reason},
            )
        log.info("job paused", extra={"job_id": job_id})
        return self._action(job_id)

    def resume(self, job_id: str) -> JobActionResult:
        """Back to `queued`, and wake the dispatcher rather than wait out its poll."""
        row = self._require_live(job_id)
        if row["status"] in ACTIVE_JOB_STATUSES:
            return self._action(job_id)
        if row["status"] not in RESUMABLE_JOB_STATUSES:
            raise JobServiceError(
                "not_resumable",
                f"A job in status {row['status']} cannot be resumed.",
                status_code=409,
                detail={"status": row["status"]},
            )
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE job SET status = 'queued', block_reason = NULL"
                    " WHERE job_id = :job_id"
                ),
                {"job_id": job_id},
            )
        log.info("job resumed", extra={"job_id": job_id})
        self._notify(job_id)
        return self._action(job_id)

    async def cancel(self, job_id: str) -> JobActionResult:
        """Cooperative and instant: pending tasks are cancelled, in-flight ones finish.

        A cancelled job therefore leaves valid partial data plus an accurate ledger of exactly
        what it did and did not fetch, which is the reason the ledger exists.
        """
        row = self._job_row(job_id)
        if row is None:
            raise JobServiceError("not_found", f"No job {job_id}.", status_code=404)
        if row["status"] in TERMINAL_JOB_STATUSES:
            return self._action(job_id)

        if self._supervisor is not None:
            cancelled = int(await self._supervisor.cancel_job(job_id))
        else:
            with self._engine.begin() as connection:
                connection.execute(
                    text("UPDATE job SET cancel_requested = 1 WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
            cancelled = self._queue.cancel_pending(job_id)
            self._settle_if_finished(job_id)
        log.info("job cancel requested", extra={"job_id": job_id, "cancelled_tasks": cancelled})
        result = self._action(job_id)
        return JobActionResult(
            job_id=result.job_id,
            status=result.status,
            cancel_requested=result.cancel_requested,
            tasks_cancelled=cancelled,
        )

    def retry_failed(self, job_id: str) -> RetryFailedAccepted:
        """A child job holding copies of exactly the failed tasks.

        The parent's record is never rewritten. Its failures stay visible, and the child shows
        that the retry succeeded where the parent did not, which is what a user needs in order to
        trust the ledger at all.
        """
        parent = self._job_row(job_id)
        if parent is None:
            raise JobServiceError("not_found", f"No job {job_id}.", status_code=404)
        with self._engine.connect() as connection:
            failed = [
                dict(item)
                for item in connection.execute(
                    text(
                        "SELECT task_id, seq, kind, priority, underlying_id, contract_id,"
                        " fyers_symbol, expiry_date, resolution, range_from, range_to,"
                        " include_oi, request_params_json, max_attempts"
                        "  FROM task WHERE job_id = :job_id AND state = :state"
                        " ORDER BY seq"
                    ),
                    {"job_id": job_id, "state": TASK_FAILED},
                ).mappings()
            ]
        if not failed:
            raise JobServiceError(
                "no_failed_tasks",
                "This job has no failed tasks to retry.",
                status_code=409,
                detail={"job_id": job_id},
            )

        child_id = self._id_factory()
        now = iso_at(self._clock())
        rows = []
        for seq, task in enumerate(failed):
            rows.append(
                {
                    "job_id": child_id,
                    "seq": seq,
                    "kind": task["kind"],
                    "state": TASK_PENDING,
                    "priority": task["priority"],
                    "underlying_id": task["underlying_id"],
                    "contract_id": task["contract_id"],
                    "fyers_symbol": task["fyers_symbol"],
                    "expiry_date": task["expiry_date"],
                    "resolution": task["resolution"],
                    "range_from": task["range_from"],
                    "range_to": task["range_to"],
                    "include_oi": task["include_oi"],
                    "request_params_json": task["request_params_json"],
                    # The link back to the row that failed, so the child's provenance leads to
                    # the original error without duplicating it.
                    "parent_task_id": task["task_id"],
                    "attempt": 0,
                    "max_attempts": task["max_attempts"],
                    "not_before": now,
                    "created_at": now,
                }
            )
        job_row = {
            "job_id": child_id,
            "kind": parent["kind"],
            "status": "queued",
            "params_json": parent["params_json"],
            "parent_job_id": job_id,
            "schedule_id": parent["schedule_id"],
            "priority": parent["priority"],
            "est_requests": len(rows),
            "total_tasks": len(rows),
            "created_by": parent["created_by"],
            "created_at": now,
        }
        with self._engine.begin() as connection:
            connection.execute(text(_INSERT_JOB), job_row)
            self._insert_tasks(connection, rows)
        log.info(
            "retry job created",
            extra={"job_id": child_id, "parent_job_id": job_id, "total_tasks": len(rows)},
        )
        self._notify(child_id)
        return RetryFailedAccepted(
            job_id=child_id, parent_job_id=job_id, total_tasks=len(rows)
        )

    # -- helpers ------------------------------------------------------------

    def _notify(self, job_id: str) -> None:
        if self._supervisor is not None:
            self._supervisor.notify(job_id)

    def _job_row(self, job_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text(f"SELECT {_JOB_COLUMNS} FROM job WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
        return None if row is None else dict(row)

    def _require_live(self, job_id: str) -> dict[str, Any]:
        row = self._job_row(job_id)
        if row is None:
            raise JobServiceError("not_found", f"No job {job_id}.", status_code=404)
        if row["status"] in TERMINAL_JOB_STATUSES:
            raise JobServiceError(
                "job_finished",
                f"Job {job_id} is already {row['status']}.",
                status_code=409,
                detail={"status": row["status"]},
            )
        return row

    def _action(self, job_id: str) -> JobActionResult:
        row = self._job_row(job_id) or {}
        return JobActionResult(
            job_id=job_id,
            status=str(row.get("status", "")),
            cancel_requested=bool(row.get("cancel_requested", 0)),
        )

    def _child_ids(self, job_id: str) -> list[str]:
        with self._engine.connect() as connection:
            return [
                str(value)
                for value in connection.execute(
                    text("SELECT job_id FROM job WHERE parent_job_id = :job_id ORDER BY created_at"),
                    {"job_id": job_id},
                ).scalars()
            ]

    def _settle_if_finished(self, job_id: str) -> None:
        """Close a job whose tasks are all terminal. Only used without a supervisor."""
        aggregate = self._queue.aggregate(job_id)
        if aggregate is None or not aggregate.is_finished:
            return
        if aggregate.status in TERMINAL_JOB_STATUSES:
            return
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE job SET status = :status, finished_at = :now, block_reason = NULL"
                    " WHERE job_id = :job_id"
                ),
                {
                    "status": aggregate.terminal_status(),
                    "now": iso_at(self._clock()),
                    "job_id": job_id,
                },
            )


# ---------------------------------------------------------------------------
# Wiring and startup recovery
# ---------------------------------------------------------------------------


def build_planner(state: Any) -> Planner:
    """One planner, built from the services the lifespan already started."""
    if state.engine is None or state.duck_reader is None:
        raise RuntimeError("the planner needs the sqlite engine and the duckdb reader")
    return Planner(
        reader=state.duck_reader,
        engine=state.engine,
        settings=state.settings,
        governor=state.governor,
    )


def build_job_service(state: Any) -> JobService:
    """The job service a route or a schedule fire uses. Cheap: it holds no connection."""
    return JobService(
        engine=state.engine,
        planner=build_planner(state),
        supervisor=state.supervisor,
        governor=state.governor,
    )


def roll_deferred_budget_jobs(engine: Engine, *, ist_today: date | None = None) -> int:
    """Step 3 of the PIPELINE.md section 6 recovery: a new quota day releases held work.

    A job parked as `deferred_budget` was committed on a day whose budget was already spent. Once
    the IST date has moved past the day it was created on, the reason it was held no longer
    exists, so it goes back to `queued`. Comparing dates rather than clearing unconditionally
    matters for a restart inside the same day: that job is still over budget and must stay parked.
    """
    today = ist_today or datetime.now(_IST).date()
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "UPDATE job SET status = 'queued', block_reason = NULL"
                    " WHERE status = 'deferred_budget' AND substr(created_at, 1, 10) < :today"
                ),
                {"today": today.isoformat()},
            ).rowcount
        )


def restore_pipeline_mode(engine: Engine, governor: Any) -> str | None:
    """Step 4: a restart must not silently resume into a block.

    `paused_rate` and `stopped_fatal` are preserved across a restart because both mean a human
    has to look at something: the three strikes rule is per day, and a fatal stop is by
    definition not self healing. Every other mode is allowed to start clean.

    Note the standing gap this reads against: nothing in the process writes `pipeline_state.mode`
    yet, so in practice the row says `running` and this is a no-op. It is written the way the
    design specifies so that it becomes correct the moment the governor starts persisting its
    mode, rather than having to be discovered again later.
    """
    if governor is None:
        return None
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT mode, reason FROM pipeline_state WHERE id = 1")
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    mode = str(row["mode"])
    if mode not in ("paused_rate", "stopped_fatal"):
        return None
    setter = getattr(governor, "_set_mode_sync", None)
    if setter is None:  # pragma: no cover - a governor stub without the mode machine
        return None
    setter(type(governor.mode)(mode), reason=row["reason"])
    log.info("pipeline mode restored across restart", extra={"mode": mode})
    return mode


def recover_jobs(state: Any) -> None:
    """The factory the lifespan's job recovery slot calls. Idempotent, holds nothing.

    Steps 1 and 2 already ran inside `PipelineSupervisor.start`, which the lifespan orders before
    this slot. `reclaim_and_recover` is called anyway because it is idempotent and because this
    slot has to be correct on its own when the supervisor slot is empty.
    """
    from expirymanager.pipeline.supervisor import reclaim_and_recover

    engine = state.engine
    if engine is None:
        return None
    recovered = reclaim_and_recover(engine)
    released = roll_deferred_budget_jobs(engine)
    mode = restore_pipeline_mode(engine, state.governor)
    log.info(
        "job recovery complete",
        extra={
            "reclaimed": recovered.get("reclaimed", 0),
            "interrupted_jobs": recovered.get("interrupted_jobs", 0),
            "deferred_released": released,
            "mode": mode or "running",
        },
    )
    supervisor = state.supervisor
    if released and supervisor is not None:
        supervisor.notify()
    return None


def install() -> None:
    """Register the job recovery with the lifespan's named slot.

    Explicit rather than done at import, for the same reason the supervisor's install is:
    registering as a side effect of importing this module would silently change what an
    application built by a test starts.
    """
    from expirymanager.lifespan import SLOT_JOB_RECOVERY, register_component

    register_component(SLOT_JOB_RECOVERY, recover_jobs)


def _iso(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()
