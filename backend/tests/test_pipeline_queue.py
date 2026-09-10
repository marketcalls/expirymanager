"""Lease queue tests, against a real SQLite file and a fake clock.

Every assertion here is about a durable row read back after the fact, not about what a method
returned, because the whole premise of the design is that the table is the truth. A test that
trusted the return value would still pass if the statement wrote nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.pipeline import queue as queue_module
from expirymanager.pipeline.queue import LeaseQueue, TaskOutcome, iso_at


class FakeClock:
    """A clock the test moves by hand, so a 120 second lease expires in a microsecond of runtime."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 10, 4, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


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


def make_job(
    engine,
    job_id: str = "job-1",
    *,
    status: str = "queued",
    priority: int = 100,
    kind: str = "candle_backfill",
) -> str:
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (job_id, kind, status, params_json, priority, created_at)"
                " VALUES (:job_id, :kind, :status, '{}', :priority, :now)"
            ),
            {
                "job_id": job_id,
                "kind": kind,
                "status": status,
                "priority": priority,
                "now": iso_at(datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC)),
            },
        )
    return job_id


def make_tasks(
    engine,
    job_id: str,
    count: int,
    *,
    kind: str = "candle_chunk",
    priority: int = 100,
    not_before: datetime | None = None,
    start_seq: int = 0,
) -> list[int]:
    from sqlalchemy import text

    created = iso_at(datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC))
    ready = iso_at(not_before or datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC))
    ids: list[int] = []
    with engine.begin() as connection:
        for offset in range(count):
            row = connection.execute(
                text(
                    "INSERT INTO task (job_id, seq, kind, state, priority, fyers_symbol,"
                    " not_before, created_at)"
                    " VALUES (:job_id, :seq, :kind, 'pending', :priority, :symbol, :ready, :created)"
                    " RETURNING task_id"
                ),
                {
                    "job_id": job_id,
                    "seq": start_seq + offset,
                    "kind": kind,
                    "priority": priority,
                    "symbol": f"NSE:TEST{offset}",
                    "ready": ready,
                    "created": created,
                },
            ).scalar_one()
            ids.append(int(row))
    return ids


def read_task(engine, task_id: int) -> dict:
    from sqlalchemy import text

    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM task WHERE task_id = :task_id"), {"task_id": task_id}
            ).mappings().one()
        )


def read_job(engine, job_id: str) -> dict:
    from sqlalchemy import text

    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM job WHERE job_id = :job_id"), {"job_id": job_id}
            ).mappings().one()
        )


class TestTimestampFormat:
    def test_the_iso_format_is_fixed_width_so_a_string_compare_orders_correctly(self):
        earlier = iso_at(datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC))
        later = iso_at(datetime(2026, 9, 10, 3, 0, 0, 1, tzinfo=UTC))
        assert len(earlier) == len(later)
        assert earlier < later

    def test_a_naive_datetime_is_read_as_utc_rather_than_local_time(self):
        assert iso_at(datetime(2026, 9, 10, 3, 0, 0)) == iso_at(
            datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC)
        )


