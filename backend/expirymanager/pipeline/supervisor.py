"""The pipeline supervisor: the pool, the gates, the reclaim loop and the job lifecycle.

This is the object the lifespan holds in `SLOT_PIPELINE_SUPERVISOR`. It owns nothing that does the
work and everything that decides whether work may happen at all.

**Two gates, and why they are separate.** The auth gate is an `asyncio.Event` that is set while a
usable token exists. It belongs to the token broker, not to this class, because the broker is what
learns about a login, a logout and a rejection, and two events would drift the moment one of those
landed while the other was being read. The run gate mirrors the governor's mode, which covers user
pause, rate limit strikes, budget exhaustion and a fatal stop. Both must be open before a request
may be spent, and both are checked by the dispatcher before it claims anything as well as by each
worker after it has claimed, because the window between those two points is exactly where a token
expires in practice.

**Parking, not failing.** An auth error moves affected jobs to `blocked_auth` and leaves their tasks
`pending` with the leases released. Nothing is marked failed and no attempt is consumed. Since SEBI
discontinued the refresh token flow from 1 April 2026 there is no refresh path to attempt: parking
and telling the user is the entire recovery, and `on_login` is what un-parks. The concurrency
collapse lives in `TokenBroker.on_auth_error`, whose generation guard makes eight simultaneous
rejections produce exactly one park and one banner rather than eight.

**Cancellation is cooperative and instant.** `cancel_job` sets `cancel_requested`, which the lease
statement filters on, and bulk updates the remaining `pending` rows to `cancelled` in one
statement. Tasks already leased finish and write their data, so a cancelled job leaves valid partial
data plus an accurate ledger of exactly what it did and did not fetch.

**The reclaim loop returns lost leases without burning an attempt.** It runs once at startup and
then on a timer, and it is the only thing standing between a hard kill and up to sixteen rows that
nobody would ever pick up again.

Note on the governor: the worker does NOT take a governor slot. `FyersClient` already holds one for
the duration of every outbound request, and it is the only thing that talks to Fyers, so acquiring a
second slot in the worker would double count the daily budget against a limit whose fourth violation
costs the rest of the day. What the supervisor takes from the governor is its mode, which is what
the run gate reflects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import Engine, bindparam, text

from expirymanager.pipeline.dispatcher import DEFAULT_POLL_INTERVAL, Dispatcher
from expirymanager.pipeline.events import (
    EVENT_AUTH_REQUIRED,
    EVENT_JOB_BLOCKED,
    EVENT_JOB_FINISHED,
    EVENT_PIPELINE_MODE,
    EVENT_RATE_LIMITED,
    EventBus,
)
from expirymanager.pipeline.progress import DEFAULT_INTERVAL_SECONDS, ProgressAggregator
from expirymanager.pipeline.queue import (
    DEFAULT_LEASE_SECONDS,
    LeaseQueue,
    iso_at,
    utc_now,
)
from expirymanager.pipeline.worker import Worker

__all__ = [
    "DEFAULT_RECLAIM_INTERVAL",
    "DEFAULT_MODE_POLL_INTERVAL",
    "BLOCK_REASON_INTERRUPTED",
    "PipelineSupervisor",
    "reclaim_and_recover",
    "build_supervisor",
    "install",
]

log = logging.getLogger(__name__)

# PIPELINE.md section 3.1 names 60 seconds, against a 120 second lease. Two ticks inside one lease
# means a lost lease is picked up well before anything else notices it went missing.
DEFAULT_RECLAIM_INTERVAL = 60.0

# How often the governor's mode is read. A property read on an in-process object, so the cost is
# irrelevant; what matters is that a user resume from `paused_rate` reaches the dispatcher without
# the resume path having to know the supervisor exists.
DEFAULT_MODE_POLL_INTERVAL = 0.5

BLOCK_REASON_INTERRUPTED = "interrupted"

_RUNNING_MODE = "running"
_MODE_TO_JOB_STATUS = {
    "paused_auth": "blocked_auth",
    "paused_rate": "blocked_rate",
}

ThreadRunner = Callable[..., Awaitable[Any]]


async def _default_thread_runner(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Recovery, which the lifespan runs before the scheduler is allowed to fire
# ---------------------------------------------------------------------------


def reclaim_and_recover(engine: Engine, *, queue: LeaseQueue | None = None) -> dict[str, int]:
    """The startup recovery of PIPELINE.md section 6, steps 1 and 2.

    Returns leased rows to pending without touching `attempt`, then moves any job left `running` by
    a crash to `paused` with `block_reason = 'interrupted'` so the dashboard can offer a Resume
    prompt rather than silently restarting a multi-day backfill the user may no longer want.

    Idempotent, so it is safe for W12's recovery slot to call it as well. Steps 3 and 4 of that
    section, the deferred_budget date roll and the preserved pipeline mode, belong to the job
    service and the scheduler and are deliberately not done here.
    """
    lease_queue = queue or LeaseQueue(engine)
    reclaimed = lease_queue.reclaim_expired_leases()
    with engine.begin() as connection:
        interrupted = connection.execute(
            text(
                "UPDATE job SET status = 'paused', block_reason = :reason"
                " WHERE status = 'running'"
            ),
            {"reason": BLOCK_REASON_INTERRUPTED},
        ).rowcount
    if reclaimed or interrupted:
        log.info(
            "pipeline recovery complete",
            extra={"reclaimed": reclaimed, "interrupted_jobs": interrupted},
        )
    return {"reclaimed": reclaimed, "interrupted_jobs": interrupted}


# ---------------------------------------------------------------------------
# The supervisor
# ---------------------------------------------------------------------------


class PipelineSupervisor:
    """Owns the worker pool, the gates, the reclaim loop and the job lifecycle."""

    def __init__(
        self,
        *,
        engine: Engine,
        settings: Any = None,
        governor: Any = None,
        token_broker: Any = None,
        bus: EventBus | None = None,
        services: Any = None,
        queue: LeaseQueue | None = None,
        worker_count: int | None = None,
        lease_seconds: int | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        reclaim_interval: float = DEFAULT_RECLAIM_INTERVAL,
        progress_interval: float = DEFAULT_INTERVAL_SECONDS,
        mode_poll_interval: float = DEFAULT_MODE_POLL_INTERVAL,
        clock: Callable[[], Any] = utc_now,
        to_thread: ThreadRunner | None = None,
        recover_on_start: bool = True,
        auto_reclaim: bool = True,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._governor = governor
        self._token_broker = token_broker
        self._services = services
        self._clock = clock
        self._to_thread = to_thread or _default_thread_runner
        self._recover_on_start = recover_on_start
        self._auto_reclaim = auto_reclaim
        self._reclaim_interval = reclaim_interval
        self._mode_poll_interval = mode_poll_interval

        self.bus = bus if bus is not None else EventBus()
        self.worker_count = worker_count or self._setting_int("worker_count", 8)
        self.queue = queue or LeaseQueue(
            engine,
            clock=clock,
            lease_seconds=lease_seconds or DEFAULT_LEASE_SECONDS,
        )
        self.dispatcher = Dispatcher(
            self.queue,
            worker_count=self.worker_count,
            owner=f"worker-{uuid.uuid4().hex[:12]}",
            poll_interval=poll_interval,
            bus=self.bus,
            to_thread=self._to_thread,
        )
        self.progress = ProgressAggregator(
            self.queue,
            self.bus,
            interval=progress_interval,
            effective_rpm=float(self._setting_int("throttle_per_minute", 170)),
            to_thread=self._to_thread,
        )

        # The auth gate belongs to the token broker when there is one, so there is exactly one
        # event in the process and a login that lands anywhere opens it everywhere.
        if token_broker is not None and hasattr(token_broker, "auth_gate"):
            self.auth_gate: asyncio.Event = token_broker.auth_gate
        else:
            self.auth_gate = asyncio.Event()
            self.auth_gate.set()

        # Mirrors the governor's mode. Separate from the auth gate because a rate limit strike and
        # a dead token are different problems with different resumes: one needs an explicit human
        # decision, the other needs a login.
        self.run_gate = asyncio.Event()
        self.run_gate.set()

        self._workers: list[Worker] = []
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._reclaim_task: asyncio.Task[None] | None = None
        self._mode_task: asyncio.Task[None] | None = None
        self._started = False
        self._last_mode = _RUNNING_MODE
        self._park_lock = asyncio.Lock()

    # -- settings -----------------------------------------------------------

    def _setting_int(self, key: str, fallback: int) -> int:
        if self._settings is None:
            return fallback
        try:
            return int(self._settings.get_int(key))
        except Exception:  # noqa: BLE001 - a settings read must never stop the pipeline starting
            return fallback

    # -- inspection ---------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    @property
    def workers(self) -> tuple[Worker, ...]:
        return tuple(self._workers)

    @property
    def owner(self) -> str:
        return self.dispatcher.owner

    def gates_open(self) -> bool:
        """Both gates. Read by the dispatcher before a claim and by a worker after one."""
        return self.auth_gate.is_set() and self.run_gate.is_set()

    def token_generation(self) -> int:
        if self._token_broker is None:
            return 0
        try:
            return int(self._token_broker.generation)
        except Exception:  # noqa: BLE001 - a broker without a generation is a test double
            return 0

    def governor_mode(self) -> str:
        if self._governor is None:
            return _RUNNING_MODE
        return str(getattr(self._governor, "mode", _RUNNING_MODE))

    def snapshot(self) -> dict[str, Any]:
        """What the diagnostics endpoint renders. No secrets, no token, no symbol."""
        return {
            "started": self._started,
            "worker_count": self.worker_count,
            "owner": self.owner,
            "buffered": self.dispatcher.buffered,
            "buffer_capacity": self.dispatcher.capacity,
            "leased_total": self.dispatcher.leased_total,
            "dispatcher_paused": self.dispatcher.paused,
            "auth_gate_open": self.auth_gate.is_set(),
            "run_gate_open": self.run_gate.is_set(),
            "mode": self.governor_mode(),
            "completed": sum(worker.completed for worker in self._workers),
            "failed": sum(worker.failed for worker in self._workers),
            "parked": sum(worker.parked for worker in self._workers),
        }

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        if self._recover_on_start:
            await self._to_thread(reclaim_and_recover, self._engine, queue=self.queue)
        self._sync_gates()
        self._last_mode = self.governor_mode()

        await self.dispatcher.start()
        if not self.gates_open():
            # Startup into an already parked pipeline. The dispatcher must not claim anything,
            # and pausing it here rather than letting it claim and immediately release is what
            # stops a restart from churning the task table.
            await self.dispatcher.pause(self._park_reason())
        await self.progress.start()

        for index in range(self.worker_count):
            worker = Worker(
                f"worker-{index}",
                supervisor=self,
                dispatcher=self.dispatcher,
                queue=self.queue,
                bus=self.bus,
                services=self._services,
                to_thread=self._to_thread,
            )
            self._workers.append(worker)
            self._worker_tasks.append(
                asyncio.create_task(worker.run(), name=f"pipeline-{worker.name}")
            )

        if self._auto_reclaim:
            self._reclaim_task = asyncio.create_task(
                self._reclaim_loop(), name="pipeline-reclaim"
            )
        if self._governor is not None:
            self._mode_task = asyncio.create_task(self._mode_loop(), name="pipeline-mode")
        self._started = True
        log.info("pipeline supervisor started", extra={"worker_count": self.worker_count})

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for task in (self._reclaim_task, self._mode_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reclaim_task = None
        self._mode_task = None

        for task in self._worker_tasks:
            task.cancel()
        for task in self._worker_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._worker_tasks.clear()

        await self.dispatcher.stop()
        # Every lease this process still holds, including whatever a worker was cancelled on top
        # of. Doing it here rather than inside the cancelled worker is the only way to be sure the
        # statement actually runs.
        with contextlib.suppress(Exception):
            await self._to_thread(self.queue.release_all, self.owner)
        self._workers.clear()
        await self.progress.stop()
        log.info("pipeline supervisor stopped")

    # -- the job lifecycle --------------------------------------------------

    def notify(self, job_id: str | None = None) -> None:
        """A job was committed or unparked. Wake the dispatcher rather than wait out the poll."""
        if job_id:
            self.progress.mark_dirty(job_id)
        self.dispatcher.wake()

    async def on_task_settled(self, job_id: str) -> None:
        """Called by a worker after every terminal or parked outcome."""
        self.progress.mark_dirty(job_id)
        await self._settle_job(job_id)

    async def _settle_job(self, job_id: str) -> None:
        aggregate = await self._to_thread(self.queue.aggregate, job_id, sync_counters=False)
        if aggregate is None or not aggregate.is_finished:
            return
        if aggregate.status not in ("queued", "running", "paused"):
            return
        status = aggregate.terminal_status()
        await self._to_thread(self._finish_job, job_id, status)
        self.bus.publish(
            EVENT_JOB_FINISHED,
            {"job_id": job_id, "status": status, "reason": None},
        )
        await self.progress.flush(force=True)
        self.progress.forget(job_id)
        log.info("job finished", extra={"job_id": job_id, "job_status": status})

    def _finish_job(self, job_id: str, status: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE job SET status = :status, finished_at = :now,"
                    " block_reason = NULL WHERE job_id = :job_id"
                ),
                {"status": status, "now": iso_at(self._clock()), "job_id": job_id},
            )

    async def cancel_job(self, job_id: str) -> int:
        """Stop a job promptly. In flight tasks finish; everything pending is cancelled at once.

        The buffer is drained first. Rows sitting in it are `leased` but have not spent a request
        yet, and letting a cancel run them anyway would charge the user for up to sixteen requests
        they explicitly asked not to make. Draining returns them to `pending`, where the bulk update
        below catches them. Work belonging to other jobs goes back to pending too and is simply
        re-leased on the next refill.
        """
        await self._to_thread(self._request_cancel, job_id)
        was_paused = self.dispatcher.paused
        await self.dispatcher.pause(f"cancelling job {job_id}")
        try:
            cancelled = await self._to_thread(self.queue.cancel_pending, job_id)
        finally:
            if not was_paused:
                self.dispatcher.resume()
        self.dispatcher.wake()
        self.progress.mark_dirty(job_id)
        await self._settle_job(job_id)
        log.info("job cancelled", extra={"job_id": job_id, "cancelled_tasks": cancelled})
        return int(cancelled)

    def _request_cancel(self, job_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text("UPDATE job SET cancel_requested = 1 WHERE job_id = :job_id"),
                {"job_id": job_id},
            )

    # -- the auth gate ------------------------------------------------------

    async def on_auth_failure(
        self, generation: int, *, reason: str = "", fatal: bool = False
    ) -> bool:
        """React to one worker seeing an auth rejection. Returns True if this call did the parking.

        The gate is cleared unconditionally and first, before anything that can await, because the
        whole point is that no other worker spends a request after this one learned the token is
        dead. The generation guard inside the broker then decides which single caller does the
        expensive part: the mode change, the job parking and the banner.
        """
        self.auth_gate.clear()
        parked = True
        if self._token_broker is not None:
            parked = await self._token_broker.on_auth_error(generation, reason=reason)
        if not parked:
            # Another worker already parked this generation. Nothing more to do, which is exactly
            # the collapse: eight rejections, one login prompt.
            return False

        async with self._park_lock:
            await self.dispatcher.pause(reason or "the broker rejected the access token")
            blocked = await self._to_thread(self._park_jobs, "blocked_auth", reason)
        # Both classes park identically. `fatal` only changes what the banner says, because -15 and
        # an HTTP 401 mean the token was rejected outright rather than merely aged out, and there
        # is no refresh path that would have treated them differently anyway.
        token_state = "needs_reauth"
        self.bus.publish(
            EVENT_AUTH_REQUIRED,
            {
                "token_state": token_state,
                "reason": reason or "the broker rejected the access token",
                "parked_jobs": blocked["jobs"],
                "parked_tasks": blocked["tasks"],
                "fatal": bool(fatal),
            },
        )
        for job_id in blocked["job_ids"]:
            self.bus.publish(
                EVENT_JOB_BLOCKED,
                {"job_id": job_id, "status": "blocked_auth", "reason": reason},
            )
        self._publish_mode("paused_auth", reason)
        log.warning(
            "pipeline parked awaiting authentication",
            extra={"parked_jobs": blocked["jobs"], "parked_tasks": blocked["tasks"]},
        )
        return True

    async def on_login(self) -> None:
        """Resume at the exact task the pipeline stopped on.

        Called by the OAuth callback after `TokenBroker.store_login` has already bumped the
        generation and opened its gate. Nothing is re-planned and nothing is rewound: the parked
        tasks are still `pending` with their original `attempt`, so the first lease after this
        picks up precisely where the pool stopped.
        """
        self._sync_gates()
        if self._governor is not None:
            with contextlib.suppress(Exception):
                await self._governor.resume(by="login")
        self._sync_gates()
        resumed = await self._to_thread(self._unpark_jobs, "blocked_auth")
        self.dispatcher.resume()
        self.dispatcher.wake()
        self._publish_mode(self.governor_mode(), None)
        log.info("pipeline resumed after login", extra={"resumed_jobs": resumed})

    # -- the rate limit -----------------------------------------------------

    async def on_rate_limited(self, *, reason: str = "rate limit exceeded") -> None:
        """Stop the whole pipeline on strike one and wait for an explicit human resume.

        The governor has already recorded the strike by the time this runs, because `FyersClient`
        calls `note_rate_limited` on the way out. Automatic resume is deliberately absent: the
        account is blocked for the rest of the day on the fourth violation, and a crash loop that
        resumed itself would spend the remaining three in minutes.
        """
        self.run_gate.clear()
        async with self._park_lock:
            await self.dispatcher.pause(reason)
            blocked = await self._to_thread(self._park_jobs, "blocked_rate", reason)
        snapshot = None
        if self._governor is not None:
            # `FyersClient` already told the governor about the strike on the way out, so the mode
            # is normally paused_rate by now. Asserting it here as well covers a handler that
            # classified a rate limit without going through the client: without this the run gate
            # would be closed while the governor still said running, and the mode loop would find
            # nothing to resume from.
            with contextlib.suppress(Exception):
                await self._ensure_paused_rate(reason)
            with contextlib.suppress(Exception):
                snapshot = self._governor.snapshot()
        self.bus.publish(
            EVENT_RATE_LIMITED,
            {
                "strikes_used": getattr(snapshot, "minute_violations", None),
                "strikes_remaining": getattr(snapshot, "strikes_remaining", None),
                "blocked_until": getattr(snapshot, "blocked_until", None),
            },
        )
        for job_id in blocked["job_ids"]:
            self.bus.publish(
                EVENT_JOB_BLOCKED,
                {"job_id": job_id, "status": "blocked_rate", "reason": reason},
            )
        self._publish_mode("paused_rate", reason)

    # -- gates and modes ----------------------------------------------------

    def _sync_gates(self) -> None:
        """Point both gates at the truth their owners hold."""
        if self._token_broker is not None:
            try:
                usable = bool(self._token_broker.has_valid_token())
            except Exception:  # noqa: BLE001 - an unreadable broker is a closed gate
                usable = False
            if usable:
                self.auth_gate.set()
            else:
                self.auth_gate.clear()
        mode = self.governor_mode()
        if mode == _RUNNING_MODE:
            self.run_gate.set()
        else:
            self.run_gate.clear()

    def _park_reason(self) -> str:
        if not self.auth_gate.is_set():
            return "awaiting authentication"
        return f"pipeline mode {self.governor_mode()}"

    async def _ensure_paused_rate(self, reason: str) -> None:
        from expirymanager.brokers.fyers.throttle import GovernorMode

        if self.governor_mode() == _RUNNING_MODE:
            await self._governor.set_mode(GovernorMode.PAUSED_RATE, reason=reason)

    def _publish_mode(self, mode: str, reason: str | None) -> None:
        """Announce a mode and re-anchor the mode loop against the governor.

        `_last_mode` is set from the governor rather than from the mode just announced. Setting it
        to the announcement would make the loop's very next poll see a difference it did not cause
        and unpark everything, which is precisely the bug where a rate limit stop resumed itself
        one tick later.
        """
        self.bus.publish(EVENT_PIPELINE_MODE, {"mode": mode, "reason": reason})
        self._last_mode = self.governor_mode()

    def _park_jobs(self, status: str, reason: str) -> dict[str, Any]:
        """Move live jobs to a blocked status. Their tasks are untouched and stay pending."""
        with self._engine.begin() as connection:
            job_ids = [
                str(value)
                for value in connection.execute(
                    text(
                        "SELECT job_id FROM job WHERE status IN ('queued', 'running')"
                        " ORDER BY priority, created_at"
                    )
                ).scalars().all()
            ]
            if not job_ids:
                return {"jobs": 0, "tasks": 0, "job_ids": []}
            connection.execute(
                text(
                    "UPDATE job SET status = :status, block_reason = :reason"
                    " WHERE status IN ('queued', 'running')"
                ),
                {"status": status, "reason": reason or status},
            )
            # Scoped to the jobs this call parked, not to every job that happens to share the
            # status. A banner that counts a job somebody else parked an hour ago is a banner the
            # user cannot reconcile with what they were watching.
            statement = text(
                "SELECT count(*) FROM task WHERE state IN ('pending', 'leased')"
                " AND job_id IN :job_ids"
            ).bindparams(bindparam("job_ids", expanding=True, value=job_ids))
            tasks = int(connection.execute(statement, {"job_ids": job_ids}).scalar_one())
        return {"jobs": len(job_ids), "tasks": tasks, "job_ids": job_ids}

    def _unpark_jobs(self, status: str) -> int:
        with self._engine.begin() as connection:
            return connection.execute(
                text(
                    "UPDATE job SET status = 'queued', block_reason = NULL"
                    " WHERE status = :status AND cancel_requested = 0"
                ),
                {"status": status},
            ).rowcount

    # -- the loops ----------------------------------------------------------

    async def _reclaim_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reclaim_interval)
            try:
                reclaimed = await self._to_thread(self.queue.reclaim_expired_leases)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must outlive one bad statement
                log.exception("lease reclaim failed")
                continue
            if reclaimed:
                self.dispatcher.wake()

    async def _mode_loop(self) -> None:
        """Follow the governor's mode so a resume anywhere reaches the dispatcher.

        Polled rather than pushed because the governor exposes no callback, and a poll of an
        in-process attribute is cheaper than the indirection a callback registry would add to the
        one class in the system that must stay simple enough to reason about under a rate limit.
        """
        while True:
            await asyncio.sleep(self._mode_poll_interval)
            mode = self.governor_mode()
            if mode == self._last_mode:
                continue
            self._last_mode = mode
            if mode == _RUNNING_MODE:
                self.run_gate.set()
                if self.auth_gate.is_set():
                    await self._to_thread(self._unpark_jobs, "blocked_rate")
                    self.dispatcher.resume()
                    self.dispatcher.wake()
            else:
                self.run_gate.clear()
                await self.dispatcher.pause(f"pipeline mode {mode}")
                job_status = _MODE_TO_JOB_STATUS.get(mode)
                if job_status is not None:
                    await self._to_thread(self._park_jobs, job_status, f"pipeline mode {mode}")
            reason = getattr(self._governor, "reason", None)
            self.bus.publish(EVENT_PIPELINE_MODE, {"mode": mode, "reason": reason})


# ---------------------------------------------------------------------------
# Lifespan wiring
# ---------------------------------------------------------------------------


def build_supervisor(state: Any) -> PipelineSupervisor:
    """The factory the lifespan slot calls, with `AppState` in hand."""
    if state.engine is None:
        raise RuntimeError("the pipeline supervisor needs the sqlite engine")
    return PipelineSupervisor(
        engine=state.engine,
        settings=state.settings,
        governor=state.governor,
        token_broker=state.token_broker,
        services=state,
    )


def install() -> None:
    """Register the supervisor with the lifespan's named slot.

    Explicit rather than done at import, because registering as a side effect of importing this
    module would silently change what an application built by a test starts. `SLOT_JOB_RECOVERY` is
    deliberately left alone: W12's job service owns the rest of the recovery listed in PIPELINE.md
    section 6, and the reclaim and interrupted-job steps this item owns already run inside
    `PipelineSupervisor.start`, which the lifespan orders before that slot.
    """
    from expirymanager.lifespan import SLOT_PIPELINE_SUPERVISOR, register_component

    register_component(SLOT_PIPELINE_SUPERVISOR, build_supervisor)
