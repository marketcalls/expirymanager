"""Refills a small in-memory prefetch buffer from the durable lease queue.

The buffer exists for one reason: leasing one row per worker per task would make one SQLite write
transaction the gate on every single request, and eight workers contending on that write is a
throughput cliff that has nothing to do with the Fyers rate limit. Claiming a batch amortises it.

The buffer is a prefetch buffer and nothing more. SQLite is the durable truth; anything sitting in
the buffer is already `leased` in the table, so a hard kill costs at most `2 * worker_count` leases
and the reclaim loop returns them without burning an attempt. That bound is why the capacity is
fixed at twice the worker count rather than being tuned: doubling it doubles what a kill loses and
buys nothing, because the workers are rate limited, not queue limited.

Two behaviours matter more than the refill itself:

- The dispatcher, not the worker, is what stops when the pipeline parks. Pausing here drains the
  buffer and releases those leases back to `pending`, so a parked pipeline holds zero leases and
  the reclaim loop has nothing to do. Stopping at the worker instead would leave up to sixteen rows
  `leased` with nobody running them, and every one of those would sit until its 120 second lease
  lapsed before the next login could pick it up.
- Leasing is filtered in SQL by job status and `cancel_requested`, so a cancel is honoured on the
  very next refill without the dispatcher keeping a set of cancelled job ids in memory that a
  restart would forget.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from expirymanager.pipeline.events import EVENT_JOB_STARTED, EventBus
from expirymanager.pipeline.queue import LeasedTask, LeaseQueue

__all__ = [
    "DEFAULT_POLL_INTERVAL",
    "Dispatcher",
]

log = logging.getLogger(__name__)

# How long the refill loop sleeps when the queue had nothing for it. Short enough that a schedule
# fire feels immediate even if nobody remembers to call `wake`, long enough that an idle process
# is not running two SQLite statements a second forever.
DEFAULT_POLL_INTERVAL = 0.5

ThreadRunner = Callable[..., Awaitable[Any]]


async def _default_thread_runner(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


class Dispatcher:
    """Owns the prefetch buffer and the single coroutine that fills it."""

    def __init__(
        self,
        queue: LeaseQueue,
        *,
        worker_count: int = 8,
        owner: str | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        bus: EventBus | None = None,
        to_thread: ThreadRunner | None = None,
    ) -> None:
        self._queue = queue
        self._worker_count = max(1, worker_count)
        # One owner string per process run. It is the lease owner written onto every row this
        # dispatcher claims, which is what makes an ack from a previous process a no-op.
        self._owner = owner or f"dispatcher-{uuid.uuid4().hex[:12]}"
        self._poll_interval = poll_interval
        self._bus = bus
        self._to_thread = to_thread or _default_thread_runner

        self._buffer: asyncio.Queue[LeasedTask] = asyncio.Queue(maxsize=2 * self._worker_count)
        self._wakeup = asyncio.Event()
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._paused_reason: str | None = None
        self._in_flight_batch: list[LeasedTask] = []
        self._leased_total = 0

    # -- identity and inspection -------------------------------------------

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def capacity(self) -> int:
        return self._buffer.maxsize

    @property
    def buffered(self) -> int:
        return self._buffer.qsize()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return not self._resumed.is_set()

    @property
    def paused_reason(self) -> str | None:
        return self._paused_reason

    @property
    def leased_total(self) -> int:
        """Lifetime count of claimed tasks. Used by tests and by the diagnostics endpoint."""
        return self._leased_total

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._resumed.set()
        self._paused_reason = None
        self._spawn()

    async def stop(self) -> None:
        """Stop refilling and hand every unstarted lease back."""
        self._running = False
        self._resumed.set()
        await self._halt()
        await self.drain()

    def wake(self) -> None:
        """Ask for an immediate refill. Called when a job is committed or unparked."""
        self._wakeup.set()

    async def pause(self, reason: str) -> None:
        """Stop leasing and give back everything buffered.

        Called before any other reaction to an auth failure, a rate limit or a user pause, because
        the buffered rows are the requests that would otherwise be spent next.

        The refill coroutine is cancelled rather than signalled. A signal would leave it parked
        inside `Queue.put` on a full buffer holding a claimed row, and the drain below would race
        with that put: the released set and the buffer could each end up holding a lease the other
        thought it had. Cancelling first makes the drain the only thing touching the buffer.
        """
        if self.paused:
            self._paused_reason = reason or self._paused_reason
            return
        self._paused_reason = reason
        self._resumed.clear()
        await self._halt()
        await self.drain()
        log.info("dispatcher paused", extra={"reason": reason})

    def resume(self) -> None:
        if not self.paused:
            return
        self._paused_reason = None
        self._resumed.set()
        self._wakeup.set()
        if self._running:
            self._spawn()
        log.info("dispatcher resumed")

    def _spawn(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="pipeline-dispatcher")

    async def _halt(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def drain(self) -> int:
        """Empty the buffer and release those leases back to pending, attempt untouched."""
        released: list[int] = [task.task_id for task in self._in_flight_batch]
        self._in_flight_batch = []
        while True:
            try:
                task = self._buffer.get_nowait()
            except asyncio.QueueEmpty:
                break
            released.append(task.task_id)
        if not released:
            return 0
        count = await self._to_thread(self._queue.release, released, owner=self._owner)
        log.info("dispatcher released buffered leases", extra={"released": count})
        return int(count)

    # -- the buffer ---------------------------------------------------------

    async def get(self) -> LeasedTask:
        """Take the next claimed task. Workers block here, which is the whole idle mechanism."""
        return await self._buffer.get()

    def task_done(self) -> None:
        self._buffer.task_done()

    # -- the refill loop ----------------------------------------------------

    async def _loop(self) -> None:
        while self._running and self._resumed.is_set():
            try:
                await self._refill_once()
            except asyncio.CancelledError:
                # A cancel between the claim and the buffer put would otherwise strand those
                # leases for a full lease period. Give them back before unwinding.
                await self._release_in_flight()
                raise
            except Exception:  # noqa: BLE001 - the loop must outlive one bad statement
                log.exception("dispatcher refill failed")
                await self._release_in_flight()
                await self._idle()

    async def _refill_once(self) -> None:
        room = self._buffer.maxsize - self._buffer.qsize()
        if room <= 0:
            # The workers are saturated. Waiting on the wakeup event rather than spinning is what
            # keeps an idle-but-full dispatcher off the CPU.
            await self._idle()
            return
        # A cancel that lands while this await is in a worker thread loses the result of a lease
        # statement that already committed, so up to `room` rows stay `leased` with nobody holding
        # them. That is exactly what the reclaim loop is for, and it costs one reclaim interval
        # rather than any correctness: the rows come back to `pending` with `attempt` untouched.
        # The alternative, holding the pause until the thread returns, would make a pause take as
        # long as a contended SQLite write, which is the one thing a rate limit stop cannot afford.
        leased = await self._to_thread(self._queue.lease, owner=self._owner, limit=room)
        promoted = self._queue.last_promoted
        if not leased:
            await self._idle()
            return
        self._leased_total += len(leased)
        self._in_flight_batch = list(leased)
        if self._bus is not None:
            for job_id in promoted:
                self._bus.publish(EVENT_JOB_STARTED, {"job_id": job_id, "status": "running"})
        while self._in_flight_batch:
            task = self._in_flight_batch[0]
            await self._buffer.put(task)
            self._in_flight_batch.pop(0)

    async def _release_in_flight(self) -> None:
        if not self._in_flight_batch:
            return
        ids = [task.task_id for task in self._in_flight_batch]
        self._in_flight_batch = []
        with contextlib.suppress(Exception):
            await self._to_thread(self._queue.release, ids, owner=self._owner)

    async def _idle(self) -> None:
        self._wakeup.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wakeup.wait(), timeout=self._poll_interval)