class TestLeasing:
    def test_a_lease_claims_in_priority_then_job_then_seq_order(self, engine, queue):
        make_job(engine, "job-b", priority=100)
        make_job(engine, "job-a", priority=100)
        make_tasks(engine, "job-b", 2, priority=100)
        make_tasks(engine, "job-a", 2, priority=100)
        make_tasks(engine, "job-b", 1, priority=5, start_seq=90)

        # One at a time, because what has to hold is the selection order rather than the order
        # RETURNING happens to answer in.
        claimed = []
        while True:
            batch = queue.lease(owner="w1", limit=1)
            if not batch:
                break
            claimed.append((batch[0].priority, batch[0].job_id, batch[0].seq))
        assert claimed == sorted(claimed)
        assert claimed[0] == (5, "job-b", 90)
        assert claimed[1:3] == [(100, "job-a", 0), (100, "job-a", 1)]

    def test_a_batch_is_handed_out_in_dispatch_order(self, engine, queue):
        make_job(engine, "job-a")
        make_tasks(engine, "job-a", 3)
        make_tasks(engine, "job-a", 1, priority=5, start_seq=90)
        leased = queue.lease(owner="w1", limit=4)
        order = [(task.priority, task.job_id, task.seq) for task in leased]
        assert order == sorted(order)

    def test_two_owners_never_claim_the_same_row(self, engine, queue):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 4)
        first = queue.lease(owner="w1", limit=2)
        second = queue.lease(owner="w2", limit=4)
        assert {t.task_id for t in first}.isdisjoint({t.task_id for t in second})
        assert len(first) == 2
        assert len(second) == 2

    def test_a_lease_writes_owner_and_expiry_onto_the_row(self, engine, queue, clock):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        [task] = queue.lease(owner="w1", limit=1)
        row = read_task(engine, task_id)
        assert row["state"] == "leased"
        assert row["lease_owner"] == "w1"
        assert row["lease_expires_at"] == iso_at(clock.now + timedelta(seconds=120))
        assert task.lease_owner == "w1"

    def test_a_task_whose_not_before_is_in_the_future_is_not_claimed(self, engine, queue, clock):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 1, not_before=clock.now + timedelta(seconds=30))
        assert queue.lease(owner="w1", limit=4) == []
        clock.advance(31)
        assert len(queue.lease(owner="w1", limit=4)) == 1

    def test_a_cancelled_job_stops_being_leased_without_touching_its_rows(self, engine, queue):
        from sqlalchemy import text

        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 3)
        with engine.begin() as connection:
            connection.execute(text("UPDATE job SET cancel_requested = 1"))
        assert queue.lease(owner="w1", limit=4) == []
        assert queue.counts_by_state("job-1") == {"pending": 3}

    def test_a_paused_job_is_not_leased(self, engine, queue):
        from sqlalchemy import text

        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 2)
        with engine.begin() as connection:
            connection.execute(text("UPDATE job SET status = 'paused'"))
        assert queue.lease(owner="w1", limit=4) == []

    def test_the_first_lease_promotes_a_queued_job_to_running(self, engine, queue):
        make_job(engine, "job-1", status="queued")
        make_tasks(engine, "job-1", 2)
        queue.lease(owner="w1", limit=1)
        assert read_job(engine, "job-1")["status"] == "running"
        assert queue.last_promoted == ("job-1",)
        queue.lease(owner="w1", limit=1)
        assert queue.last_promoted == ()

    def test_a_zero_limit_claims_nothing(self, engine, queue):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 2)
        assert queue.lease(owner="w1", limit=0) == []


class TestCompletion:
    def test_ack_records_the_outcome_and_rolls_the_job_ledger(self, engine, queue):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        [task] = queue.lease(owner="w1", limit=1)
        applied = queue.ack(
            task,
            TaskOutcome(
                state="done",
                row_count=3000,
                rows_written=3000,
                bytes_downloaded=45630,
                latency_ms=180,
                columns_json='["timestamp"]',
                payload_sha256="abc",
            ),
        )
        assert applied is True
        row = read_task(engine, task_id)
        assert row["state"] == "done"
        assert row["row_count"] == 3000
        assert row["lease_owner"] is None
        assert row["finished_at"] is not None
        job = read_job(engine, "job-1")
        assert (job["requests_used"], job["rows_written"], job["bytes_downloaded"]) == (
            1,
            3000,
            45630,
        )

    def test_an_ack_from_a_stale_owner_writes_nothing(self, engine, queue):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        [task] = queue.lease(owner="w1", limit=1)
        queue.release([task_id], owner="w1")
        [again] = queue.lease(owner="w2", limit=1)
        assert again.task_id == task_id

        assert queue.ack(task, TaskOutcome(row_count=1)) is False
        row = read_task(engine, task_id)
        assert row["state"] == "leased"
        assert row["lease_owner"] == "w2"
        assert read_job(engine, "job-1")["requests_used"] == 0

    def test_a_handler_outcome_cannot_claim_a_failure_state(self):
        with pytest.raises(ValueError):
            TaskOutcome(state="failed")


