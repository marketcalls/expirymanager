"""Dispatcher, worker, supervisor, progress and event bus tests.

Everything runs against a real SQLite file with a fake clock and a fake handler. No HTTP, no
governor timing and no sleeping for a lease to expire: the properties under test are about which
durable row ends up in which state, and a test that had to wait 120 seconds to prove a reclaim
would be a test nobody runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from expirymanager.brokers.fyers.errors import Classification, RetryClass, classify
from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.pipeline import worker as worker_module
from expirymanager.pipeline.dispatcher import Dispatcher
from expirymanager.pipeline.events import (
    EVENT_AUTH_REQUIRED,
    EVENT_JOB_FINISHED,
    EVENT_JOB_PROGRESS,
    EVENT_JOB_STARTED,
    EVENT_NOTIFICATION,
    EVENT_TASK_COMPLETED,
    EventBus,
)
from expirymanager.pipeline.progress import ProgressAggregator
from expirymanager.pipeline.queue import LeaseQueue, TaskOutcome, iso_at
from expirymanager.pipeline.supervisor import PipelineSupervisor, reclaim_and_recover
from expirymanager.pipeline.worker import HandlerError, Worker

from tests.test_pipeline_queue import FakeClock, make_job, make_tasks, read_job, read_task


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


async def inline(fn, /, *args, **kwargs):
    """Run "blocking" work on the loop, so every test is deterministic.

    The production runner is `asyncio.to_thread`. Substituting an inline call removes the only
    source of nondeterminism in these tests without changing a single code path under test: the
    supervisor, dispatcher and workers only ever see an awaitable either way.
    """
    return fn(*args, **kwargs)


class FakeGovernor:
    """The mode surface the supervisor reads, and nothing else.

    The real governor is exercised by test_fyers_governor.py. What matters here is that the
    supervisor follows a mode it does not own.
    """

    def __init__(self, mode: str = "running") -> None:
        self.mode = mode
        self.reason: str | None = None
        self.resumed = 0

    async def resume(self, *, by: str | None = None) -> str:
        self.resumed += 1
        self.mode = "running"
        self.reason = None
        return self.mode

    def snapshot(self) -> Any:
        class Snapshot:
            minute_violations = 1
            strikes_remaining = 2
            blocked_until = None

        return Snapshot()

    async def pause_auth(self, *, reason: str = "") -> None:
        self.mode = "paused_auth"
        self.reason = reason

    async def set_mode(self, mode, *, reason: str | None = None) -> None:
        self.mode = str(mode)
        self.reason = reason


class FakeTokenBroker:
    """The generation guard, reproduced faithfully because the collapse depends on it."""

    def __init__(self, *, governor: FakeGovernor | None = None) -> None:
        self.auth_gate = asyncio.Event()
        self.auth_gate.set()
        self.generation = 1
        self._parked_generation: int | None = None
        self._governor = governor
        self.auth_error_calls = 0
        self.parked_count = 0

    def has_valid_token(self) -> bool:
        return self.auth_gate.is_set()

    async def on_auth_error(self, generation: int, *, reason: str = "") -> bool:
        self.auth_error_calls += 1
        if generation < self.generation:
            return False
        if self._parked_generation is not None and generation <= self._parked_generation:
            return False
        self._parked_generation = generation
        self.auth_gate.clear()
        self.parked_count += 1
        if self._governor is not None:
            await self._governor.pause_auth(reason=reason)
        return True

    def store_login(self) -> None:
        self.generation += 1
        self._parked_generation = None
        self.auth_gate.set()


@pytest.fixture
def engine(tmp_path: Path):
    eng = sqlite_module.create_engine(tmp_path / "data" / "config.sqlite3")
    migrate_module.migrate(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def queue(engine, clock) -> LeaseQueue:
    return LeaseQueue(engine, clock=clock, lease_seconds=120)


@pytest.fixture(autouse=True)
def clean_registry():
    worker_module.clear_registry()
    yield
    worker_module.clear_registry()


def build_supervisor(engine, queue, **kwargs) -> PipelineSupervisor:
    defaults: dict[str, Any] = {
        "engine": engine,
        "queue": queue,
        "bus": EventBus(),
        "worker_count": 2,
        "poll_interval": 0.01,
        "progress_interval": 0.01,
        "reclaim_interval": 0.05,
        "mode_poll_interval": 0.01,
        "to_thread": inline,
        "clock": queue.now,
        "recover_on_start": False,
        "auto_reclaim": False,
    }
    defaults.update(kwargs)
    return PipelineSupervisor(**defaults)


async def wait_for(predicate, *, timeout: float = 3.0, interval: float = 0.005) -> None:
    """Poll until a condition holds. Fails loudly rather than hanging the suite."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition did not hold within the timeout")


