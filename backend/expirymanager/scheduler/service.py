"""The scheduler: an APScheduler `AsyncIOScheduler` rebuilt from SQLite on every mutation.

Jobstore: `MemoryJobStore` only, and that is a decision rather than a default. `sqlite.schedule`
is the persistent source of truth and `sync()` rebuilds every trigger from it at startup and after
every create, update, enable, disable or delete. A persistent APScheduler jobstore would be a
second, opaque source of truth that the UI cannot render or repair, pickled triggers survive
across upgrades in surprising ways, and a schedule the user deleted can come back. With this
design what the schedules screen shows is exactly what will fire, and there is nothing else.

Every trigger is a `CronTrigger` in the row's timezone with `coalesce=True`, `max_instances=1` and
the row's own `misfire_grace_time`, so a laptop that was asleep at 18:00 runs the job once when it
wakes rather than four times or not at all.

Two guards sit above every fire, and they are the reason the scheduler can never be the thing that
spends the third strike:

1. needs_reauth, or a pipeline parked on auth, records `skipped_needs_auth` with a notification.
2. A broker block still in force, a stopped pipeline, or an exhausted sweep reserve records
   `skipped_blocked` or `skipped_budget`.

Both guards are skipped for the kinds that need neither a token nor budget. The symbol master is
the one that matters: it reads unauthenticated public files, a missed day is unrecoverable, and
holding it behind the auth guard would mean the one job that must survive a dead token is the
first one a dead token stops.

Run-now runs the identical body the cron trigger runs, guards included, and writes the same
`schedule_run` row. There is one fire path, so what the user tests with the button is what will
happen at 18:30.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from expirymanager.api.schemas.schedules import (
    DEFAULT_RUN_HISTORY,
    DEFAULT_TIMEZONE,
    MAX_RUN_HISTORY,
    CronError,
    RunNowResult,
    ScheduleCreate,
    ScheduleRow,
    ScheduleRunRow,
    ScheduleUpdate,
    SchedulerSnapshot,
    validate_cron,
)
from expirymanager.pipeline.queue import iso_at, utc_now
from expirymanager.scheduler import jobs_def
from expirymanager.scheduler.jobs_def import (
    OUTCOME_ERROR,
    OUTCOME_SKIPPED_BLOCKED,
    OUTCOME_SKIPPED_BUDGET,
    OUTCOME_SKIPPED_DISABLED,
    OUTCOME_SKIPPED_HOLIDAY,
    OUTCOME_SKIPPED_NEEDS_AUTH,
    FireContext,
    FireResult,
    ScheduleSpec,
)

__all__ = [
    "IST",
    "SchedulerError",
    "ScheduleNotFound",
    "BuiltinSchedule",
    "UnknownScheduleKind",
    "InvalidCron",
    "SchedulerService",
    "build_scheduler",
    "install",
]

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_SCHEDULE_COLUMNS = (
    "schedule_id, name, kind, cron, timezone, params_json, enabled, trading_days_only,"
    " misfire_grace_seconds, max_requests_per_run, is_builtin, last_fired_at, next_fire_at,"
    " created_at, updated_at"
)


class SchedulerError(Exception):
    """Base for a refused schedule command. Carries the documented API code."""

    code = "scheduler_error"
    status_code = 400

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ScheduleNotFound(SchedulerError):
    code = "not_found"
    status_code = 404


class BuiltinSchedule(SchedulerError):
    code = "builtin_schedule"
    status_code = 409


class UnknownScheduleKind(SchedulerError):
    code = "unknown_kind"
    status_code = 400


class InvalidCron(SchedulerError):
    code = "invalid_cron"
    status_code = 400


class SchedulerService:
    """Owns the APScheduler instance and the `schedule` table it is rebuilt from."""

    def __init__(
        self,
        *,
        engine: Engine,
        services: Any = None,
        job_service_factory: Any = None,
        clock: Any = None,
        id_factory: Any = None,
        scheduler: Any = None,
        autostart: bool = True,
    ) -> None:
        self._engine = engine
        self._services = services
        self._job_service_factory = job_service_factory
        # The clock returns an IST-aware datetime. Tests inject a fake one and every guard,
        # every trading day check and every `fired_at` reads through it, so a fire is reproducible
        # without waiting for a wall clock minute to pass.
        self._clock = clock or (lambda: datetime.now(IST))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._scheduler = scheduler
        self._autostart = autostart
        self._owns_scheduler = scheduler is None
        self._started = False
        self._fire_lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    @property
    def scheduler(self) -> Any:
        return self._scheduler

    async def start(self) -> None:
        """Build the scheduler, install every enabled trigger and start ticking."""
        if self._started:
            return
        if self._scheduler is None:
            from apscheduler.jobstores.memory import MemoryJobStore
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            self._scheduler = AsyncIOScheduler(
                jobstores={"default": MemoryJobStore()},
                timezone=IST,
                job_defaults={"coalesce": True, "max_instances": 1},
            )
        if self._autostart and not self._scheduler.running:
            self._scheduler.start()
        self._started = True
        installed = self.sync()
        log.info("scheduler started", extra={"schedules_installed": installed})

    async def stop(self) -> None:
        if self._scheduler is not None and self._owns_scheduler:
            with contextlib.suppress(Exception):
                # wait=False: a fire in flight has already committed its job row, and a shutdown
                # that blocked on a running plan would hold the DuckDB writer open past teardown.
                self._scheduler.shutdown(wait=False)
        self._started = False
        log.info("scheduler stopped")

    # -- the rebuild --------------------------------------------------------

    def sync(self) -> int:
        """Rebuild every APScheduler job from the table. Returns how many are installed.

        Called at startup and after every mutation. It removes everything first rather than
        diffing, because a diff has to be right about which fields change a trigger and a rebuild
        cannot be wrong: the table is the truth, twelve triggers cost microseconds to build, and
        the property that matters is that nothing survives that the table does not name.
        """
        if self._scheduler is None:
            return 0
        self._scheduler.remove_all_jobs()
        installed = 0
        for spec in self.list_specs():
            if not spec.enabled:
                continue
            if jobs_def.get_action(spec.kind) is None:
                # Installed nothing rather than raising. A row whose kind has no action is a
                # schedule that cannot work, but refusing to start the whole scheduler over one
                # bad row would take down the other eleven.
                log.warning(
                    "schedule kind has no action, the trigger was not installed",
                    extra={"schedule_id": spec.schedule_id, "schedule_kind": spec.kind},
                )
                continue
            try:
                trigger = self._trigger(spec)
            except CronError:
                log.warning(
                    "schedule cron is not runnable, the trigger was not installed",
                    extra={"schedule_id": spec.schedule_id},
                )
                continue
            self._scheduler.add_job(
                self._fire_from_trigger,
                trigger=trigger,
                id=spec.schedule_id,
                name=spec.name,
                args=[spec.schedule_id],
                coalesce=True,
                max_instances=1,
                misfire_grace_time=spec.misfire_grace_seconds or None,
                replace_existing=True,
            )
            installed += 1
        self._write_next_fire_times()
        return installed

    def _trigger(self, spec: ScheduleSpec) -> Any:
        from apscheduler.triggers.cron import CronTrigger

        expression = validate_cron(spec.cron, spec.timezone or DEFAULT_TIMEZONE)
        return CronTrigger.from_crontab(
            expression, timezone=ZoneInfo(spec.timezone or DEFAULT_TIMEZONE)
        )

    def _write_next_fire_times(self) -> None:
        """Mirror what APScheduler computed back onto the rows the UI renders.

        The next fire time is derived state, not truth, so it is written after the rebuild rather
        than maintained by hand. A disabled schedule gets NULL, which is what makes the screen
        say "not scheduled" instead of showing a time that will never arrive.
        """
        if self._scheduler is None:
            return
        installed: dict[str, str | None] = {}
        for job in self._scheduler.get_jobs():
            moment = getattr(job, "next_run_time", None)
            installed[job.id] = moment.isoformat() if moment is not None else None
        with self._engine.begin() as connection:
            connection.execute(text("UPDATE schedule SET next_fire_at = NULL"))
            for schedule_id, moment in installed.items():
                connection.execute(
                    text(
                        "UPDATE schedule SET next_fire_at = :moment WHERE schedule_id = :id"
                    ),
                    {"moment": moment, "id": schedule_id},
                )

    # -- reads --------------------------------------------------------------

    def list_specs(self) -> list[ScheduleSpec]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(f"SELECT {_SCHEDULE_COLUMNS} FROM schedule ORDER BY kind, name")
                )
                .mappings()
                .all()
            )
        return [ScheduleSpec.from_row(row) for row in rows]

    def get_spec(self, schedule_id: str) -> ScheduleSpec:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text(f"SELECT {_SCHEDULE_COLUMNS} FROM schedule WHERE schedule_id = :id"),
                    {"id": schedule_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ScheduleNotFound(f"no schedule with id {schedule_id!r}")
        return ScheduleSpec.from_row(row)

    def list_schedules(self) -> list[ScheduleRow]:
        """Every schedule, with its last run folded in. What `GET /api/v1/schedules` renders."""
        last = self._last_runs()
        out: list[ScheduleRow] = []
        for spec in self.list_specs():
            meta = jobs_def.action_meta(spec.kind)
            entry = last.get(spec.schedule_id)
            out.append(
                ScheduleRow(
                    schedule_id=spec.schedule_id,
                    name=spec.name,
                    kind=spec.kind,
                    cron=spec.cron,
                    timezone=spec.timezone,
                    enabled=spec.enabled,
                    trading_days_only=spec.trading_days_only,
                    misfire_grace_seconds=spec.misfire_grace_seconds,
                    max_requests_per_run=spec.max_requests_per_run,
                    is_builtin=spec.is_builtin,
                    params=dict(spec.params),
                    next_fire_at=spec.next_fire_at,
                    last_fired_at=spec.last_fired_at,
                    last_outcome=None if entry is None else entry["outcome"],
                    last_job_id=None if entry is None else entry["job_id"],
                    description=None if meta is None else (meta.description or None),
                )
            )
        return out

    def _last_runs(self) -> dict[str, dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT r.schedule_id, r.outcome, r.job_id, r.fired_at FROM schedule_run r"
                        " JOIN (SELECT schedule_id, max(fired_at) AS newest FROM schedule_run"
                        "        GROUP BY schedule_id) latest"
                        "   ON latest.schedule_id = r.schedule_id AND latest.newest = r.fired_at"
                    )
                )
                .mappings()
                .all()
            )
        return {str(row["schedule_id"]): dict(row) for row in rows}

    def runs(self, schedule_id: str, *, limit: int = DEFAULT_RUN_HISTORY) -> list[ScheduleRunRow]:
        """Run history for one schedule, newest first."""
        self.get_spec(schedule_id)
        bounded = max(1, min(int(limit), MAX_RUN_HISTORY))
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT run_id, schedule_id, fired_at, job_id, outcome, note"
                        " FROM schedule_run WHERE schedule_id = :id"
                        " ORDER BY fired_at DESC, rowid DESC LIMIT :limit"
                    ),
                    {"id": schedule_id, "limit": bounded},
                )
                .mappings()
                .all()
            )
        return [ScheduleRunRow(**dict(row)) for row in rows]

    def snapshot(self) -> SchedulerSnapshot:
        if self._scheduler is None:
            return SchedulerSnapshot(running=False)
        jobs = list(self._scheduler.get_jobs())
        moments = [
            job.next_run_time for job in jobs if getattr(job, "next_run_time", None) is not None
        ]
        return SchedulerSnapshot(
            running=bool(getattr(self._scheduler, "running", False)),
            job_count=len(jobs),
            timezone=DEFAULT_TIMEZONE,
            next_fire_at=min(moments).isoformat() if moments else None,
            schedule_ids=[job.id for job in jobs],
        )

    # -- mutations ----------------------------------------------------------

    def create(self, body: ScheduleCreate) -> ScheduleRow:
        if jobs_def.get_action(body.kind) is None:
            raise UnknownScheduleKind(
                f"{body.kind!r} is not a schedule kind this build knows how to run",
                detail={"known_kinds": list(jobs_def.known_kinds())},
            )
        try:
            cron = validate_cron(body.cron, body.timezone or DEFAULT_TIMEZONE)
        except CronError as exc:
            raise InvalidCron(str(exc)) from exc
        schedule_id = self._id_factory()
        now = iso_at(utc_now())
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO schedule (schedule_id, name, kind, cron, timezone, params_json,"
                    " enabled, trading_days_only, misfire_grace_seconds, max_requests_per_run,"
                    " is_builtin, created_at, updated_at)"
                    " VALUES (:schedule_id, :name, :kind, :cron, :timezone, :params_json,"
                    " :enabled, :trading_days_only, :misfire_grace_seconds, :max_requests_per_run,"
                    " 0, :created_at, :updated_at)"
                ),
                {
                    "schedule_id": schedule_id,
                    "name": body.name,
                    "kind": body.kind,
                    "cron": cron,
                    "timezone": body.timezone or DEFAULT_TIMEZONE,
                    "params_json": json.dumps(body.params, sort_keys=True),
                    "enabled": 1 if body.enabled else 0,
                    "trading_days_only": 1 if body.trading_days_only else 0,
                    "misfire_grace_seconds": int(body.misfire_grace_seconds),
                    "max_requests_per_run": body.max_requests_per_run,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        self.sync()
        return self._row_for(schedule_id)

    def update(self, schedule_id: str, body: ScheduleUpdate) -> ScheduleRow:
        """Patch any editable field. A builtin may be re-timed and disabled, never re-kinded.

        `ScheduleUpdate` has no `kind` field at all, so a builtin cannot be pointed at another
        action even by accident, and its run history stays a record of the thing it names.
        """
        spec = self.get_spec(schedule_id)
        assignments: dict[str, Any] = {}
        if body.name is not None:
            assignments["name"] = body.name
        if body.cron is not None:
            timezone = body.timezone or spec.timezone
            try:
                assignments["cron"] = validate_cron(body.cron, timezone)
            except CronError as exc:
                raise InvalidCron(str(exc)) from exc
        if body.timezone is not None:
            assignments["timezone"] = body.timezone
        if body.params is not None:
            assignments["params_json"] = json.dumps(body.params, sort_keys=True)
        if body.enabled is not None:
            assignments["enabled"] = 1 if body.enabled else 0
        if body.trading_days_only is not None:
            assignments["trading_days_only"] = 1 if body.trading_days_only else 0
        if body.misfire_grace_seconds is not None:
            assignments["misfire_grace_seconds"] = int(body.misfire_grace_seconds)
        if body.max_requests_per_run is not None:
            assignments["max_requests_per_run"] = int(body.max_requests_per_run)
        if assignments:
            assignments["updated_at"] = iso_at(utc_now())
            clause = ", ".join(f"{column} = :{column}" for column in assignments)
            with self._engine.begin() as connection:
                connection.execute(
                    text(f"UPDATE schedule SET {clause} WHERE schedule_id = :schedule_id"),
                    {**assignments, "schedule_id": schedule_id},
                )
        self.sync()
        return self._row_for(schedule_id)

    def set_enabled(self, schedule_id: str, enabled: bool) -> ScheduleRow:
        return self.update(schedule_id, ScheduleUpdate(enabled=enabled))

    def delete(self, schedule_id: str) -> None:
        spec = self.get_spec(schedule_id)
        if spec.is_builtin:
            raise BuiltinSchedule(
                f"{spec.name} is a builtin schedule. It can be disabled or re-timed, but "
                "deleting it would leave a feature with nothing to drive it."
            )
        with self._engine.begin() as connection:
            connection.execute(
                text("DELETE FROM schedule_run WHERE schedule_id = :id"), {"id": schedule_id}
            )
            connection.execute(
                text("DELETE FROM schedule WHERE schedule_id = :id"), {"id": schedule_id}
            )
        self.sync()

    def _row_for(self, schedule_id: str) -> ScheduleRow:
        for row in self.list_schedules():
            if row.schedule_id == schedule_id:
                return row
        raise ScheduleNotFound(f"no schedule with id {schedule_id!r}")

    # -- firing -------------------------------------------------------------

    async def _fire_from_trigger(self, schedule_id: str) -> None:
        """What APScheduler calls. Never raises: a raising trigger is a dead trigger."""
        try:
            await self.fire(schedule_id, triggered_by="cron")
        except Exception:  # noqa: BLE001 - the scheduler must survive one bad fire
            log.exception("schedule fire failed", extra={"schedule_id": schedule_id})

    async def run_now(self, schedule_id: str) -> RunNowResult:
        """`POST /api/v1/schedules/{id}/run-now`.

        Runs the identical body the cron trigger runs, both guards included, so the button is a
        test of the real thing rather than of a second code path that happens to look like it.
        The disabled check is the one difference: a user asking explicitly is not a misfire.
        """
        return await self.fire(schedule_id, triggered_by="run_now", ignore_disabled=True)

    async def fire(
        self,
        schedule_id: str,
        *,
        triggered_by: str = "cron",
        ignore_disabled: bool = False,
    ) -> RunNowResult:
        """The one fire path. Guards, action, `schedule_run` row, event frame."""
        spec = self.get_spec(schedule_id)
        now = self._clock()
        async with self._fire_lock:
            result = await self._guarded(spec, now, ignore_disabled=ignore_disabled)
            return self._record(spec, now, result)

    async def _guarded(
        self, spec: ScheduleSpec, now: datetime, *, ignore_disabled: bool
    ) -> FireResult:
        if not spec.enabled and not ignore_disabled:
            return FireResult(OUTCOME_SKIPPED_DISABLED, (), "the schedule is disabled")

        action = jobs_def.get_action(spec.kind)
        if action is None:
            return FireResult(
                OUTCOME_ERROR, (), f"no action is registered for the kind {spec.kind!r}"
            )

        if spec.trading_days_only:
            holiday = self._non_trading_reason(now)
            if holiday is not None:
                return FireResult(OUTCOME_SKIPPED_HOLIDAY, (), holiday)

        if jobs_def.needs_token(spec.kind):
            reason = self._auth_guard()
            if reason is not None:
                self._notify_needs_auth(spec, reason)
                return FireResult(OUTCOME_SKIPPED_NEEDS_AUTH, (), reason)

        if jobs_def.spends_budget(spec.kind):
            outcome = self._budget_guard()
            if outcome is not None:
                return FireResult(outcome[0], (), outcome[1])

        ctx = FireContext(
            schedule=spec,
            services=self._services,
            engine=self._engine,
            now=now,
            job_service=self._build_job_service(),
            triggered_by="cron",
            id_factory=self._id_factory,
        )
        try:
            return await action(ctx)
        except Exception as exc:  # noqa: BLE001 - one bad action must not stop the scheduler
            log.exception("schedule action failed", extra={"schedule_id": spec.schedule_id})
            return FireResult(OUTCOME_ERROR, (), f"{type(exc).__name__}: {exc}")

    # -- the two guards -----------------------------------------------------

    def _non_trading_reason(self, now: datetime) -> str | None:
        """Whether today is a trading day on any exchange the registry actually uses.

        Any rather than all: NSE and BSE publish the same list today, but a schedule that stopped
        firing because one exchange took a holiday would silently skip the other one's data, and
        the per-underlying planning inside each action already knows its own calendar.
        """
        today = now.date()
        with self._engine.connect() as connection:
            exchanges = (
                connection.execute(
                    text(
                        "SELECT DISTINCT exchange FROM underlying_registry"
                        " WHERE is_active = 1 AND exchange <> 'MCX'"
                    )
                )
                .scalars()
                .all()
            )
        if not exchanges:
            return None
        for exchange in exchanges:
            calendar, _loaded = jobs_def.load_calendar(self._engine, str(exchange))
            if calendar.is_trading_day(str(exchange), today):
                return None
        if today.weekday() >= 5:
            return f"{today.isoformat()} is a weekend"
        return f"{today.isoformat()} is an exchange holiday"

    def _auth_guard(self) -> str | None:
        """Guard one: needs_reauth, or a pipeline already parked on auth."""
        broker = getattr(self._services, "token_broker", None)
        if broker is None:
            # No broker at all is the same answer as no token. A kind that needs one must not be
            # allowed to run and discover that eight requests later.
            return "the Fyers token broker is not available"
        try:
            if not broker.has_valid_token():
                return (
                    "the Fyers access token is missing or expired, so the run was skipped "
                    "rather than spent on requests that would be rejected"
                )
        except Exception:  # noqa: BLE001 - an unreadable token is a reason to skip, not crash
            return "the stored Fyers token could not be read"
        governor = getattr(self._services, "governor", None)
        mode = str(getattr(governor, "mode", "running")) if governor is not None else "running"
        if mode == "paused_auth":
            return "the pipeline is parked awaiting authentication"
        return None

    def _budget_guard(self) -> tuple[str, str] | None:
        """Guard two: a broker block, a stopped pipeline, or an exhausted sweep reserve."""
        governor = getattr(self._services, "governor", None)
        if governor is None:
            return None
        try:
            snapshot = governor.snapshot()
        except Exception:  # noqa: BLE001
            return None
        mode = str(snapshot.mode)
        blocked_until = snapshot.blocked_until
        if blocked_until:
            try:
                until = datetime.fromisoformat(str(blocked_until))
            except ValueError:
                until = None
            if until is not None and self._clock() < until:
                return (
                    OUTCOME_SKIPPED_BLOCKED,
                    f"the broker has blocked the account until {blocked_until}",
                )
        if mode in ("stopped_fatal", "paused_rate", "paused_user"):
            return (OUTCOME_SKIPPED_BLOCKED, f"the pipeline is {mode}")
        if mode == "stopped_budget":
            return (OUTCOME_SKIPPED_BUDGET, "today's request budget is spent")
        reserve = self._reserve_allowance(snapshot)
        if reserve <= 0:
            return (
                OUTCOME_SKIPPED_BUDGET,
                "the sweep reserve for today is exhausted, so the run was deferred rather than "
                "allowed to eat into the budget an interactive download needs",
            )
        return None

    def _reserve_allowance(self, snapshot: Any) -> int:
        """How many requests a sweep may still spend today.

        The same reserve the planner prices against, read here so a fire that could not commit a
        single task records `skipped_budget` instead of running every underlying's planner to
        discover the same thing eight times.
        """
        settings = getattr(self._services, "settings", None)
        fraction = 0.70
        if settings is not None:
            with contextlib.suppress(Exception):
                raw = settings.get("budget_reserve_fraction")
                if raw is not None:
                    fraction = float(raw)
        allowance = int(snapshot.daily_budget * fraction) - int(snapshot.requests_used)
        return max(0, allowance)

    def _notify_needs_auth(self, spec: ScheduleSpec, reason: str) -> None:
        ctx = FireContext(
            schedule=spec,
            services=self._services,
            engine=self._engine,
            now=self._clock(),
        )
        jobs_def._notify(
            ctx,
            "warning",
            "needs_reauth",
            "A scheduled run was skipped",
            f"{spec.name} did not run because {reason}.",
        )

    # -- recording ----------------------------------------------------------

    def _record(self, spec: ScheduleSpec, now: datetime, result: FireResult) -> RunNowResult:
        run_id = self._id_factory()
        fired_at = now.isoformat()
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO schedule_run (run_id, schedule_id, fired_at, job_id, outcome,"
                    " note) VALUES (:run_id, :schedule_id, :fired_at, :job_id, :outcome, :note)"
                ),
                {
                    "run_id": run_id,
                    "schedule_id": spec.schedule_id,
                    "fired_at": fired_at,
                    "job_id": result.job_id,
                    "outcome": result.outcome,
                    "note": self._note(result),
                },
            )
            connection.execute(
                text(
                    "UPDATE schedule SET last_fired_at = :fired_at WHERE schedule_id = :id"
                ),
                {"fired_at": fired_at, "id": spec.schedule_id},
            )
        self._publish(spec, result)
        log.info(
            "schedule fired",
            extra={
                "schedule_id": spec.schedule_id,
                "schedule_kind": spec.kind,
                "outcome": result.outcome,
                "job_id": result.job_id or "",
            },
        )
        return RunNowResult(
            schedule_id=spec.schedule_id,
            run_id=run_id,
            job_id=result.job_id,
            outcome=result.outcome,
            note=self._note(result),
        )

    @staticmethod
    def _note(result: FireResult) -> str | None:
        """The note, with the extra job ids appended when a fire created more than one.

        One fire can create several jobs, because `PlanRequest` prices exactly one underlying and
        a nightly sweep covers all of them. `schedule_run.job_id` carries the first, so the rest
        are named in the note rather than lost.
        """
        note = result.note
        if len(result.job_ids) > 1:
            extra = ", ".join(result.job_ids[1:])
            note = f"{note or ''}; also created {extra}".strip("; ")
        return note

    def _publish(self, spec: ScheduleSpec, result: FireResult) -> None:
        supervisor = getattr(self._services, "supervisor", None)
        bus = getattr(supervisor, "bus", None)
        if bus is None:
            return
        from expirymanager.pipeline.events import EVENT_SCHEDULE_FIRED

        with contextlib.suppress(Exception):
            bus.publish(
                EVENT_SCHEDULE_FIRED,
                {
                    "schedule_id": spec.schedule_id,
                    "job_id": result.job_id,
                    "outcome": result.outcome,
                },
            )

    def _build_job_service(self) -> Any:
        if self._job_service_factory is not None:
            return self._job_service_factory()
        if self._services is None:
            return None
        try:
            from expirymanager.pipeline.jobs import build_job_service

            return build_job_service(self._services)
        except Exception:  # noqa: BLE001 - a degraded startup must not make every fire raise
            log.warning("the job service could not be built for this fire")
            return None


# ---------------------------------------------------------------------------
# Lifespan wiring
# ---------------------------------------------------------------------------


def build_scheduler(state: Any) -> SchedulerService:
    """The factory the lifespan's scheduler slot calls."""
    if state.engine is None:
        raise RuntimeError("the scheduler needs the sqlite engine")
    return SchedulerService(engine=state.engine, services=state)


def install() -> None:
    """Register the scheduler with the lifespan's named slot.

    Explicit rather than an import side effect, for the same reason the supervisor's and the job
    service's installs are: registering by importing would silently change what an application
    built by a test starts, and three items would each be doing it invisibly.
    """
    from expirymanager.lifespan import SLOT_SCHEDULER, register_component

    register_component(SLOT_SCHEDULER, build_scheduler)
