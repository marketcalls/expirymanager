"""Progress aggregation, at most once per second per job, published as diffs.

Two rules from PIPELINE.md section 8 shape everything here, and both are the opposite of what a
progress bar usually does.

**Counters are never incremented.** Every number is a fresh `GROUP BY state` over the durable task
rows. An in-memory tally would be wrong after a restart, wrong after a reclaimed lease, and wrong
after any missed frame, and each of those is invisible until a user reports that a finished job
still says 96 percent. Recomputing costs one indexed aggregate per active job per second, which is
nothing next to a request budget of 170 a minute.

**Frames carry diffs, not state.** The browser holds the job record and applies a patch, so sending
the whole record every second would push twelve unchanged fields through the stream for every one
that moved. The diff is computed against the last frame this process published for that job, and
`job_id` is always included so a patch is addressable on its own.

The aggregate is also written back onto the job's counter columns in the same transaction, which is
what keeps `GET /api/v1/jobs/{id}` and the SSE frame identical without either side maintaining its
own tally.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from expirymanager.pipeline.events import EVENT_JOB_PROGRESS, EventBus
from expirymanager.pipeline.queue import JobAggregate, LeaseQueue

__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "PROGRESS_FIELDS",
    "snapshot_of",
    "ProgressAggregator",
]

log = logging.getLogger(__name__)

# API.md section 11 says at most once a second, and the UI is unreadable faster than that anyway.
DEFAULT_INTERVAL_SECONDS = 1.0

# The exact field set of a `job_progress` frame. Listed rather than derived so a rename here has to
# be a deliberate change to a contract the frontend already reads.
PROGRESS_FIELDS: tuple[str, ...] = (
    "status",
    "total",
    "done",
    "empty",
    "failed",
    "skipped",
    "cancelled",
    "pending",
    "leased",
    "requests_used",
    "rows_written",
    "eta_seconds",
)

ThreadRunner = Callable[..., Awaitable[Any]]


async def _default_thread_runner(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


def snapshot_of(aggregate: JobAggregate, *, effective_rpm: float) -> dict[str, Any]:
    """Flatten one aggregate into the frame field set.

    `eta_seconds` uses the governor's effective rate rather than the published one, for the same
    reason the planner does: an estimate built on 200 a minute is an estimate the pipeline will
    never meet, and a progress bar that always runs late reads as a broken progress bar.
    """
    remaining = aggregate.open
    eta = None
    if remaining and effective_rpm > 0:
        eta = int(round(remaining / (effective_rpm / 60.0)))
    return {
        "status": aggregate.status,
        "total": aggregate.total,
        "done": aggregate.done,
        "empty": aggregate.empty,
        "failed": aggregate.failed,
        "skipped": aggregate.skipped,
        "cancelled": aggregate.cancelled,
        "pending": aggregate.pending,
        "leased": aggregate.leased,
        "requests_used": aggregate.requests_used,
        "rows_written": aggregate.rows_written,
        "eta_seconds": eta,
    }


class ProgressAggregator:
    """Tracks which jobs moved, and publishes their diffs no faster than the interval."""

    def __init__(
        self,
        queue: LeaseQueue,
        bus: EventBus | None = None,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        effective_rpm: float = 170.0,
        clock: Callable[[], float] = time.monotonic,
        to_thread: ThreadRunner | None = None,
    ) -> None:
        self._queue = queue
        self._bus = bus
        self._interval = interval
        self._effective_rpm = effective_rpm
        self._clock = clock
        self._to_thread = to_thread or _default_thread_runner
        self._dirty: set[str] = set()
        self._last: dict[str, dict[str, Any]] = {}
        self._published_at: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._wakeup = asyncio.Event()

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def dirty_jobs(self) -> tuple[str, ...]:
        return tuple(sorted(self._dirty))

    def mark_dirty(self, job_id: str) -> None:
        """Called by a worker when a task settled. Cheap and synchronous by design."""
        if not job_id:
            return
        self._dirty.add(job_id)
        self._wakeup.set()

    def forget(self, job_id: str) -> None:
        """Drop the remembered diff base, so the next frame for this job is complete again."""
        self._dirty.discard(job_id)
        self._last.pop(job_id, None)
        self._published_at.pop(job_id, None)

    async def snapshot(self, job_id: str) -> dict[str, Any] | None:
        """The same numbers the frame carries, for a REST caller. Always fresh, never throttled."""
        aggregate = await self._to_thread(self._queue.aggregate, job_id, sync_counters=False)
        if aggregate is None:
            return None
        return snapshot_of(aggregate, effective_rpm=self._effective_rpm)

    async def flush(self, *, force: bool = False) -> int:
        """Publish a diff for every dirty job whose interval has elapsed.

        `force` ignores the interval, which is what a job finishing needs: the last frame of a job
        must not be swallowed because it landed inside the same second as the one before it.
        """
        if not self._dirty:
            return 0
        now = self._clock()
        published = 0
        for job_id in sorted(self._dirty):
            last_at = self._published_at.get(job_id)
            if not force and last_at is not None and (now - last_at) < self._interval:
                continue
            self._dirty.discard(job_id)
            aggregate = await self._to_thread(
                self._queue.aggregate, job_id, sync_counters=True
            )
            if aggregate is None:
                self.forget(job_id)
                continue
            current = snapshot_of(aggregate, effective_rpm=self._effective_rpm)
            diff = _diff(self._last.get(job_id), current)
            self._last[job_id] = current
            self._published_at[job_id] = now
            if not diff:
                continue
            if self._bus is not None:
                self._bus.publish(EVENT_JOB_PROGRESS, {"job_id": job_id, **diff})
            published += 1
        return published

    # -- the loop -----------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="pipeline-progress")

    async def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # One last pass so the frame that says a job finished is never the one that is lost.
        with contextlib.suppress(Exception):
            await self.flush(force=True)

    async def _loop(self) -> None:
        while self._running:
            self._wakeup.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._interval)
            if not self._running:
                return
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad aggregate must not stop progress forever
                log.exception("progress aggregation failed")
            # No extra sleep. The once-per-second rule is enforced per job inside `flush` against
            # `_published_at`, which is the right place for it: a burst of completions across eight
            # jobs must not make each of them wait for the others.


def _diff(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> dict[str, Any]:
    """Only the fields that changed. A first frame for a job is therefore the whole record."""
    if previous is None:
        return dict(current)
    return {key: value for key, value in current.items() if previous.get(key) != value}