def frames(bus: EventBus, event: str) -> list[dict]:
    return [dict(frame.data) for frame in bus.history() if frame.event == event]


# ---------------------------------------------------------------------------
# The event bus
# ---------------------------------------------------------------------------


class TestEventBus:
    def test_ids_are_monotonic_and_start_at_one(self):
        bus = EventBus()
        assert bus.last_id == 0
        first = bus.publish(EVENT_JOB_STARTED, {"job_id": "a"})
        second = bus.publish(EVENT_JOB_FINISHED, {"job_id": "a"})
        assert (first.id, second.id) == (1, 2)
        assert bus.last_id == 2

    def test_an_unknown_event_name_is_refused(self):
        with pytest.raises(ValueError):
            EventBus().publish("nonsense", {})

    def test_the_ring_holds_only_the_last_capacity_frames(self):
        bus = EventBus(capacity=4)
        for index in range(10):
            bus.publish(EVENT_NOTIFICATION, {"n": index})
        history = bus.history()
        assert len(history) == 4
        assert [frame.data["n"] for frame in history] == [6, 7, 8, 9]

    def test_replay_returns_only_frames_newer_than_the_given_id(self):
        bus = EventBus(capacity=8)
        for index in range(5):
            bus.publish(EVENT_NOTIFICATION, {"n": index})
        assert [frame.data["n"] for frame in bus.replay(2)] == [2, 3, 4]
        assert bus.replay(None) == ()
        assert len(bus.replay(0)) == 5

    async def test_a_subscriber_receives_frames_in_order(self):
        bus = EventBus()
        with bus.stream() as subscription:
            bus.publish(EVENT_JOB_STARTED, {"job_id": "a"})
            bus.publish(EVENT_JOB_FINISHED, {"job_id": "a"})
            first = await subscription.get()
            second = await subscription.get()
        assert (first.event, second.event) == (EVENT_JOB_STARTED, EVENT_JOB_FINISHED)
        assert bus.subscriber_count == 0

    async def test_a_slow_subscriber_loses_its_oldest_frames_and_never_blocks_the_publisher(self):
        bus = EventBus()
        subscription = bus.subscribe()
        subscription.queue = asyncio.Queue(maxsize=2)
        for index in range(5):
            bus.publish(EVENT_NOTIFICATION, {"n": index})
        assert subscription.dropped == 3
        assert [(await subscription.get()).data["n"] for _ in range(2)] == [3, 4]



# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    async def test_the_prefetch_buffer_is_twice_the_worker_count_and_no_larger(
        self, engine, queue
    ):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 40)
        dispatcher = Dispatcher(
            queue, worker_count=4, owner="w", poll_interval=0.01, to_thread=inline
        )
        await dispatcher.start()
        try:
            await wait_for(lambda: dispatcher.buffered == 8)
            await asyncio.sleep(0.05)
            assert dispatcher.buffered == 8
            assert queue.counts_by_state("job-1")["leased"] == 8
        finally:
            await dispatcher.stop()

    async def test_stopping_returns_every_buffered_lease_to_pending(self, engine, queue):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 20)
        dispatcher = Dispatcher(
            queue, worker_count=2, owner="w", poll_interval=0.01, to_thread=inline
        )
        await dispatcher.start()
        await wait_for(lambda: dispatcher.buffered == 4)
        await dispatcher.stop()
        assert queue.counts_by_state("job-1") == {"pending": 20}

    async def test_pausing_drains_the_buffer_so_a_parked_pipeline_holds_no_leases(
        self, engine, queue
    ):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 20)
        dispatcher = Dispatcher(
            queue, worker_count=2, owner="w", poll_interval=0.01, to_thread=inline
        )
        await dispatcher.start()
        try:
            await wait_for(lambda: dispatcher.buffered == 4)
            await dispatcher.pause("parked")
            assert dispatcher.paused is True
            assert queue.counts_by_state("job-1") == {"pending": 20}
            await asyncio.sleep(0.05)
            assert queue.counts_by_state("job-1") == {"pending": 20}

            dispatcher.resume()
            await wait_for(lambda: dispatcher.buffered == 4)
        finally:
            await dispatcher.stop()

    async def test_a_promoted_job_publishes_job_started_once(self, engine, queue):
        bus = EventBus()
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 6)
        dispatcher = Dispatcher(
            queue, worker_count=2, owner="w", poll_interval=0.01, bus=bus, to_thread=inline
        )
        await dispatcher.start()
        try:
            await wait_for(lambda: dispatcher.buffered == 4)
            await asyncio.sleep(0.05)
        finally:
            await dispatcher.stop()
        assert frames(bus, EVENT_JOB_STARTED) == [{"job_id": "job-1", "status": "running"}]


