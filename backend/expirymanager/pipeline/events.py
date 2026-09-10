"""The in-process event bus the SSE endpoint streams.

One bus per process, constructed by the supervisor and injected. It is deliberately the smallest
thing that satisfies the contract in API.md section 11: a monotonic id per frame, a 512 frame ring
buffer so a browser that reconnects with `Last-Event-ID` gets the frames it missed, and per
subscriber queues so one slow reader cannot stall the publisher.

Two properties are worth stating because getting either wrong is invisible until production:

- `publish` never blocks and never awaits. It is called from a worker that is holding a lease and
  from the progress loop, and a bus that could block there would couple stream backpressure to
  download throughput. A subscriber that cannot keep up loses its oldest undelivered frame and
  carries a `dropped` counter instead.
- Losing a frame is a refresh rate problem, not a correctness problem. Every frame type has a REST
  equivalent and the SPA keeps a slow background refetch, which is what makes the drop policy
  above acceptable rather than lossy.

The bus knows nothing about SSE. W20 owns the wire format, the keepalive comment and the headers.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "RING_CAPACITY",
    "SUBSCRIBER_QUEUE_SIZE",
    "EVENT_JOB_PROGRESS",
    "EVENT_JOB_STARTED",
    "EVENT_JOB_FINISHED",
    "EVENT_JOB_BLOCKED",
    "EVENT_TASK_COMPLETED",
    "EVENT_BUDGET",
    "EVENT_AUTH_REQUIRED",
    "EVENT_RATE_LIMITED",
    "EVENT_PIPELINE_MODE",
    "EVENT_SCHEDULE_FIRED",
    "EVENT_EXPORT_READY",
    "EVENT_NOTIFICATION",
    "EVENT_TYPES",
    "DISCRETE_EVENTS",
    "Frame",
    "Subscription",
    "EventBus",
]

log = logging.getLogger(__name__)

# API.md section 11 names both numbers. The ring is small on purpose: it exists to cover a
# reconnect, not to be a message log, and a browser that was away long enough to fall out of 512
# frames refetches over REST anyway.
RING_CAPACITY = 512

# Per subscriber. Larger than the ring would be pointless and smaller would drop frames during an
# ordinary event burst such as eight workers finishing at once.
SUBSCRIBER_QUEUE_SIZE = 256

EVENT_JOB_PROGRESS = "job_progress"
EVENT_JOB_STARTED = "job_started"
EVENT_JOB_FINISHED = "job_finished"
EVENT_JOB_BLOCKED = "job_blocked"
EVENT_TASK_COMPLETED = "task_completed"
EVENT_BUDGET = "budget"
EVENT_AUTH_REQUIRED = "auth_required"
EVENT_RATE_LIMITED = "rate_limited"
EVENT_PIPELINE_MODE = "pipeline_mode"
EVENT_SCHEDULE_FIRED = "schedule_fired"
EVENT_EXPORT_READY = "export_ready"
EVENT_NOTIFICATION = "notification"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_JOB_PROGRESS,
        EVENT_JOB_STARTED,
        EVENT_JOB_FINISHED,
        EVENT_JOB_BLOCKED,
        EVENT_TASK_COMPLETED,
        EVENT_BUDGET,
        EVENT_AUTH_REQUIRED,
        EVENT_RATE_LIMITED,
        EVENT_PIPELINE_MODE,
        EVENT_SCHEDULE_FIRED,
        EVENT_EXPORT_READY,
        EVENT_NOTIFICATION,
    }
)

# Published the moment they happen rather than on the one second progress tick, because each one
# changes what the user is allowed to do next.
DISCRETE_EVENTS: frozenset[str] = frozenset(
    {
        EVENT_JOB_STARTED,
        EVENT_JOB_FINISHED,
        EVENT_JOB_BLOCKED,
        EVENT_AUTH_REQUIRED,
        EVENT_RATE_LIMITED,
        EVENT_BUDGET,
        EVENT_SCHEDULE_FIRED,
        EVENT_EXPORT_READY,
        EVENT_NOTIFICATION,
    }
)


@dataclass(frozen=True, slots=True)
class Frame:
    """One published event.

    `id` is monotonic across the life of the process and is what `Last-Event-ID` replays against.
    It restarts at 1 on a restart, which is correct: the ring buffer is gone too, so a client
    holding a pre-restart id gets the whole ring and then refetches over REST.
    """

    id: int
    event: str
    data: Mapping[str, Any]
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "event": self.event, "data": dict(self.data), "at": self.at}


@dataclass(eq=False)
class Subscription:
    """One reader's view of the bus.

    Held by the SSE endpoint for the life of one stream. Iterating it yields frames in publication
    order; `dropped` is non zero only when this reader fell behind by more than its queue.
    """

    bus: "EventBus"
    queue: asyncio.Queue[Frame] = field(
        default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
    )
    dropped: int = 0
    closed: bool = False

    def offer(self, frame: Frame) -> None:
        """Deliver one frame, dropping the oldest undelivered frame when the queue is full."""
        if self.closed:
            return
        try:
            self.queue.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.queue.get_nowait()
            self.queue.task_done()
        except asyncio.QueueEmpty:  # pragma: no cover - only if a reader raced us
            pass
        self.dropped += 1
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:  # pragma: no cover - the get above made room
            self.dropped += 1

    async def get(self) -> Frame:
        return await self.queue.get()

    def get_nowait(self) -> Frame:
        return self.queue.get_nowait()

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Frame:
        if self.closed and self.queue.empty():
            raise StopAsyncIteration
        return await self.queue.get()

    def close(self) -> None:
        self.closed = True
        self.bus.unsubscribe(self)


class EventBus:
    """Fan out frames to every live subscriber and keep the last `capacity` for replay."""

    def __init__(self, *, capacity: int = RING_CAPACITY, clock: Any = None) -> None:
        self._ring: deque[Frame] = deque(maxlen=capacity)
        self._subscribers: list[Subscription] = []
        self._next_id = 1
        self._clock = clock or (lambda: datetime.now(UTC))
        self._closed = False

    @property
    def capacity(self) -> int:
        return self._ring.maxlen or 0

    @property
    def last_id(self) -> int:
        """The id of the most recent frame, or 0 before anything was published."""
        return self._next_id - 1

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def history(self) -> tuple[Frame, ...]:
        """Everything still in the ring, oldest first."""
        return tuple(self._ring)

    def publish(self, event: str, data: Mapping[str, Any] | None = None) -> Frame:
        """Append one frame and hand it to every subscriber. Never blocks, never awaits."""
        if event not in EVENT_TYPES:
            # A typo in an event name would otherwise be a frame the browser silently ignores.
            raise ValueError(f"unknown event type: {event!r}")
        frame = Frame(
            id=self._next_id,
            event=event,
            data=dict(data or {}),
            at=self._clock().isoformat(),
        )
        self._next_id += 1
        self._ring.append(frame)
        if self._closed:
            return frame
        for subscriber in tuple(self._subscribers):
            subscriber.offer(frame)
        return frame

    def replay(self, after_id: int | None) -> tuple[Frame, ...]:
        """Frames newer than `after_id`, oldest first.

        A None or unparseable id means the client has no position, so nothing is replayed and it
        starts from now. An id older than the whole ring returns the whole ring, which is the
        honest answer: this is everything still known.
        """
        if after_id is None:
            return ()
        return tuple(frame for frame in self._ring if frame.id > after_id)

    def subscribe(self) -> Subscription:
        subscription = Subscription(bus=self)
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        try:
            self._subscribers.remove(subscription)
        except ValueError:
            pass

    @contextmanager
    def stream(self) -> Iterator[Subscription]:
        """Subscribe for the duration of a block, and always unsubscribe."""
        subscription = self.subscribe()
        try:
            yield subscription
        finally:
            subscription.close()

    def close(self) -> None:
        """Detach every subscriber. The ring is kept so a late reader still sees history."""
        self._closed = True
        for subscriber in tuple(self._subscribers):
            subscriber.closed = True
        self._subscribers.clear()
