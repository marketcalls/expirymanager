"""The worker coroutine and the task kind handler registry.

A worker is deliberately dull. It takes one claimed task, checks the gates, calls exactly one
handler, maps whatever comes back through `errors.classify` onto exactly one queue transition, and
goes back for the next one. Everything interesting is somewhere else on purpose:

- Rate limiting is the governor's, and the governor is reached through `FyersClient`, which holds
  one `governor.slot(endpoint)` for the duration of each outbound request. The worker does not take
  a second slot. Two acquisitions per request would double count the daily budget and halve the
  effective rate against a limit whose fourth violation costs the rest of the day. What the worker
  gates on instead is the pipeline being open for business at all, which is the auth gate and the
  run gate below.
- Retry policy is `errors.classify`'s, and the attempt arithmetic is the queue's.
- Idempotency is the DuckDB writer's, which is why a replayed chunk is harmless.

A worker never retries in place and never sleeps holding a lease. Both would pin a lease slot for a
duration nobody can bound, and a lease that outlives its expiry is a task two workers believe they
own. The retry is a row update with a future `not_before`; the wait happens in the table.

`register_handler` is the seam W13 fills, one handler per task kind.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from expirymanager.brokers.fyers.errors import (
    Classification,
    RetryClass,
    classify_exception,
)
from expirymanager.pipeline.events import EVENT_TASK_COMPLETED, EventBus
from expirymanager.pipeline.queue import (
    TASK_KINDS,
    LeasedTask,
    LeaseQueue,
    TaskOutcome,
)

__all__ = [
    "HandlerContext",
    "HandlerError",
    "TaskHandler",
    "register_handler",
    "unregister_handler",
    "get_handler",
    "registered_kinds",
    "handler_registry",
    "clear_registry",
    "handler",
    "Worker",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The handler seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HandlerContext:
    """Everything a handler is given. Nothing is reachable through a global.

    `generation` is the token generation that was current when the task was picked up. A handler
    that sees an auth rejection reports this value back, and the token broker's generation guard
    uses it to collapse eight simultaneous rejections into one reaction.
    """

    task: LeasedTask
    services: Any = None
    supervisor: Any = None
    generation: int = 0
    bus: EventBus | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class TaskHandler(Protocol):
    async def __call__(self, ctx: HandlerContext) -> TaskOutcome: ...


class HandlerError(Exception):
    """Raised by a handler that already knows how the failure should be classified.

    The alternative was for each handler to call the right `nack` itself, which would put the retry
    policy in seven places instead of one. A handler reports what happened; the worker decides what
    the queue does about it.
    """

    def __init__(
        self,
        classification: Classification,
        *,
        message: str = "",
        latency_ms: int | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message or classification.reason)
        self.classification = classification
        self.latency_ms = latency_ms
        self.http_status = http_status if http_status is not None else classification.http_status


_REGISTRY: dict[str, TaskHandler] = {}


def register_handler(kind: str, fn: TaskHandler, *, replace: bool = False) -> TaskHandler:
    """Bind one task kind to its handler. Refuses a typo rather than failing at the first lease."""
    if kind not in TASK_KINDS:
        raise ValueError(f"unknown task kind: {kind!r}")
    if kind in _REGISTRY and not replace:
        raise ValueError(f"a handler for {kind!r} is already registered")
    _REGISTRY[kind] = fn
    return fn


def unregister_handler(kind: str) -> None:
    _REGISTRY.pop(kind, None)


def get_handler(kind: str) -> TaskHandler | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def handler_registry() -> Mapping[str, TaskHandler]:
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Used by tests. Production registers once at import and never clears."""
    _REGISTRY.clear()


def handler(kind: str, *, replace: bool = False) -> Callable[[TaskHandler], TaskHandler]:
    """Decorator form, so a handler module registers itself next to its definition."""

    def decorate(fn: TaskHandler) -> TaskHandler:
        register_handler(kind, fn, replace=replace)
        return fn

    return decorate


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

ThreadRunner = Callable[..., Awaitable[Any]]