# ---------------------------------------------------------------------------
# The worker and the retry policy
# ---------------------------------------------------------------------------


class TestWorkerRetryMapping:
    """The queue transition for each class must equal what errors.py declares."""

    @pytest.mark.parametrize(
        ("code", "http_status", "expected_state", "expected_attempt"),
        [
            (-50, None, "failed", 0),
            (-300, None, "failed", 0),
            (-352, None, "failed", 0),
            (None, 403, "failed", 0),
            (None, 500, "pending", 1),
            (-8, None, "pending", 0),
            (-16, None, "pending", 0),
            (-17, None, "pending", 0),
            (-15, None, "pending", 0),
            (None, 401, "pending", 0),
            (-429, None, "pending", 0),
            (None, 429, "pending", 0),
        ],
    )
    async def test_each_classification_produces_the_documented_transition(
        self, engine, queue, code, http_status, expected_state, expected_attempt
    ):
        classification = classify(status="error", code=code, http_status=http_status, message="x")

        async def failing(_ctx):
            raise HandlerError(classification, message="boom")

        worker_module.register_handler("candle_chunk", failing)
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        supervisor = build_supervisor(
            engine, queue, governor=FakeGovernor(), token_broker=FakeTokenBroker()
        )
        await supervisor.start()
        try:
            await wait_for(lambda: read_task(engine, task_id)["state"] != "leased")
            await asyncio.sleep(0.02)
        finally:
            await supervisor.stop()
        row = read_task(engine, task_id)
        assert row["state"] == expected_state
        assert row["attempt"] == expected_attempt

    async def test_a_no_data_answer_is_recorded_as_empty_and_is_not_a_failure(self, engine, queue):
        async def empty(_ctx):
            return TaskOutcome(state="empty", row_count=0, fyers_s="no_data")

        worker_module.register_handler("candle_chunk", empty)
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        supervisor = build_supervisor(engine, queue)
        await supervisor.start()
        try:
            await wait_for(lambda: read_task(engine, task_id)["state"] == "empty")
        finally:
            await supervisor.stop()
        assert read_job(engine, "job-1")["status"] == "completed"

    async def test_a_task_with_no_registered_handler_fails_rather_than_spinning(
        self, engine, queue
    ):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        supervisor = build_supervisor(engine, queue)
        await supervisor.start()
        try:
            await wait_for(lambda: read_task(engine, task_id)["state"] == "failed")
        finally:
            await supervisor.stop()
        assert "no handler" in read_task(engine, task_id)["last_error_text"]

    async def test_an_unclassified_exception_is_treated_as_fatal_not_retried_forever(
        self, engine, queue
    ):
        calls = []

        async def broken(ctx):
            calls.append(ctx.task.task_id)
            raise ValueError("a bug in the handler")

        worker_module.register_handler("candle_chunk", broken)
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        supervisor = build_supervisor(engine, queue)
        await supervisor.start()
        try:
            await wait_for(lambda: read_task(engine, task_id)["state"] == "failed")
            await asyncio.sleep(0.05)
        finally:
            await supervisor.stop()
        assert len(calls) == 1

    async def test_a_registered_kind_must_be_a_real_task_kind(self):
        with pytest.raises(ValueError):
            worker_module.register_handler("not_a_kind", lambda ctx: None)

    async def test_a_second_registration_needs_replace(self):
        worker_module.register_handler("candle_chunk", lambda ctx: None)
        with pytest.raises(ValueError):
            worker_module.register_handler("candle_chunk", lambda ctx: None)
        worker_module.register_handler("candle_chunk", lambda ctx: None, replace=True)
        assert worker_module.registered_kinds() == ("candle_chunk",)


# ---------------------------------------------------------------------------
# The happy path and job lifecycle
# ---------------------------------------------------------------------------


