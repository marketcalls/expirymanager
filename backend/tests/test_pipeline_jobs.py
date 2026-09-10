"""JobService: the commit gates, the single transaction, and the lifecycle.

The atomicity test is the important one. It forces the task insert to fail partway and then reads
the tables back, because a return value proves nothing about what a rolled back transaction left
behind. Everything else here is a gate: a stale plan, an overspent budget and a stopped pipeline
each have to refuse in a way the UI can render.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from expirymanager.api.schemas.downloads import DownloadRequest
from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.duck import DuckStore
from expirymanager.db.reader import DuckReader
from expirymanager.pipeline import jobs as jobs_module
from expirymanager.pipeline.jobs import JobService, JobServiceError
from expirymanager.pipeline.planner import Planner
from tests.test_pipeline_planner import (
    EXPIRY,
    TODAY,
    FakeGovernor,
    FakeSettings,
    cover,
    register_underlying,
    seed_catalog,
)

RES = "1"


class FakeSupervisor:
    """Records the two calls the job service is allowed to make."""

    def __init__(self) -> None:
        self.notified: list[str] = []
        self.cancelled: list[str] = []

    def notify(self, job_id: str | None = None) -> None:
        self.notified.append(str(job_id))

    async def cancel_job(self, job_id: str) -> int:
        self.cancelled.append(job_id)
        return 0


@pytest.fixture
def engine(tmp_path: Path):
    eng = sqlite_module.create_engine(tmp_path / "data" / "config.sqlite3")
    migrate_module.migrate(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def store(tmp_path: Path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    yield store
    store.close()


@pytest.fixture
def reader(store) -> DuckReader:
    return DuckReader(store)


@pytest.fixture
def world(engine, store, reader):
    register_underlying(engine)
    contracts = seed_catalog(store)
    return contracts


def build(engine, reader, *, used: int = 0, supervisor=None, governor=None) -> JobService:
    planner = Planner(
        reader=reader,
        engine=engine,
        settings=FakeSettings(),
        governor=governor or FakeGovernor(used=used),
        clock=lambda: TODAY,
    )
    counter = {"n": 0}

    def next_id() -> str:
        counter["n"] += 1
        return f"job-{counter['n']}"

    return JobService(
        engine=engine,
        planner=planner,
        supervisor=supervisor,
        governor=governor or FakeGovernor(used=used),
        id_factory=next_id,
    )


def order(**overrides) -> DownloadRequest:
    body = {
        "underlying_id": 1,
        "expiry_dates": [EXPIRY],
        "resolutions": [RES],
        "instrument_class": "OPT",
    }
    body.update(overrides)
    return DownloadRequest(**body)


def run(coro):
    return asyncio.run(coro)


def task_rows(engine, job_id: str) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM task WHERE job_id = :job_id ORDER BY seq"),
                {"job_id": job_id},
            ).mappings()
        ]


def job_count(engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM job")).scalar_one())


def task_count(engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM task")).scalar_one())


# ---------------------------------------------------------------------------
# Committing
# ---------------------------------------------------------------------------


def test_a_commit_writes_the_job_and_every_task_row(engine, reader, world):
    supervisor = FakeSupervisor()
    service = build(engine, reader, supervisor=supervisor)
    accepted = run(service.create(order(), created_by="tester"))

    assert accepted.status == "queued"
    assert accepted.total_tasks == len(world)
    rows = task_rows(engine, accepted.job_id)
    assert len(rows) == len(world)
    assert {row["state"] for row in rows} == {"pending"}
    assert [row["seq"] for row in rows] == list(range(len(world)))
    assert all(row["attempt"] == 0 for row in rows)
    assert all(row["not_before"] == row["created_at"] for row in rows)
    assert supervisor.notified == [accepted.job_id]


def test_the_committed_task_carries_the_request_identity_and_no_token(engine, reader, world):
    service = build(engine, reader)
    accepted = run(service.create(order()))
    row = task_rows(engine, accepted.job_id)[0]

    assert row["kind"] == "candle_chunk"
    assert row["resolution"] == RES
    assert row["range_to"] == EXPIRY.isoformat()
    assert row["include_oi"] == 1
    params = json.loads(row["request_params_json"])
    assert params["date_format"] == 1
    assert "token" not in json.dumps(params).lower()


def test_the_job_row_records_the_sheet_and_the_preview_it_was_priced_at(engine, reader, world):
    service = build(engine, reader)
    accepted = run(service.create(order()))
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT kind, params_json, est_requests, total_tasks FROM job"
                     " WHERE job_id = :job_id"),
                {"job_id": accepted.job_id},
            )
            .mappings()
            .first()
        )
    params = json.loads(row["params_json"])
    assert row["kind"] == "candle_backfill"
    assert row["est_requests"] == accepted.est_requests
    assert row["total_tasks"] == len(world)
    assert params["expiry_dates"] == [EXPIRY.isoformat()]
    assert params["preview"]["requests_estimated"] == accepted.est_requests


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_a_failure_partway_through_the_task_insert_leaves_no_job_at_all(
    engine, reader, world, monkeypatch
):
    service = build(engine, reader)
    calls = {"n": 0}
    original = JobService._insert_tasks

    def explode(self, connection, rows):
        calls["n"] += 1
        # Write the first half, then fail, so the rollback has something real to undo.
        original(self, connection, rows[: len(rows) // 2])
        raise RuntimeError("disk went away")

    monkeypatch.setattr(JobService, "_insert_tasks", explode)
    with pytest.raises(RuntimeError):
        run(service.create(order()))

    assert calls["n"] == 1
    assert job_count(engine) == 0
    assert task_count(engine) == 0


def test_a_task_row_the_schema_refuses_takes_the_whole_job_with_it(engine, reader, world):
    # ck_task_kind rejects the last row. A partial write here would leave the dispatcher running
    # tasks for a job whose total was never true, which is the failure this transaction exists
    # to make impossible.
    service = build(engine, reader)
    original_plan = service._planner.plan

    async def poisoned(request, **kwargs):
        plan = await original_plan(request, **kwargs)
        tasks = list(plan.tasks)
        tasks[-1] = dataclasses.replace(tasks[-1], kind="not_a_kind")
        return dataclasses.replace(plan, tasks=tuple(tasks))

    service._planner.plan = poisoned
    with pytest.raises(IntegrityError):
        run(service.create(order()))
    assert job_count(engine) == 0
    assert task_count(engine) == 0


def test_a_job_larger_than_one_insert_batch_is_still_one_transaction(
    engine, reader, world, monkeypatch
):
    # The batch size is a memory bound, never a transaction boundary. Shrinking it to two forces
    # several batches for six tasks, and a failure on a later one must still undo the earlier.
    monkeypatch.setattr(jobs_module, "TASK_INSERT_BATCH", 2)
    service = build(engine, reader)
    accepted = run(service.create(order()))
    assert len(task_rows(engine, accepted.job_id)) == len(world)

    original_plan = service._planner.plan

    async def poisoned(request, **kwargs):
        plan = await original_plan(request, **kwargs)
        tasks = list(plan.tasks)
        tasks[-1] = dataclasses.replace(tasks[-1], kind="not_a_kind")
        return dataclasses.replace(plan, tasks=tuple(tasks))

    service._planner.plan = poisoned
    before_jobs, before_tasks = job_count(engine), task_count(engine)
    with pytest.raises(IntegrityError):
        run(service.create(order(force_refresh=True)))
    assert (job_count(engine), task_count(engine)) == (before_jobs, before_tasks)


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def test_a_stale_confirm_requests_is_refused_with_both_numbers(engine, reader, world):
    service = build(engine, reader)
    with pytest.raises(JobServiceError) as caught:
        run(service.create(order(confirm_requests=999)))

    assert caught.value.code == "plan_changed"
    assert caught.value.status_code == 409
    assert caught.value.detail["confirm_requests"] == 999
    assert caught.value.detail["requests_estimated"] == len(world)
    assert job_count(engine) == 0


def test_a_matching_confirm_requests_commits(engine, reader, world):
    service = build(engine, reader)
    preview = run(service.estimate(order()))
    accepted = run(service.create(order(confirm_requests=preview.requests_estimated)))
    assert accepted.status == "queued"


def test_an_estimate_the_user_never_saw_is_not_gated(engine, reader, world):
    # A schedule fire passes no confirm_requests and must not be refused for it.
    service = build(engine, reader)
    accepted = run(service.create(order(), schedule_id=None))
    assert accepted.total_tasks == len(world)


def test_coverage_landing_between_the_plan_and_the_commit_moves_the_number(
    engine, store, reader, world
):
    service = build(engine, reader)
    preview = run(service.estimate(order()))
    cover(store, world[0], 2, EXPIRY - timedelta(days=99), EXPIRY)
    # The chunk is now held, so the probe moves one chunk older and the count is unchanged, but
    # a fully covered contract would change it. Cover the whole life window to prove the gate.
    for contract_id in world:
        cover(store, contract_id, 2, EXPIRY - timedelta(days=200), EXPIRY)
    with pytest.raises(JobServiceError) as caught:
        run(service.create(order(confirm_requests=preview.requests_estimated)))
    assert caught.value.code == "plan_changed"


def test_a_plan_beyond_the_remaining_budget_is_refused_with_the_reason(engine, reader, world):
    service = build(engine, reader, used=99_999)
    with pytest.raises(JobServiceError) as caught:
        run(service.create(order()))

    assert caught.value.code == "exceeds_budget"
    assert caught.value.status_code == 409
    assert caught.value.detail["budget_allowance"] == 1
    assert caught.value.detail["requests_estimated"] == len(world)
    assert job_count(engine) == 0


def test_defer_to_tomorrow_commits_the_job_as_deferred_budget(engine, reader, world):
    supervisor = FakeSupervisor()
    service = build(engine, reader, used=99_999, supervisor=supervisor)
    accepted = run(service.create(order(defer_to_tomorrow=True)))

    assert accepted.status == "deferred_budget"
    assert accepted.deferred is True
    assert len(task_rows(engine, accepted.job_id)) == len(world)
    # Deferred work must not wake the dispatcher: the 00:01 IST reset moves it to queued.
    assert supervisor.notified == []


def test_a_sweep_is_held_to_the_reserve_while_an_interactive_download_is_not(
    engine, reader, world
):
    sweep = build(engine, reader, used=69_999)
    with pytest.raises(JobServiceError) as caught:
        run(sweep.create(order(), schedule_id="rolling_backfill"))
    assert caught.value.code == "exceeds_budget"
    assert caught.value.detail["reserve_applied"] is True

    interactive = build(engine, reader, used=69_999)
    assert run(interactive.create(order())).status == "queued"


def test_a_fatally_stopped_pipeline_takes_no_new_work(engine, reader, world):
    class StoppedGovernor(FakeGovernor):
        mode = "stopped_fatal"
        reason = "manual intervention required"

    service = build(engine, reader, governor=StoppedGovernor())
    with pytest.raises(JobServiceError) as caught:
        run(service.create(order()))
    assert caught.value.code == "pipeline_stopped"
    assert caught.value.status_code == 503


def test_a_plan_with_nothing_to_do_is_refused_rather_than_committed_as_an_empty_job(
    engine, store, reader
):
    register_underlying(engine, option_life_days=10)
    contracts = seed_catalog(store)
    for contract_id in contracts:
        cover(store, contract_id, 2, EXPIRY - timedelta(days=10), EXPIRY)
    service = build(engine, reader)
    with pytest.raises(JobServiceError) as caught:
        run(service.create(order()))
    assert caught.value.code == "nothing_to_download"
    assert caught.value.detail["chunks_skipped_covered"] == len(contracts)
    assert job_count(engine) == 0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_pause_and_resume_move_the_job_and_touch_no_task_row(engine, reader, world):
    supervisor = FakeSupervisor()
    service = build(engine, reader, supervisor=supervisor)
    accepted = run(service.create(order()))
    before = task_rows(engine, accepted.job_id)

    paused = service.pause(accepted.job_id)
    assert paused.status == "paused"
    assert task_rows(engine, accepted.job_id) == before

    resumed = service.resume(accepted.job_id)
    assert resumed.status == "queued"
    assert task_rows(engine, accepted.job_id) == before
    assert supervisor.notified == [accepted.job_id, accepted.job_id]


def test_pause_is_idempotent_and_a_finished_job_refuses_both(engine, reader, world):
    service = build(engine, reader)
    accepted = run(service.create(order()))
    service.pause(accepted.job_id)
    assert service.pause(accepted.job_id).status == "paused"

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE job SET status = 'completed' WHERE job_id = :job_id"),
            {"job_id": accepted.job_id},
        )
    with pytest.raises(JobServiceError) as caught:
        service.pause(accepted.job_id)
    assert caught.value.code == "job_finished"


def test_a_deferred_job_resumes_into_the_queue(engine, reader, world):
    service = build(engine, reader, used=99_999)
    accepted = run(service.create(order(defer_to_tomorrow=True)))
    assert service.resume(accepted.job_id).status == "queued"


def test_cancel_stops_pending_work_and_leaves_the_ledger_accurate(engine, reader, world):
    service = build(engine, reader)
    accepted = run(service.create(order()))
    result = run(service.cancel(accepted.job_id))

    assert result.tasks_cancelled == len(world)
    rows = task_rows(engine, accepted.job_id)
    assert {row["state"] for row in rows} == {"cancelled"}
    assert service.get(accepted.job_id)["status"] == "cancelled"
    assert service.get(accepted.job_id)["cancel_requested"] == 1


def test_cancel_delegates_to_the_supervisor_when_there_is_one(engine, reader, world):
    supervisor = FakeSupervisor()
    service = build(engine, reader, supervisor=supervisor)
    accepted = run(service.create(order()))
    run(service.cancel(accepted.job_id))
    assert supervisor.cancelled == [accepted.job_id]


def test_cancelling_a_finished_job_is_a_no_op(engine, reader, world):
    service = build(engine, reader)
    accepted = run(service.create(order()))
    run(service.cancel(accepted.job_id))
    again = run(service.cancel(accepted.job_id))
    assert again.status == "cancelled"


def test_retry_failed_creates_a_child_and_never_rewrites_the_parent(engine, reader, world):
    supervisor = FakeSupervisor()
    service = build(engine, reader, supervisor=supervisor)
    accepted = run(service.create(order()))
    rows = task_rows(engine, accepted.job_id)
    failed_ids = [row["task_id"] for row in rows[:2]]
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE task SET state = 'failed', last_error_text = 'boom'"
                " WHERE task_id IN (:a, :b)"
            ),
            {"a": failed_ids[0], "b": failed_ids[1]},
        )

    child = service.retry_failed(accepted.job_id)
    assert child.parent_job_id == accepted.job_id
    assert child.total_tasks == 2

    parent_rows = task_rows(engine, accepted.job_id)
    assert sum(1 for row in parent_rows if row["state"] == "failed") == 2
    assert all(row["last_error_text"] == "boom" for row in parent_rows if row["state"] == "failed")

    child_rows = task_rows(engine, child.job_id)
    assert [row["state"] for row in child_rows] == ["pending", "pending"]
    assert [row["attempt"] for row in child_rows] == [0, 0]
    assert [row["parent_task_id"] for row in child_rows] == failed_ids
    assert service.get(accepted.job_id)["children"] == [child.job_id]
    assert supervisor.notified[-1] == child.job_id


def test_retry_failed_with_nothing_failed_is_refused(engine, reader, world):
    service = build(engine, reader)
    accepted = run(service.create(order()))
    with pytest.raises(JobServiceError) as caught:
        service.retry_failed(accepted.job_id)
    assert caught.value.code == "no_failed_tasks"


def test_get_returns_a_fresh_aggregate_over_durable_rows(engine, reader, world):
    service = build(engine, reader)
    accepted = run(service.create(order()))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE task SET state = 'done' WHERE job_id = :job_id AND seq < 2"),
            {"job_id": accepted.job_id},
        )
    record = service.get(accepted.job_id)
    assert record["counts"]["done"] == 2
    assert record["counts"]["pending"] == len(world) - 2
    assert record["open_tasks"] == len(world) - 2


def test_get_on_a_missing_job_is_a_404(engine, reader):
    service = build(engine, reader)
    with pytest.raises(JobServiceError) as caught:
        service.get("nope")
    assert caught.value.status_code == 404


def test_list_jobs_pages_newest_first(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store)
    seed_catalog(store, expiry=date(2025, 4, 24), strikes=(23000,))
    service = build(engine, reader)
    first = run(service.create(order()))
    second = run(service.create(order(expiry_dates=[date(2025, 4, 24)])))

    rows, cursor = service.list_jobs(limit=1)
    assert len(rows) == 1
    assert rows[0]["job_id"] in {first.job_id, second.job_id}
    assert cursor is not None

    rest, _ = service.list_jobs(limit=10, cursor=cursor)
    assert {row["job_id"] for row in rows} | {row["job_id"] for row in rest} == {
        first.job_id,
        second.job_id,
    }


def test_list_jobs_filters_by_status_and_kind(engine, reader, world):
    service = build(engine, reader)
    accepted = run(service.create(order()))
    assert [row["job_id"] for row in service.list_jobs(status="queued")[0]] == [accepted.job_id]
    assert service.list_jobs(status="completed")[0] == []
    assert [row["job_id"] for row in service.list_jobs(kind="candle_backfill")[0]] == [
        accepted.job_id
    ]


# ---------------------------------------------------------------------------
# Startup recovery and wiring
# ---------------------------------------------------------------------------


def test_a_deferred_job_is_released_once_the_ist_date_has_rolled(engine, reader, world):
    service = build(engine, reader, used=99_999)
    accepted = run(service.create(order(defer_to_tomorrow=True)))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE job SET created_at = :then WHERE job_id = :job_id"),
            {"then": "2020-01-01T00:00:00.000000+00:00", "job_id": accepted.job_id},
        )
    assert jobs_module.roll_deferred_budget_jobs(engine, ist_today=date(2020, 1, 2)) == 1
    assert service.get(accepted.job_id)["status"] == "queued"


def test_a_deferred_job_stays_parked_on_a_restart_inside_the_same_day(engine, reader, world):
    # The budget it was deferred for is still spent, so a restart must not release it.
    service = build(engine, reader, used=99_999)
    accepted = run(service.create(order(defer_to_tomorrow=True)))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE job SET created_at = :then WHERE job_id = :job_id"),
            {"then": "2020-01-01T18:00:00.000000+00:00", "job_id": accepted.job_id},
        )
    assert jobs_module.roll_deferred_budget_jobs(engine, ist_today=date(2020, 1, 1)) == 0
    assert service.get(accepted.job_id)["status"] == "deferred_budget"


def test_a_preserved_blocking_mode_is_restored_across_a_restart(engine, reader):
    from expirymanager.brokers.fyers.governor import FyersGovernor, GovernorMode

    governor = FyersGovernor()
    assert governor.mode is GovernorMode.RUNNING
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE pipeline_state SET mode = 'paused_rate', reason = 'two strikes spent'"
                " WHERE id = 1"
            )
        )
    assert jobs_module.restore_pipeline_mode(engine, governor) == "paused_rate"
    assert governor.mode is GovernorMode.PAUSED_RATE
    assert governor.reason == "two strikes spent"


def test_a_running_mode_is_not_restored_because_there_is_nothing_to_preserve(engine, reader):
    from expirymanager.brokers.fyers.governor import FyersGovernor, GovernorMode

    governor = FyersGovernor()
    assert jobs_module.restore_pipeline_mode(engine, governor) is None
    assert governor.mode is GovernorMode.RUNNING


def test_install_registers_the_job_recovery_slot():
    from expirymanager.lifespan import (
        SLOT_JOB_RECOVERY,
        registered_components,
        unregister_component,
    )

    unregister_component(SLOT_JOB_RECOVERY)
    try:
        jobs_module.install()
        assert SLOT_JOB_RECOVERY in registered_components()
    finally:
        unregister_component(SLOT_JOB_RECOVERY)