async def _default_thread_runner(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


class Worker:
    """One coroutine that drains claimed tasks through their handlers.

    There are more workers than the governor's in flight semaphore allows on purpose: the semaphore
    bounds concurrent sockets, the worker count bounds how many tasks are in flight through the
    whole pipeline including the DuckDB write, and having slightly more workers than sockets keeps
    the socket pool saturated while a write commits.
    """

    def __init__(
        self,
        name: str,
        *,
        supervisor: Any,
        dispatcher: Any,
        queue: LeaseQueue,
        bus: EventBus | None = None,
        services: Any = None,
        to_thread: ThreadRunner | None = None,
        registry: Callable[[str], TaskHandler | None] = get_handler,
    ) -> None:
        self.name = name
        self._supervisor = supervisor
        self._dispatcher = dispatcher
        self._queue = queue
        self._bus = bus
        self._services = services
        self._to_thread = to_thread or _default_thread_runner
        self._registry = registry
        self.completed = 0
        self.failed = 0
        self.parked = 0
        self.released = 0
        # Task ids this worker was holding when it was cancelled. Drained by the supervisor.
        self.abandoned: list[int] = []

    async def run(self) -> None:
        while True:
            task = await self._dispatcher.get()
            try:
                await self._run_one(task)
            except asyncio.CancelledError:
                # Shutdown mid task. Nothing is awaited here: this coroutine is already cancelled,
                # so a release statement issued now would very likely be cancelled too and the
                # lease would silently stay held. The id is recorded instead and the supervisor
                # releases it after the pool has stopped, which is the only point at which the
                # release is guaranteed to run.
                self.abandoned.append(task.task_id)
                raise
            except Exception:  # noqa: BLE001 - one bad task must not kill the pool
                log.exception(
                    "worker failed outside the handler",
                    extra={"task_id": task.task_id, "worker": self.name},
                )
                await self._release(task)
            finally:
                with contextlib.suppress(ValueError):
                    self._dispatcher.task_done()

    async def _run_one(self, task: LeasedTask) -> None:
        # The gate is checked after the claim as well as by the dispatcher, because the gate can
        # close between the two. Giving the lease straight back costs one statement and is what
        # guarantees no request is spent after the pipeline parks.
        if not self._supervisor.gates_open():
            await self._release(task)
            return

        fn = self._registry(task.kind)
        if fn is None:
            await self._to_thread(
                self._queue.nack_fatal,
                task,
                owner=self._dispatcher.owner,
                error_text=f"no handler is registered for task kind {task.kind}",
            )
            self.failed += 1
            log.error("no handler for task kind", extra={"task_kind": task.kind})
            return

        generation = self._supervisor.token_generation()
        context = HandlerContext(
            task=task,
            services=self._services,
            supervisor=self._supervisor,
            generation=generation,
            bus=self._bus,
        )
        started = time.perf_counter()
        try:
            outcome = await fn(context)
        except asyncio.CancelledError:
            raise
        except HandlerError as exc:
            latency = exc.latency_ms
            if latency is None:
                latency = int((time.perf_counter() - started) * 1000)
            await self._apply_failure(task, exc.classification, str(exc), latency, generation)
            return
        except Exception as exc:  # noqa: BLE001 - classified, not swallowed
            latency = int((time.perf_counter() - started) * 1000)
            classification = self._classify_unexpected(exc)
            await self._apply_failure(task, classification, repr(exc), latency, generation)
            return

        if not isinstance(outcome, TaskOutcome):
            raise TypeError(
                f"handler for {task.kind} returned {type(outcome).__name__}, not a TaskOutcome"
            )
        applied = await self._to_thread(
            self._queue.ack, task, outcome, owner=self._dispatcher.owner
        )
        if applied:
            self.completed += 1
            self._publish_completion(task, outcome)
        await self._supervisor.on_task_settled(task.job_id)

    def _classify_unexpected(self, exc: BaseException) -> Classification:
        """Map an exception a handler did not classify.

        `AuthTokenUnavailable` is the one that matters: it means the token broker had nothing to
        offer, which is an auth park and not a fatal task. Everything else goes through the
        transport classifier, which treats a timeout or a connection reset as transient and
        anything else as fatal.
        """
        from expirymanager.brokers.fyers.client import AuthTokenUnavailable

        if isinstance(exc, AuthTokenUnavailable):
            return Classification(
                retry_class=RetryClass.AUTH_FATAL,
                reason="no usable access token, log in again",
                message=type(exc).__name__,
            )
        return classify_exception(exc)

    async def _apply_failure(
        self,
        task: LeasedTask,
        classification: Classification,
        error_text: str,
        latency_ms: int,
        generation: int,
    ) -> None:
        owner = self._dispatcher.owner
        kwargs: dict[str, Any] = {
            "owner": owner,
            "error_text": error_text[:2000],
            "http_status": classification.http_status,
            "fyers_code": classification.code,
        }
        retry_class = classification.retry_class

        if retry_class is RetryClass.EMPTY:
            # A handler that raises EMPTY rather than returning it is still reporting a success.
            await self._to_thread(
                self._queue.ack,
                task,
                TaskOutcome(
                    state="empty",
                    row_count=0,
                    latency_ms=latency_ms,
                    fyers_s="no_data",
                    http_status=classification.http_status,
                ),
                owner=owner,
            )
            self.completed += 1
            await self._supervisor.on_task_settled(task.job_id)
            return

        if classification.is_auth_failure:
            # Park the row first, then tell the supervisor. In that order the task is already back
            # in `pending` by the time the supervisor moves the job to blocked_auth, so the parked
            # task count the banner shows can never be short by one.
            await self._to_thread(self._queue.nack_auth, task, fyers_s="error", **kwargs)
            self.parked += 1
            await self._supervisor.on_auth_failure(
                generation, reason=classification.reason, fatal=retry_class is RetryClass.AUTH_FATAL
            )
            return

        if retry_class is RetryClass.RATE_LIMITED:
            await self._to_thread(self._queue.nack_rate, task, fyers_s="error", **kwargs)
            self.parked += 1
            await self._supervisor.on_rate_limited(reason=classification.reason)
            return

        if retry_class is RetryClass.TRANSIENT:
            _applied, state, attempt = await self._to_thread(
                self._queue.nack_transient, task, latency_ms=latency_ms, **kwargs
            )
            if state == "failed":
                self.failed += 1
                log.warning(
                    "task failed after exhausting its attempts",
                    extra={"task_id": task.task_id, "attempt": attempt},
                )
            await self._supervisor.on_task_settled(task.job_id)
            return

        await self._to_thread(self._queue.nack_fatal, task, latency_ms=latency_ms, **kwargs)
        self.failed += 1
        await self._supervisor.on_task_settled(task.job_id)

    def _publish_completion(self, task: LeasedTask, outcome: TaskOutcome) -> None:
        if self._bus is None:
            return
        self._bus.publish(
            EVENT_TASK_COMPLETED,
            {
                "job_id": task.job_id,
                "task_id": task.task_id,
                "kind": task.kind,
                "state": outcome.state,
                "fyers_symbol": task.fyers_symbol,
                "row_count": outcome.row_count,
                "latency_ms": outcome.latency_ms,
            },
        )

    async def _release(self, task: LeasedTask) -> None:
        with contextlib.suppress(Exception):
            await self._to_thread(
                self._queue.release, [task.task_id], owner=self._dispatcher.owner
            )
        self.released += 1