class TestJobLifecycle:
    async def test_a_job_runs_to_completion_and_publishes_the_discrete_frames(self, engine, queue):
        seen: list[int] = []

        async def ok(ctx):
            seen.append(ctx.task.task_id)
            return TaskOutcome(state="done", row_count=10, rows_written=10, bytes_downloaded=152)

        worker_module.register_handler("candle_chunk", ok)
        make_job(engine, "job-1")
        task_ids = make_tasks(engine, "job-1", 9)
        supervisor = build_supervisor(engine, queue)
        await supervisor.start()
        try:
            await wait_for(lambda: read_job(engine, "job-1")["status"] == "completed")
        finally:
            await supervisor.stop()

        assert sorted(seen) == sorted(task_ids)
        assert queue.counts_by_state("job-1") == {"done": 9}
        job = read_job(engine, "job-1")
        assert (job["requests_used"], job["rows_written"], job["done_tasks"]) == (9, 90, 9)
        assert job["finished_at"] is not None

        bus = supervisor.bus
        assert frames(bus, EVENT_JOB_STARTED) == [{"job_id": "job-1", "status": "running"}]
        finished = frames(bus, EVENT_JOB_FINISHED)
        assert finished == [{"job_id": "job-1", "status": "completed", "reason": None}]
        assert len(frames(bus, EVENT_TASK_COMPLETED)) == 9

    async def test_a_job_with_one_fatal_task_completes_with_errors(self, engine, queue):
        async def mixed(ctx):
            if ctx.task.seq == 1:
                raise HandlerError(
                    classify(status="error", code=-300, message="invalid symbol"),
                    message="invalid symbol",
                )
            return TaskOutcome(state="done", row_count=1)

        worker_module.register_handler("candle_chunk", mixed)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 3)
        supervisor = build_supervisor(engine, queue)
        await supervisor.start()
        try:
            await wait_for(
                lambda: read_job(engine, "job-1")["status"] == "completed_with_errors"
            )
        finally:
            await supervisor.stop()
        assert queue.counts_by_state("job-1") == {"done": 2, "failed": 1}

    async def test_cancellation_stops_promptly_and_leaves_an_accurate_ledger(self, engine, queue):
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[int] = []

        async def slow(ctx):
            calls.append(ctx.task.task_id)
            started.set()
            await release.wait()
            return TaskOutcome(state="done", row_count=1)

        worker_module.register_handler("candle_chunk", slow)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 20)
        supervisor = build_supervisor(engine, queue, worker_count=2)
        await supervisor.start()
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            in_flight = len(calls)
            cancelled = await supervisor.cancel_job("job-1")
            # Everything except the tasks already inside a handler, including whatever was merely
            # prefetched. A cancel must not spend a request the user asked not to make.
            assert cancelled == 20 - in_flight
            release.set()
            await wait_for(lambda: read_job(engine, "job-1")["status"] == "cancelled")
        finally:
            release.set()
            await supervisor.stop()

        counts = queue.counts_by_state("job-1")
        # In flight work finished and wrote its data; nothing else was ever requested.
        assert counts.get("done", 0) == in_flight
        assert counts.get("done", 0) + counts.get("cancelled", 0) == 20
        assert len(calls) == in_flight

    async def test_a_transient_failure_is_retried_after_its_backoff_and_then_succeeds(
        self, engine, queue, clock
    ):
        attempts: list[int] = []

        async def flaky(ctx):
            attempts.append(ctx.task.attempt)
            if ctx.task.attempt == 0:
                raise HandlerError(
                    Classification(retry_class=RetryClass.TRANSIENT, reason="http 500"),
                    message="http 500",
                )
            return TaskOutcome(state="done", row_count=1)

        worker_module.register_handler("candle_chunk", flaky)
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        supervisor = build_supervisor(engine, queue, worker_count=1)
        await supervisor.start()
        try:
            await wait_for(lambda: read_task(engine, task_id)["attempt"] == 1)
            # The retry waits in the table, not in the worker, so nothing runs until the clock
            # passes not_before. That is the property that stops a worker sleeping on a lease.
            await asyncio.sleep(0.05)
            assert read_task(engine, task_id)["state"] == "pending"
            assert attempts == [0]

            clock.advance(120)
            supervisor.notify("job-1")
            await wait_for(lambda: read_task(engine, task_id)["state"] == "done")
        finally:
            await supervisor.stop()
        assert attempts == [0, 1]


# ---------------------------------------------------------------------------
# Crash and resume
# ---------------------------------------------------------------------------