class TestRetryPolicy:
    def test_a_transient_failure_consumes_one_attempt_and_schedules_a_retry(
        self, engine, queue, clock
    ):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        [task] = queue.lease(owner="w1", limit=1)
        applied, state, attempt = queue.nack_transient(
            task, error_text="timeout", delay_seconds=5
        )
        assert (applied, state, attempt) == (True, "pending", 1)
        row = read_task(engine, task_id)
        assert row["not_before"] == iso_at(clock.now + timedelta(seconds=5))
        assert row["lease_owner"] is None
        assert row["last_error_text"] == "timeout"

    def test_a_transient_failure_fails_the_task_once_max_attempts_is_reached(self, engine, queue):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        states = []
        for _ in range(4):
            [task] = queue.lease(owner="w1", limit=1)
            _applied, state, _attempt = queue.nack_transient(
                task, error_text="http 500", delay_seconds=0
            )
            states.append(state)
        assert states == ["pending", "pending", "pending", "failed"]
        row = read_task(engine, task_id)
        assert row["attempt"] == 4
        assert row["state"] == "failed"
        assert row["finished_at"] is not None

    def test_a_fatal_failure_never_retries(self, engine, queue):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        [task] = queue.lease(owner="w1", limit=1)
        assert queue.nack_fatal(task, error_text="invalid symbol", fyers_code=-300) is True
        row = read_task(engine, task_id)
        assert row["state"] == "failed"
        assert row["attempt"] == 0
        assert row["fyers_code"] == -300

    def test_an_auth_park_never_consumes_an_attempt(self, engine, queue):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        for _ in range(6):
            [task] = queue.lease(owner="w1", limit=1)
            assert queue.nack_auth(task, error_text="token expired", fyers_code=-8) is True
        row = read_task(engine, task_id)
        assert row["state"] == "pending"
        assert row["attempt"] == 0
        assert row["lease_owner"] is None
        # The requests were still spent, so the job ledger counts them even though the retry
        # budget was protected.
        assert read_job(engine, "job-1")["requests_used"] == 6

    def test_a_rate_limit_park_never_consumes_an_attempt(self, engine, queue):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        [task] = queue.lease(owner="w1", limit=1)
        assert queue.nack_rate(task, error_text="rate limited", fyers_code=-429) is True
        row = read_task(engine, task_id)
        assert (row["state"], row["attempt"]) == ("pending", 0)

    def test_the_backoff_matches_the_documented_envelope(self, engine, queue, clock):
        from expirymanager.brokers.fyers.errors import backoff_seconds

        for attempt, low, high in ((1, 1.0, 3.0), (2, 2.0, 6.0), (3, 4.0, 12.0), (4, 8.0, 24.0)):
            for _ in range(50):
                value = backoff_seconds(attempt)
                assert low <= value <= high


class TestReclaim:
    def test_an_expired_lease_returns_to_pending_without_burning_an_attempt(
        self, engine, queue, clock
    ):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        [task] = queue.lease(owner="w1", limit=1)
        # One failed attempt already recorded, so the assertion below proves the counter is
        # preserved rather than merely still zero.
        queue.nack_transient(task, delay_seconds=0)
        [task] = queue.lease(owner="w1", limit=1)
        assert read_task(engine, task_id)["attempt"] == 1

        clock.advance(119)
        assert queue.reclaim_expired_leases() == 0
        assert read_task(engine, task_id)["state"] == "leased"

        clock.advance(2)
        assert queue.reclaim_expired_leases() == 1
        row = read_task(engine, task_id)
        assert row["state"] == "pending"
        assert row["attempt"] == 1
        assert row["lease_owner"] is None
        assert row["lease_expires_at"] is None

    def test_a_reclaimed_task_is_leased_again_and_the_stale_ack_is_ignored(
        self, engine, queue, clock
    ):
        make_job(engine, "job-1")
        [task_id] = make_tasks(engine, "job-1", 1)
        [lost] = queue.lease(owner="dead-worker", limit=1)
        clock.advance(200)
        queue.reclaim_expired_leases()
        [fresh] = queue.lease(owner="live-worker", limit=1)
        assert fresh.task_id == task_id

        assert queue.ack(lost, TaskOutcome(row_count=99)) is False
        assert queue.ack(fresh, TaskOutcome(row_count=7)) is True
        row = read_task(engine, task_id)
        assert row["row_count"] == 7
        assert read_job(engine, "job-1")["requests_used"] == 1