class TestCrashAndResume:
    async def test_a_crash_mid_task_replays_exactly_that_one_chunk(self, engine, queue):
        blocked = asyncio.Event()
        hold = asyncio.Event()
        calls: list[int] = []

        async def handler(ctx):
            calls.append(ctx.task.seq)
            if ctx.task.seq == 1 and not hold.is_set():
                blocked.set()
                await asyncio.sleep(30)
            return TaskOutcome(state="done", row_count=1)

        worker_module.register_handler("candle_chunk", handler)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 3)

        first = build_supervisor(engine, queue, worker_count=1)
        await first.start()
        await asyncio.wait_for(blocked.wait(), timeout=2)
        # A hard stop while seq 1 is in flight. Its lease is released rather than left to lapse,
        # so the next process picks it up immediately.
        await first.stop()

        assert calls == [0, 1]
        counts = queue.counts_by_state("job-1")
        assert counts == {"done": 1, "pending": 2}
        assert read_task(engine, [t for t in _task_ids(engine, "job-1")][1])["attempt"] == 0

        # PIPELINE.md section 6: recovery moves an interrupted running job to paused so the user
        # is asked before a multi day backfill restarts itself.
        recovery = reclaim_and_recover(engine, queue=queue)
        assert recovery["interrupted_jobs"] == 1
        assert read_job(engine, "job-1")["block_reason"] == "interrupted"

        with engine.begin() as connection:
            connection.execute(
                text("UPDATE job SET status = 'queued', block_reason = NULL")
            )
        hold.set()
        second = build_supervisor(engine, queue, worker_count=1)
        await second.start()
        try:
            await wait_for(lambda: read_job(engine, "job-1")["status"] == "completed")
        finally:
            await second.stop()

        # Exactly one chunk was replayed: seq 1, and nothing else.
        assert calls == [0, 1, 1, 2]
        assert queue.counts_by_state("job-1") == {"done": 3}

    async def test_startup_reclaims_a_lease_left_by_a_dead_process(self, engine, queue, clock):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        queue.lease(owner="a-process-that-died", limit=1)
        clock.advance(300)

        async def ok(_ctx):
            return TaskOutcome(state="done", row_count=1)

        worker_module.register_handler("candle_chunk", ok)
        with engine.begin() as connection:
            connection.execute(text("UPDATE job SET status = 'queued'"))
        supervisor = build_supervisor(engine, queue, recover_on_start=True)
        await supervisor.start()
        try:
            await wait_for(lambda: read_task(engine, task_id)["state"] == "done")
        finally:
            await supervisor.stop()
        # The reclaim did not charge the task for the process that died holding it.
        assert read_task(engine, task_id)["attempt"] == 0


def _task_ids(engine, job_id: str) -> list[int]:
    with engine.connect() as connection:
        return [
            int(value)
            for value in connection.execute(
                text("SELECT task_id FROM task WHERE job_id = :job_id ORDER BY seq"),
                {"job_id": job_id},
            ).scalars().all()
        ]


# ---------------------------------------------------------------------------
# The auth gate
# ---------------------------------------------------------------------------


class TestAuthGate:
    async def test_an_auth_error_parks_the_job_rather_than_failing_it(self, engine, queue):
        broker = FakeTokenBroker()
        governor = FakeGovernor()
        calls: list[int] = []

        async def rejecting(ctx):
            calls.append(ctx.task.task_id)
            raise HandlerError(
                classify(status="error", code=-8, message="token expired"),
                message="token expired",
            )

        worker_module.register_handler("candle_chunk", rejecting)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 12)
        supervisor = build_supervisor(
            engine, queue, worker_count=4, token_broker=broker, governor=governor
        )
        await supervisor.start()
        try:
            await wait_for(lambda: read_job(engine, "job-1")["status"] == "blocked_auth")
            await asyncio.sleep(0.05)

            assert supervisor.auth_gate.is_set() is False
            assert supervisor.dispatcher.paused is True
            assert supervisor.dispatcher.buffered == 0
            counts = queue.counts_by_state("job-1")
            # Nothing failed, nothing is still leased, and every task is back to pending.
            assert counts == {"pending": 12}
            assert all(read_task(engine, tid)["attempt"] == 0 for tid in _task_ids(engine, "job-1"))
            spent = len(calls)
            assert spent < 12

            auth_frames = frames(supervisor.bus, EVENT_AUTH_REQUIRED)
            assert len(auth_frames) == 1
            assert auth_frames[0]["parked_jobs"] == 1
            assert auth_frames[0]["parked_tasks"] == 12

            # No further request is spent while the gate is closed.
            await asyncio.sleep(0.1)
            assert len(calls) == spent
        finally:
            await supervisor.stop()

    async def test_starting_into_a_dead_token_claims_nothing_at_all(self, engine, queue):
        broker = FakeTokenBroker()
        broker.auth_gate.clear()
        calls: list[int] = []

        async def never(ctx):
            calls.append(ctx.task.task_id)
            return TaskOutcome(state="done")

        worker_module.register_handler("candle_chunk", never)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 5)
        supervisor = build_supervisor(engine, queue, token_broker=broker)
        await supervisor.start()
        try:
            await asyncio.sleep(0.1)
            assert calls == []
            assert supervisor.dispatcher.paused is True
            assert queue.counts_by_state("job-1") == {"pending": 5}
        finally:
            await supervisor.stop()

    async def test_a_gate_that_closes_after_the_claim_returns_the_lease_untouched(
        self, engine, queue
    ):
        broker = FakeTokenBroker()
        supervisor = build_supervisor(engine, queue, worker_count=1, token_broker=broker)
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)

        async def never_called(_ctx):
            raise AssertionError("the handler must not run behind a closed gate")

        worker_module.register_handler("candle_chunk", never_called)
        [task] = queue.lease(owner=supervisor.dispatcher.owner, limit=1)
        broker.auth_gate.clear()

        worker = Worker(
            "solo",
            supervisor=supervisor,
            dispatcher=supervisor.dispatcher,
            queue=queue,
            to_thread=inline,
        )
        await worker._run_one(task)
        row = read_task(engine, task_id)
        assert (row["state"], row["attempt"], row["lease_owner"]) == ("pending", 0, None)

    async def test_concurrent_auth_errors_collapse_into_one_park(self, engine, queue):
        broker = FakeTokenBroker()
        gate = asyncio.Event()

        async def rejecting(_ctx):
            # Every worker fails at the same instant, which is the case the generation guard
            # exists for.
            await gate.wait()
            raise HandlerError(
                classify(status="error", code=-16, message="unable to authenticate"),
                message="unable to authenticate",
            )

        worker_module.register_handler("candle_chunk", rejecting)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 8)
        supervisor = build_supervisor(engine, queue, worker_count=4, token_broker=broker)
        await supervisor.start()
        try:
            await wait_for(lambda: supervisor.dispatcher.buffered >= 1)
            await asyncio.sleep(0.05)
            gate.set()
            await wait_for(lambda: read_job(engine, "job-1")["status"] == "blocked_auth")
            await asyncio.sleep(0.05)
        finally:
            gate.set()
            await supervisor.stop()

        assert broker.auth_error_calls >= 2
        assert broker.parked_count == 1
        assert len(frames(supervisor.bus, EVENT_AUTH_REQUIRED)) == 1

    async def test_the_job_resumes_at_the_exact_task_after_the_next_login(self, engine, queue):
        broker = FakeTokenBroker()
        governor = FakeGovernor()
        reject_until_login = True
        seen: list[int] = []

        async def handler(ctx):
            seen.append(ctx.task.seq)
            if reject_until_login:
                raise HandlerError(
                    classify(status="error", code=-17, message="token invalid"),
                    message="token invalid",
                )
            return TaskOutcome(state="done", row_count=1)

        worker_module.register_handler("candle_chunk", handler)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 6)
        supervisor = build_supervisor(
            engine, queue, worker_count=2, token_broker=broker, governor=governor
        )
        await supervisor.start()
        try:
            await wait_for(lambda: read_job(engine, "job-1")["status"] == "blocked_auth")
            spent_before = len(seen)

            reject_until_login = False
            broker.store_login()
            await supervisor.on_login()

            await wait_for(lambda: read_job(engine, "job-1")["status"] == "completed")
        finally:
            await supervisor.stop()

        assert governor.resumed == 1
        assert queue.counts_by_state("job-1") == {"done": 6}
        # Nothing was rewound: every task that had already been attempted is retried and no task
        # ran more times than the park cost it.
        assert len(seen) == spent_before + 6
        assert all(read_task(engine, tid)["attempt"] == 0 for tid in _task_ids(engine, "job-1"))