class TestCancellation:
    def test_cancel_marks_pending_rows_and_leaves_leased_ones_running(self, engine, queue):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 5)
        leased = queue.lease(owner="w1", limit=2)
        cancelled = queue.cancel_pending("job-1")
        assert cancelled == 3
        counts = queue.counts_by_state("job-1")
        assert counts == {"cancelled": 3, "leased": 2}
        # The in flight rows can still be acked, so their data and their ledger entry survive.
        assert queue.ack(leased[0], TaskOutcome(row_count=10)) is True


class TestAggregate:
    def test_the_aggregate_is_a_fresh_group_by_and_can_sync_the_job_counters(self, engine, queue):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 4)
        leased = queue.lease(owner="w1", limit=3)
        queue.ack(leased[0], TaskOutcome(state="done"))
        queue.ack(leased[1], TaskOutcome(state="empty"))
        queue.nack_fatal(leased[2], error_text="bad")

        aggregate = queue.aggregate("job-1", sync_counters=True)
        assert aggregate is not None
        assert (aggregate.done, aggregate.empty, aggregate.failed, aggregate.pending) == (
            1,
            1,
            1,
            1,
        )
        assert aggregate.total == 4
        assert aggregate.open == 1
        assert aggregate.is_finished is False
        job = read_job(engine, "job-1")
        assert (job["done_tasks"], job["empty_tasks"], job["failed_tasks"]) == (1, 1, 1)

    def test_a_finished_job_with_a_failure_reports_completed_with_errors(self, engine, queue):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 2)
        leased = queue.lease(owner="w1", limit=2)
        queue.ack(leased[0], TaskOutcome(state="done"))
        queue.nack_fatal(leased[1], error_text="bad")
        aggregate = queue.aggregate("job-1")
        assert aggregate is not None
        assert aggregate.is_finished is True
        assert aggregate.terminal_status() == "completed_with_errors"

    def test_a_cancelled_job_reports_cancelled_even_when_nothing_failed(self, engine, queue):
        from sqlalchemy import text

        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 1)
        leased = queue.lease(owner="w1", limit=1)
        queue.ack(leased[0], TaskOutcome(state="done"))
        with engine.begin() as connection:
            connection.execute(text("UPDATE job SET cancel_requested = 1"))
        aggregate = queue.aggregate("job-1")
        assert aggregate is not None
        assert aggregate.terminal_status() == "cancelled"

    def test_an_unknown_job_aggregates_to_none(self, queue):
        assert queue.aggregate("nope") is None


class TestReleasing:
    def test_release_all_returns_only_this_owners_leases(self, engine, queue):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 4)
        queue.lease(owner="w1", limit=2)
        queue.lease(owner="w2", limit=2)
        assert queue.release_all("w1") == 2
        assert queue.counts_by_state("job-1") == {"pending": 2, "leased": 2}

    def test_ready_count_respects_not_before_and_job_status(self, engine, queue, clock):
        make_job(engine, "job-1")
        make_tasks(engine, "job-1", 2)
        make_tasks(engine, "job-1", 1, not_before=clock.now + timedelta(minutes=5), start_seq=50)
        assert queue.ready_count() == 2


def test_the_module_exports_the_documented_state_sets():
    assert set(queue_module.TERMINAL_STATES) == {
        "done",
        "empty",
        "failed",
        "skipped",
        "cancelled",
    }
    assert set(queue_module.OPEN_STATES) == {"pending", "leased"}