# ---------------------------------------------------------------------------
# The rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    async def test_a_rate_limit_stops_the_whole_pipeline_and_waits_for_a_human(
        self, engine, queue
    ):
        governor = FakeGovernor()
        calls: list[int] = []

        async def limited(ctx):
            calls.append(ctx.task.task_id)
            raise HandlerError(
                classify(status="error", code=-429, message="rate limit"),
                message="rate limit",
            )

        worker_module.register_handler("candle_chunk", limited)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 10)
        supervisor = build_supervisor(engine, queue, worker_count=2, governor=governor)
        await supervisor.start()
        try:
            await wait_for(lambda: read_job(engine, "job-1")["status"] == "blocked_rate")
            spent = len(calls)
            await asyncio.sleep(0.1)
            assert len(calls) == spent
            assert supervisor.run_gate.is_set() is False
            assert queue.counts_by_state("job-1") == {"pending": 10}
            assert all(read_task(engine, tid)["attempt"] == 0 for tid in _task_ids(engine, "job-1"))
        finally:
            await supervisor.stop()

    async def test_the_supervisor_follows_a_governor_resume_without_being_told(
        self, engine, queue
    ):
        governor = FakeGovernor(mode="paused_rate")

        async def ok(_ctx):
            return TaskOutcome(state="done", row_count=1)

        worker_module.register_handler("candle_chunk", ok)
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 3)
        supervisor = build_supervisor(engine, queue, worker_count=2, governor=governor)
        await supervisor.start()
        try:
            await asyncio.sleep(0.05)
            assert supervisor.run_gate.is_set() is False
            assert queue.counts_by_state("job-1") == {"pending": 3}

            await governor.resume(by="user")
            await wait_for(lambda: read_job(engine, "job-1")["status"] == "completed")
        finally:
            await supervisor.stop()


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


class TestProgress:
    async def test_the_first_frame_is_whole_and_later_frames_are_diffs(self, engine, queue):
        bus = EventBus()
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 3)
        aggregator = ProgressAggregator(queue, bus, interval=0.0, to_thread=inline)

        leased = queue.lease(owner="w1", limit=3)
        queue.ack(leased[0], TaskOutcome(state="done", row_count=5))
        aggregator.mark_dirty("job-1")
        await aggregator.flush(force=True)

        queue.ack(leased[1], TaskOutcome(state="done", row_count=5))
        aggregator.mark_dirty("job-1")
        await aggregator.flush(force=True)

        published = frames(bus, EVENT_JOB_PROGRESS)
        assert set(published[0]) == {"job_id", *_progress_fields()}
        # Only what moved: the two counts, the request ledger and the estimate that depends on
        # them. The nine unchanged fields are absent, which is the whole point of a diff.
        assert set(published[1]) == {
            "job_id",
            "done",
            "leased",
            "requests_used",
            "eta_seconds",
        }
        assert published[1]["done"] == 2

    async def test_nothing_is_published_when_nothing_changed(self, engine, queue):
        bus = EventBus()
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 1)
        aggregator = ProgressAggregator(queue, bus, interval=0.0, to_thread=inline)
        aggregator.mark_dirty("job-1")
        assert await aggregator.flush(force=True) == 1
        aggregator.mark_dirty("job-1")
        assert await aggregator.flush(force=True) == 0

    async def test_the_interval_throttles_to_at_most_one_frame_a_second(self, engine, queue):
        bus = EventBus()
        ticks = [1000.0]
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 4)
        aggregator = ProgressAggregator(
            queue, bus, interval=1.0, clock=lambda: ticks[0], to_thread=inline
        )
        leased = queue.lease(owner="w1", limit=4)

        queue.ack(leased[0], TaskOutcome(state="done"))
        aggregator.mark_dirty("job-1")
        await aggregator.flush()

        ticks[0] += 0.4
        queue.ack(leased[1], TaskOutcome(state="done"))
        aggregator.mark_dirty("job-1")
        await aggregator.flush()
        assert len(frames(bus, EVENT_JOB_PROGRESS)) == 1

        ticks[0] += 0.7
        await aggregator.flush()
        assert len(frames(bus, EVENT_JOB_PROGRESS)) == 2

    async def test_the_aggregate_is_recomputed_rather_than_incremented(self, engine, queue):
        bus = EventBus()
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 4)
        aggregator = ProgressAggregator(queue, bus, interval=0.0, to_thread=inline)
        leased = queue.lease(owner="w1", limit=4)
        for task in leased:
            queue.ack(task, TaskOutcome(state="done"))
        # One flush, four completions. An in-memory counter would have to have seen each of them.
        aggregator.mark_dirty("job-1")
        await aggregator.flush(force=True)
        assert frames(bus, EVENT_JOB_PROGRESS)[0]["done"] == 4

    async def test_a_snapshot_matches_what_the_frame_carries(self, engine, queue):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 2)
        aggregator = ProgressAggregator(queue, None, interval=0.0, to_thread=inline)
        snapshot = await aggregator.snapshot("job-1")
        assert snapshot is not None
        assert snapshot["total"] == 2
        assert snapshot["pending"] == 2
        assert await aggregator.snapshot("missing") is None


def _progress_fields() -> tuple[str, ...]:
    from expirymanager.pipeline.progress import PROGRESS_FIELDS

    return PROGRESS_FIELDS


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestLifespanWiring:
    def test_install_registers_the_supervisor_slot_and_leaves_the_others_alone(self):
        from expirymanager import lifespan as lifespan_module
        from expirymanager.pipeline import supervisor as supervisor_module

        # Save and restore rather than clear on the way out. create_app installs all three slots
        # now, so an earlier test in the session may legitimately have left them registered, and
        # wiping them here would break whatever runs next. What this test is about is that
        # install() fills its own slot and touches no other.
        saved = dict(lifespan_module._component_factories)
        try:
            for slot in lifespan_module.COMPONENT_SLOTS:
                lifespan_module.unregister_component(slot)
            supervisor_module.install()
            assert lifespan_module.registered_components() == (
                lifespan_module.SLOT_PIPELINE_SUPERVISOR,
            )
        finally:
            lifespan_module._component_factories.clear()
            lifespan_module._component_factories.update(saved)

    def test_the_real_lifespan_starts_and_stops_the_supervisor(self, tmp_path, monkeypatch):
        """The registration point, exercised through a real application startup.

        Nothing is mocked out of the startup path here. This is the test that would catch a
        supervisor that only works against the fakes above, or one that leaks a task through
        shutdown, which a reasoned-about lifespan always eventually does.
        """
        from fastapi.testclient import TestClient

        from expirymanager import lifespan as lifespan_module
        from expirymanager.app import create_app
        from expirymanager.pipeline import supervisor as supervisor_module

        root = tmp_path / "expirymanager-home"
        monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
        try:
            supervisor_module.install()
            application = create_app(root=root, serve_static=False)
            with TestClient(application, base_url="https://127.0.0.1:8000") as client:
                assert client.get("/api/v1/bootstrap").status_code == 200
                supervisor = application.state.services.supervisor
                assert isinstance(supervisor, PipelineSupervisor)
                assert supervisor.started is True
                assert supervisor.worker_count == 8
                assert supervisor.dispatcher.capacity == 16
            # Shutdown ran, the pool is gone and nothing is left holding a lease.
            assert supervisor.started is False
            assert supervisor.workers == ()
            assert supervisor.dispatcher.running is False
        finally:
            for slot in lifespan_module.COMPONENT_SLOTS:
                lifespan_module.unregister_component(slot)
            sqlite_module.dispose_engine()

    def test_the_factory_refuses_to_build_without_an_engine(self):
        from expirymanager.pipeline import supervisor as supervisor_module

        class State:
            engine = None

        with pytest.raises(RuntimeError):
            supervisor_module.build_supervisor(State())


class TestSupervisorSnapshot:
    async def test_the_snapshot_reports_gates_and_counters_without_any_secret(self, engine, queue):
        supervisor = build_supervisor(
            engine, queue, token_broker=FakeTokenBroker(), governor=FakeGovernor()
        )
        await supervisor.start()
        try:
            snapshot = supervisor.snapshot()
        finally:
            await supervisor.stop()
        assert snapshot["auth_gate_open"] is True
        assert snapshot["run_gate_open"] is True
        assert snapshot["buffer_capacity"] == 4
        assert snapshot["mode"] == "running"
        assert "token" not in " ".join(snapshot.keys())


class TestRecovery:
    def test_recovery_is_idempotent(self, engine, queue):
        make_job(engine, "job-1", status="running")
        make_tasks(engine, "job-1", 2)
        queue.lease(owner="dead", limit=1)
        queue._clock.advance(300)
        first = reclaim_and_recover(engine, queue=queue)
        assert first == {"reclaimed": 1, "interrupted_jobs": 1}
        second = reclaim_and_recover(engine, queue=queue)
        assert second == {"reclaimed": 0, "interrupted_jobs": 0}
        assert read_job(engine, "job-1")["status"] == "paused"


def test_the_iso_helper_is_shared_so_every_writer_agrees_on_the_format():
    moment = datetime(2026, 9, 10, 4, 0, 0, tzinfo=UTC)
    assert iso_at(moment) == "2026-09-10T04:00:00.000000+00:00"
    assert iso_at(moment + timedelta(microseconds=1)) > iso_at(moment)
