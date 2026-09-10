"""The scheduler: the rebuild from SQLite, both guards, run-now, and the 03:00 logout.

Everything here runs on a fake clock. A schedule test that waited for a wall clock minute would
either be slow or be a sleep, and neither proves that the guard ran before the action did.

The two tests that matter most are the last two. The 03:00 logout has to park a running job and
not fail it, and the same job has to come back at the exact task after a login, because that is
the difference between an overnight backfill that resumes in the morning and one that has to be
re-planned. And a token past its JWT exp has to read as expired the moment it is read, without
waiting for any sweep, because until this item that state was a column that never ticked.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from expirymanager.api.schemas.schedules import (
    ScheduleCreate,
    ScheduleUpdate,
    validate_cron,
)
from expirymanager.brokers.fyers import tokens as tokens_module
from expirymanager.brokers.fyers.tokens import (
    FyersCredentials,
    InMemoryTokenStore,
    TokenBroker,
    TokenRecord,
    TokenState,
    effective_token_state,
)
from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.duck import DuckStore
from expirymanager.db.reader import DuckReader
from expirymanager.pipeline.queue import LeaseQueue, iso_at, utc_now
from expirymanager.pipeline.supervisor import PipelineSupervisor
from expirymanager.scheduler import jobs_def
from expirymanager.scheduler.service import (
    BuiltinSchedule,
    InvalidCron,
    ScheduleNotFound,
    SchedulerService,
    UnknownScheduleKind,
)
from tests.test_pipeline_planner import register_underlying, seed_catalog

IST_NOON_TUESDAY = datetime(2025, 6, 3, 12, 0, 0, tzinfo=tokens_module.UTC)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeCredentials:
    """One synthetic app registration. No real credential appears anywhere in this file."""

    def __init__(self) -> None:
        self.row = FyersCredentials(
            credential_id="cred-1",
            app_id="TESTAPP-100",
            app_secret="not-a-real-secret",
            redirect_uri="http://127.0.0.1:8000/fyers/callback",
        )

    def load_active(self):
        return self.row


class FakeSnapshot:
    def __init__(self, **values) -> None:
        self.mode = values.get("mode", "running")
        self.blocked_until = values.get("blocked_until")
        self.daily_budget = values.get("daily_budget", 100_000)
        self.requests_used = values.get("requests_used", 0)
        self.per_minute = 170


class FakeGovernor:
    def __init__(self, **values) -> None:
        self._values = values
        self.mode = values.get("mode", "running")
        self.refreshed = 0
        self.paused_auth: list[str] = []
        self.daily_budget = values.get("daily_budget", 100_000)
        self.requests_used = values.get("requests_used", 0)

    def snapshot(self) -> FakeSnapshot:
        return FakeSnapshot(**self._values)

    async def refresh_day(self) -> str:
        self.refreshed += 1
        return "running"

    async def pause_auth(self, *, reason: str = "") -> None:
        self.paused_auth.append(reason)
        self.mode = "paused_auth"

    async def resume(self, *, by: str | None = None) -> str:
        self.mode = "running"
        return "running"


class FakeSettings:
    def __init__(self, **values) -> None:
        self._values = {"budget_reserve_fraction": 0.70, "throttle_per_minute": 170}
        self._values.update(values)

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self._values.get(key, default))

    def get_float(self, key: str) -> float:
        return float(self._values[key])


class Services:
    """The subset of AppState the scheduler and the actions reach for."""

    def __init__(self, **values) -> None:
        self.engine = values.get("engine")
        self.settings = values.get("settings") or FakeSettings()
        self.governor = values.get("governor")
        self.token_broker = values.get("token_broker")
        self.duck = values.get("duck")
        self.components: dict = {}
        self._supervisor = values.get("supervisor")

    @property
    def duck_reader(self):
        return None if self.duck is None else self.duck.reader

    @property
    def duck_writer(self):
        return None if self.duck is None else self.duck.writer

    @property
    def supervisor(self):
        return self._supervisor


class RecordingJobService:
    """Stands in for W12's JobService. Records every sheet the actions priced.

    It writes a real job row, because `schedule_run.job_id` is a foreign key and a double that
    handed back an id nothing points at would pass a test the production path cannot pass.
    """

    def __init__(self, engine=None, *, refuse: str | None = None) -> None:
        self.requests: list = []
        self.refuse = refuse
        self._engine = engine
        self._n = 0

    async def create(self, request, **kwargs):
        self.requests.append((request, kwargs))
        if self.refuse:
            from expirymanager.pipeline.planner import PipelineRequestError

            raise PipelineRequestError(self.refuse, "refused", status_code=409)
        self._n += 1
        job_id = f"{id(self):x}-{self._n}"
        if self._engine is not None:
            with self._engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO job (job_id, kind, status, params_json, schedule_id,"
                        " created_at) VALUES (:id, 'candle_backfill', 'queued', '{}', :sched,"
                        " '2025-06-03T12:00:00+00:00')"
                    ),
                    {"id": job_id, "sched": kwargs.get("schedule_id")},
                )

        class _Accepted:
            pass

        accepted = _Accepted()
        accepted.job_id = job_id
        return accepted


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


class DuckHolder:
    def __init__(self, reader) -> None:
        self.reader = reader
        self.writer = None


def broker_with_token(
    *, expires_in_seconds: float | None = 3600, governor=None
) -> TokenBroker:
    store = InMemoryTokenStore()
    broker = TokenBroker(credentials=FakeCredentials(), tokens=store, governor=governor)
    expires_at = (
        None
        if expires_in_seconds is None
        else (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).isoformat()
    )
    store.save(
        credential_id="cred-1",
        access_token="synthetic.access.token",
        refresh_token=None,
        generation=1,
        access_expires_at=expires_at,
        refresh_expires_at=None,
    )
    broker.reload()
    return broker


_IDS = {"n": 0}


def build(engine, *, services=None, clock=None, job_service=None, autostart=True):
    def next_id() -> str:
        # Unique across every service a test builds, because two services writing to one engine
        # would otherwise collide on the schedule_run primary key.
        _IDS["n"] += 1
        return f"id-{_IDS['n']}"

    return SchedulerService(
        engine=engine,
        services=services,
        job_service_factory=(lambda: job_service) if job_service is not None else None,
        clock=clock or (lambda: IST_NOON_TUESDAY),
        id_factory=next_id,
        autostart=autostart,
    )


def enable_only(engine, schedule_id: str) -> None:
    """Leave exactly one schedule enabled, so a sync installs exactly one trigger."""
    with engine.begin() as connection:
        connection.execute(text("UPDATE schedule SET enabled = 0"))
        connection.execute(
            text("UPDATE schedule SET enabled = 1 WHERE schedule_id = :id"),
            {"id": schedule_id},
        )


# ---------------------------------------------------------------------------
# The seeded schedules
# ---------------------------------------------------------------------------


class TestBuiltinSchedules:
    def test_migration_seeds_every_builtin_schedule(self, engine):
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT schedule_id, kind, is_builtin FROM schedule")
            ).all()
        assert {row[0] for row in rows} == set(jobs_def.BUILTIN_SCHEDULE_IDS)
        assert all(row[2] == 1 for row in rows)

    def test_every_seeded_kind_has_an_action_registered(self, engine):
        with engine.connect() as connection:
            kinds = set(connection.execute(text("SELECT kind FROM schedule")).scalars().all())
        assert kinds <= set(jobs_def.known_kinds())

    def test_every_seeded_cron_is_one_apscheduler_will_accept(self, engine):
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT cron, timezone FROM schedule")).all()
        for expression, timezone in rows:
            assert validate_cron(expression, timezone) == expression

    def test_the_documented_times_and_priorities_are_what_was_seeded(self, engine):
        with engine.connect() as connection:
            rows = dict(
                connection.execute(text("SELECT kind, cron FROM schedule")).all()
            )
        assert rows["symbol_master"] == "15 8 * * *"
        assert rows["seconds_capture"] == "15 16 * * 1-5"
        assert rows["rolling_backfill"] == "30 18 * * 1-5"
        assert rows["token_health"] == "0 * * * *"
        assert rows["maintenance"] == "0 2 * * *"
        assert rows["budget_reset"] == "1 0 * * *"
        # The explicit user requirement: token loss is a planned 03:00 event.
        assert rows["token_logout"] == "0 3 * * *"

    def test_the_market_data_schedules_need_a_token_and_the_others_do_not(self):
        assert jobs_def.needs_token("rolling_backfill") is True
        assert jobs_def.needs_token("seconds_capture") is True
        # The one irreplaceable job reads unauthenticated public files, so a dead token must not
        # be the thing that stops it.
        assert jobs_def.needs_token("symbol_master") is False
        assert jobs_def.spends_budget("symbol_master") is False
        assert jobs_def.needs_token("token_logout") is False
        assert jobs_def.needs_token("budget_reset") is False


# ---------------------------------------------------------------------------
# The rebuild
# ---------------------------------------------------------------------------


class TestSync:
    def test_startup_rebuilds_every_enabled_trigger_from_the_table(self, engine):
        service = build(engine)
        asyncio.run(service.start())
        try:
            installed = {job.id for job in service.scheduler.get_jobs()}
            assert installed == set(jobs_def.BUILTIN_SCHEDULE_IDS)
        finally:
            asyncio.run(service.stop())

    def test_a_disabled_row_installs_no_trigger_at_all(self, engine):
        enable_only(engine, "builtin_maintenance")
        service = build(engine)
        asyncio.run(service.start())
        try:
            assert [job.id for job in service.scheduler.get_jobs()] == ["builtin_maintenance"]
        finally:
            asyncio.run(service.stop())

    def test_enabling_a_row_and_syncing_installs_it(self, engine):
        enable_only(engine, "builtin_maintenance")
        service = build(engine)

        async def scenario():
            # One loop for the whole scenario: a started AsyncIOScheduler holds the loop it was
            # started on, and a mutation from outside it would be a wakeup on a closed loop.
            await service.start()
            try:
                service.set_enabled("builtin_token_logout", True)
                return {job.id for job in service.scheduler.get_jobs()}
            finally:
                await service.stop()

        assert "builtin_token_logout" in asyncio.run(scenario())

    def test_a_deleted_schedule_cannot_come_back_from_the_scheduler(self, engine):
        service = build(engine)

        async def scenario():
            await service.start()
            try:
                row = service.create(
                    ScheduleCreate(name="Mine", kind="maintenance", cron="0 4 * * *")
                )
                assert row.schedule_id in {job.id for job in service.scheduler.get_jobs()}
                service.delete(row.schedule_id)
                # The rebuild is from the table, so nothing survives that the table does not name.
                return row.schedule_id, {job.id for job in service.scheduler.get_jobs()}
            finally:
                await service.stop()

        schedule_id, installed = asyncio.run(scenario())
        assert schedule_id not in installed
        with pytest.raises(ScheduleNotFound):
            service.get_spec(schedule_id)

    def test_next_fire_at_is_written_back_onto_the_rows(self, engine):
        enable_only(engine, "builtin_maintenance")
        service = build(engine)
        asyncio.run(service.start())
        try:
            with engine.connect() as connection:
                rows = dict(
                    connection.execute(
                        text("SELECT schedule_id, next_fire_at FROM schedule")
                    ).all()
                )
            assert rows["builtin_maintenance"] is not None
            # A disabled schedule shows nothing rather than a time that will never arrive.
            assert rows["builtin_token_logout"] is None
        finally:
            asyncio.run(service.stop())

    def test_a_row_whose_kind_has_no_action_does_not_stop_the_other_triggers(self, engine):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO schedule (schedule_id, name, kind, cron, timezone, params_json,"
                    " enabled, trading_days_only, misfire_grace_seconds, is_builtin, created_at,"
                    " updated_at) VALUES ('orphan', 'Orphan', 'no_such_kind', '0 5 * * *',"
                    " 'Asia/Kolkata', '{}', 1, 0, 3600, 0, '2025-01-01', '2025-01-01')"
                )
            )
        service = build(engine)
        asyncio.run(service.start())
        try:
            installed = {job.id for job in service.scheduler.get_jobs()}
            assert "orphan" not in installed
            assert installed == set(jobs_def.BUILTIN_SCHEDULE_IDS)
        finally:
            asyncio.run(service.stop())


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCrud:
    def test_an_unrunnable_cron_is_refused_with_the_documented_code(self, engine):
        service = build(engine)
        with pytest.raises(ValueError):
            ScheduleCreate(name="Bad", kind="maintenance", cron="not a cron")
        with pytest.raises(InvalidCron) as caught:
            service.update("builtin_maintenance", ScheduleUpdate.model_construct(cron="61 * * * *"))
        assert caught.value.code == "invalid_cron"

    def test_an_unknown_kind_is_refused(self, engine):
        service = build(engine)
        with pytest.raises(UnknownScheduleKind) as caught:
            service.create(ScheduleCreate(name="X", kind="not_a_kind", cron="0 4 * * *"))
        assert caught.value.code == "unknown_kind"

    def test_a_builtin_cannot_be_deleted_but_can_be_retimed_and_disabled(self, engine):
        service = build(engine)
        with pytest.raises(BuiltinSchedule) as caught:
            service.delete("builtin_rolling_backfill")
        assert caught.value.code == "builtin_schedule"
        row = service.update(
            "builtin_rolling_backfill",
            ScheduleUpdate(cron="0 19 * * 1-5", enabled=False),
        )
        assert row.cron == "0 19 * * 1-5"
        assert row.enabled is False

    def test_a_builtin_cannot_be_re_kinded_because_the_field_does_not_exist(self):
        with pytest.raises(ValueError):
            ScheduleUpdate(kind="maintenance")

    def test_params_survive_a_round_trip(self, engine):
        service = build(engine)
        service.update("builtin_rolling_backfill", ScheduleUpdate(params={"max_expiries": 7}))
        spec = service.get_spec("builtin_rolling_backfill")
        assert spec.int_param("max_expiries", 40) == 7


# ---------------------------------------------------------------------------
# Guard one: authentication
# ---------------------------------------------------------------------------


class TestAuthGuard:
    def test_a_market_data_schedule_is_skipped_when_there_is_no_token(self, engine):
        services = Services(engine=engine, governor=FakeGovernor())
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome == "skipped_needs_auth"
        assert result.job_id is None

    def test_the_skip_writes_a_notification_the_banner_can_render(self, engine):
        services = Services(engine=engine, governor=FakeGovernor())
        service = build(engine, services=services)
        asyncio.run(service.run_now("builtin_rolling_backfill"))
        with engine.connect() as connection:
            codes = connection.execute(text("SELECT code FROM notification")).scalars().all()
        assert "needs_reauth" in codes

    def test_an_expired_token_is_a_skip_and_not_a_run(self, engine):
        broker = broker_with_token(expires_in_seconds=-60)
        services = Services(engine=engine, token_broker=broker, governor=FakeGovernor())
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_seconds_capture"))
        assert result.outcome == "skipped_needs_auth"

    def test_a_pipeline_parked_on_auth_skips_even_with_a_token(self, engine):
        services = Services(
            engine=engine,
            token_broker=broker_with_token(),
            governor=FakeGovernor(mode="paused_auth"),
        )
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome == "skipped_needs_auth"

    def test_the_symbol_master_still_runs_with_a_dead_token(self, engine):
        services = Services(engine=engine, governor=FakeGovernor())
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_symbol_master"))
        assert result.outcome == "enqueued"
        with engine.connect() as connection:
            kinds = connection.execute(text("SELECT kind FROM task")).scalars().all()
        assert kinds == ["symbol_master"]


# ---------------------------------------------------------------------------
# Guard two: budget and blocks
# ---------------------------------------------------------------------------


class TestBudgetGuard:
    def _service(self, engine, governor, job_service=None):
        services = Services(
            engine=engine, token_broker=broker_with_token(), governor=governor
        )
        return build(engine, services=services, job_service=job_service)

    def test_a_broker_block_still_in_force_is_recorded_as_blocked(self, engine):
        until = (IST_NOON_TUESDAY + timedelta(hours=2)).isoformat()
        service = self._service(engine, FakeGovernor(blocked_until=until))
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome == "skipped_blocked"

    def test_a_lapsed_block_does_not_stop_the_run(self, engine):
        until = (IST_NOON_TUESDAY - timedelta(hours=2)).isoformat()
        service = self._service(
            engine, FakeGovernor(blocked_until=until), job_service=RecordingJobService(engine)
        )
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome != "skipped_blocked"

    def test_a_stopped_budget_pipeline_is_recorded_as_skipped_budget(self, engine):
        service = self._service(engine, FakeGovernor(mode="stopped_budget"))
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome == "skipped_budget"

    def test_a_fatally_stopped_pipeline_is_recorded_as_blocked(self, engine):
        service = self._service(engine, FakeGovernor(mode="stopped_fatal"))
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome == "skipped_blocked"

    def test_an_exhausted_sweep_reserve_stops_the_sweep_before_it_plans(self, engine):
        # 70 percent of 100,000 is 70,000. At 69,999 spent the sweep has one request left, at
        # 70,000 it has none, and the scheduler must not be the thing that spends past the
        # reserve an interactive download depends on.
        allowed = self._service(
            engine,
            FakeGovernor(requests_used=69_999),
            job_service=RecordingJobService(engine),
        )
        assert asyncio.run(allowed.run_now("builtin_rolling_backfill")).outcome != "skipped_budget"
        exhausted = self._service(engine, FakeGovernor(requests_used=70_000))
        assert asyncio.run(exhausted.run_now("builtin_rolling_backfill")).outcome == "skipped_budget"

    def test_the_budget_guard_does_not_apply_to_a_schedule_that_spends_nothing(self, engine):
        service = self._service(engine, FakeGovernor(mode="stopped_budget"))
        result = asyncio.run(service.run_now("builtin_symbol_master"))
        assert result.outcome == "enqueued"


# ---------------------------------------------------------------------------
# The trading day guard, and disabled schedules
# ---------------------------------------------------------------------------


class TestFireGuards:
    def test_a_disabled_schedule_does_not_fire(self, engine):
        service = build(engine, services=Services(engine=engine))
        service.set_enabled("builtin_maintenance", False)
        result = asyncio.run(service.fire("builtin_maintenance"))
        assert result.outcome == "skipped_disabled"

    def test_run_now_overrides_disabled_because_a_user_asked_explicitly(self, engine):
        service = build(engine, services=Services(engine=engine))
        service.set_enabled("builtin_maintenance", False)
        result = asyncio.run(service.run_now("builtin_maintenance"))
        assert result.outcome == "completed"

    def test_a_weekend_skips_a_trading_days_only_schedule(self, engine):
        saturday = datetime(2025, 6, 7, 12, 0, tzinfo=UTC)
        services = Services(
            engine=engine, token_broker=broker_with_token(), governor=FakeGovernor()
        )
        service = build(engine, services=services, clock=lambda: saturday)
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome == "skipped_holiday"
        assert result.note is not None and "weekend" in result.note

    def test_an_exchange_holiday_skips_a_trading_days_only_schedule(self, engine):
        # 2025-08-15 is Independence Day and is in the seeded holiday list for both exchanges.
        holiday = datetime(2025, 8, 15, 12, 0, tzinfo=UTC)
        services = Services(
            engine=engine, token_broker=broker_with_token(), governor=FakeGovernor()
        )
        service = build(engine, services=services, clock=lambda: holiday)
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome == "skipped_holiday"
        assert result.note is not None and "holiday" in result.note

    def test_a_holiday_does_not_stop_a_schedule_that_is_not_trading_days_only(self, engine):
        holiday = datetime(2025, 8, 15, 12, 0, tzinfo=UTC)
        services = Services(engine=engine, governor=FakeGovernor())
        service = build(engine, services=services, clock=lambda: holiday)
        result = asyncio.run(service.run_now("builtin_symbol_master"))
        assert result.outcome == "enqueued"

    def test_an_action_that_raises_is_recorded_as_an_error_and_not_propagated(self, engine):
        async def boom(ctx):
            raise RuntimeError("the action exploded")

        jobs_def.register_action("test_boom", boom, needs_token=False, spends_budget=False)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO schedule (schedule_id, name, kind, cron, timezone,"
                        " params_json, enabled, trading_days_only, misfire_grace_seconds,"
                        " is_builtin, created_at, updated_at) VALUES ('boom', 'Boom',"
                        " 'test_boom', '0 5 * * *', 'Asia/Kolkata', '{}', 1, 0, 3600, 0,"
                        " '2025-01-01', '2025-01-01')"
                    )
                )
            service = build(engine, services=Services(engine=engine))
            result = asyncio.run(service.run_now("boom"))
            assert result.outcome == "error"
            assert "the action exploded" in (result.note or "")
        finally:
            jobs_def.unregister_action("test_boom")


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


class TestRunHistory:
    def test_run_now_records_a_row_and_stamps_the_schedule(self, engine):
        service = build(engine, services=Services(engine=engine))
        result = asyncio.run(service.run_now("builtin_maintenance"))
        history = service.runs("builtin_maintenance")
        assert [row.run_id for row in history] == [result.run_id]
        assert history[0].outcome == "completed"
        with engine.connect() as connection:
            fired = connection.execute(
                text("SELECT last_fired_at FROM schedule WHERE schedule_id = 'builtin_maintenance'")
            ).scalar()
        assert fired == IST_NOON_TUESDAY.isoformat()

    def test_history_is_newest_first_and_bounded(self, engine):
        moments = [IST_NOON_TUESDAY + timedelta(minutes=index) for index in range(4)]
        cursor = {"n": 0}

        def clock():
            value = moments[min(cursor["n"], len(moments) - 1)]
            cursor["n"] += 1
            return value

        service = build(engine, services=Services(engine=engine), clock=clock)
        for _ in moments:
            asyncio.run(service.run_now("builtin_maintenance"))
        history = service.runs("builtin_maintenance", limit=2)
        assert len(history) == 2
        assert history[0].fired_at > history[1].fired_at

    def test_the_last_outcome_appears_on_the_schedule_listing(self, engine):
        service = build(engine, services=Services(engine=engine))
        asyncio.run(service.run_now("builtin_maintenance"))
        row = next(
            item for item in service.list_schedules() if item.kind == "maintenance"
        )
        assert row.last_outcome == "completed"

    def test_a_run_history_read_for_an_unknown_schedule_is_a_not_found(self, engine):
        service = build(engine, services=Services(engine=engine))
        with pytest.raises(ScheduleNotFound):
            service.runs("nope")

    def test_every_outcome_the_service_can_write_is_accepted_by_the_check(self, engine):
        # A vocabulary the database refuses is a schedule run that vanishes, so each value is
        # written for real rather than compared against a constant.
        service = build(engine, services=Services(engine=engine))
        spec = service.get_spec("builtin_maintenance")
        for index, outcome in enumerate(jobs_def.OUTCOMES):
            service._record(
                spec,
                IST_NOON_TUESDAY + timedelta(seconds=index),
                jobs_def.FireResult(outcome, (), "written by the test"),
            )
        assert len(service.runs("builtin_maintenance", limit=100)) == len(jobs_def.OUTCOMES)


# ---------------------------------------------------------------------------
# What a fire enqueues
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_expiry_discovery_writes_one_task_per_active_underlying(self, engine):
        services = Services(
            engine=engine, token_broker=broker_with_token(), governor=FakeGovernor()
        )
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_expiry_discovery"))
        assert result.outcome == "enqueued"
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT kind, fyers_symbol, request_params_json FROM task ORDER BY seq")
            ).all()
            underlyings = connection.execute(
                text(
                    "SELECT count(*) FROM underlying_registry WHERE is_active = 1"
                    " AND exchange <> 'MCX'"
                )
            ).scalar()
        assert len(rows) == underlyings
        assert {row[0] for row in rows} == {"expiry_dates"}
        params = json.loads(rows[0][2])
        # The measured 366 day ceiling is a hard error, not a truncation, so the window must be
        # inside it before the request is ever built.
        span = date.fromisoformat(params["to_date"]) - date.fromisoformat(params["from_date"])
        assert span.days <= jobs_def.EXPIRY_WINDOW_MAX_DAYS

    def test_no_token_is_ever_written_into_a_task_row(self, engine):
        services = Services(
            engine=engine, token_broker=broker_with_token(), governor=FakeGovernor()
        )
        service = build(engine, services=services)
        asyncio.run(service.run_now("builtin_expiry_discovery"))
        with engine.connect() as connection:
            blobs = connection.execute(
                text("SELECT request_params_json FROM task")
            ).scalars().all()
        for blob in blobs:
            assert "token" not in blob.lower()

    def test_an_enqueued_job_is_immediately_leasable(self, engine):
        services = Services(engine=engine, governor=FakeGovernor())
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_symbol_master"))
        leased = LeaseQueue(engine).lease(owner="w1", limit=5)
        assert [task.kind for task in leased] == ["symbol_master"]
        assert leased[0].job_id == result.job_id

    def test_contract_discovery_only_asks_about_undiscovered_past_expiries(
        self, engine, store, reader
    ):
        register_underlying(engine)
        seed_catalog(store, discovered=False, expiry=date(2025, 3, 27))
        seed_catalog(store, discovered=True, expiry=date(2025, 4, 24))
        services = Services(
            engine=engine,
            token_broker=broker_with_token(),
            governor=FakeGovernor(),
            duck=DuckHolder(reader),
        )
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_contract_discovery"))
        assert result.outcome == "enqueued"
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT kind, expiry_date FROM task ORDER BY seq")
            ).all()
        assert rows == [("underlying_symbols", "2025-03-27")]

    def test_a_fire_that_finds_nothing_to_do_is_completed_and_not_an_error(
        self, engine, store, reader
    ):
        register_underlying(engine)
        seed_catalog(store, discovered=True)
        services = Services(
            engine=engine,
            token_broker=broker_with_token(),
            governor=FakeGovernor(),
            duck=DuckHolder(reader),
        )
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_contract_discovery"))
        assert result.outcome == "completed"
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM job")).scalar() == 0

    def test_seconds_capture_only_offers_expiries_inside_the_window(
        self, engine, store, reader
    ):
        inside = date(2025, 5, 29)
        outside = date(2025, 1, 30)
        register_underlying(engine)
        seed_catalog(store, expiry=inside)
        seed_catalog(store, expiry=outside)
        recorder = RecordingJobService(engine)
        services = Services(
            engine=engine,
            token_broker=broker_with_token(),
            governor=FakeGovernor(),
            duck=DuckHolder(reader),
        )
        service = build(engine, services=services, job_service=recorder)
        result = asyncio.run(service.run_now("builtin_seconds_capture"))
        assert result.outcome == "enqueued"
        request, _kwargs = recorder.requests[0]
        assert inside in request.expiry_dates
        assert outside not in request.expiry_dates
        assert request.resolutions == ["5S"]

    def test_a_missing_holiday_calendar_is_reported_rather_than_silently_narrowing(
        self, engine, store, reader
    ):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM market_holiday"))
        register_underlying(engine)
        seed_catalog(store, expiry=date(2025, 5, 29))
        services = Services(
            engine=engine,
            token_broker=broker_with_token(),
            governor=FakeGovernor(),
            duck=DuckHolder(reader),
        )
        service = build(engine, services=services, job_service=RecordingJobService(engine))
        result = asyncio.run(service.run_now("builtin_seconds_capture"))
        assert result.note is not None and "no holiday rows" in result.note

    def test_a_sweep_refused_on_budget_is_recorded_as_skipped_budget(
        self, engine, store, reader
    ):
        register_underlying(engine)
        seed_catalog(store)
        services = Services(
            engine=engine,
            token_broker=broker_with_token(),
            governor=FakeGovernor(),
            duck=DuckHolder(reader),
        )
        service = build(
            engine,
            services=services,
            job_service=RecordingJobService(engine, refuse="exceeds_budget"),
        )
        result = asyncio.run(service.run_now("builtin_rolling_backfill"))
        assert result.outcome == "skipped_budget"

    def test_a_sweep_is_always_planned_as_a_sweep(self, engine, store, reader):
        register_underlying(engine)
        seed_catalog(store)
        recorder = RecordingJobService(engine)
        services = Services(
            engine=engine,
            token_broker=broker_with_token(),
            governor=FakeGovernor(),
            duck=DuckHolder(reader),
        )
        service = build(engine, services=services, job_service=recorder)
        asyncio.run(service.run_now("builtin_rolling_backfill"))
        _request, kwargs = recorder.requests[0]
        assert kwargs["sweep"] is True
        assert kwargs["schedule_id"] == "builtin_rolling_backfill"
        # A schedule fire never saw a preview, so there is no number to gate the commit on.
        assert _request.confirm_requests is None

    def test_seconds_resolutions_never_reach_the_rolling_backfill(
        self, engine, store, reader
    ):
        register_underlying(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE underlying_registry SET default_resolutions = '[\"5S\",\"1\"]'"
                    " WHERE underlying_id = 1"
                )
            )
        seed_catalog(store)
        recorder = RecordingJobService(engine)
        services = Services(
            engine=engine,
            token_broker=broker_with_token(),
            governor=FakeGovernor(),
            duck=DuckHolder(reader),
        )
        service = build(engine, services=services, job_service=recorder)
        asyncio.run(service.run_now("builtin_rolling_backfill"))
        request, _kwargs = recorder.requests[0]
        assert request.resolutions == ["1"]


# ---------------------------------------------------------------------------
# The internal actions
# ---------------------------------------------------------------------------


class TestInternalActions:
    def test_budget_reset_rolls_the_day_and_releases_deferred_jobs(self, engine):
        governor = FakeGovernor()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job (job_id, kind, status, params_json, created_at)"
                    " VALUES ('old', 'candle_backfill', 'deferred_budget', '{}', '2025-06-01')"
                )
            )
        services = Services(engine=engine, governor=governor)
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_budget_reset"))
        assert result.outcome == "completed"
        assert governor.refreshed == 1
        with engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM job WHERE job_id = 'old'")
            ).scalar()
        assert status == "queued"

    def test_token_health_warns_inside_twenty_four_hours(self, engine):
        broker = broker_with_token(expires_in_seconds=3600)
        services = Services(engine=engine, token_broker=broker, governor=FakeGovernor())
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_token_health"))
        assert result.outcome == "completed"
        with engine.connect() as connection:
            codes = connection.execute(text("SELECT code FROM notification")).scalars().all()
        assert "token_expiring" in codes

    def test_token_health_parks_inside_the_two_minute_margin(self, engine):
        governor = FakeGovernor()
        broker = broker_with_token(expires_in_seconds=30, governor=governor)
        services = Services(engine=engine, token_broker=broker, governor=governor)
        service = build(engine, services=services)
        asyncio.run(service.run_now("builtin_token_health"))
        assert broker.has_valid_token() is False
        assert governor.paused_auth

    def test_token_health_is_quiet_when_the_token_is_healthy(self, engine):
        broker = broker_with_token(expires_in_seconds=200_000)
        services = Services(engine=engine, token_broker=broker, governor=FakeGovernor())
        service = build(engine, services=services)
        asyncio.run(service.run_now("builtin_token_health"))
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM notification")).scalar() == 0

    def test_maintenance_checkpoints_and_reports(self, engine, store, reader):
        class Holder(DuckHolder):
            pass

        holder = Holder(reader)
        services = Services(engine=engine, duck=store, governor=FakeGovernor())
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_maintenance"))
        assert result.outcome == "completed"
        assert result.note is not None and "wal" in result.note

    def test_maintenance_prunes_rate_events_past_the_retention_window(self, engine):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO rate_event (at, kind, endpoint, detail)"
                    " VALUES ('2024-01-01T00:00:00Z', 'throttle_wait', 'history', NULL),"
                    "        ('2025-06-03T00:00:00Z', 'throttle_wait', 'history', NULL)"
                )
            )
        services = Services(engine=engine, governor=FakeGovernor())
        service = build(engine, services=services)
        asyncio.run(service.run_now("builtin_maintenance"))
        with engine.connect() as connection:
            remaining = connection.execute(text("SELECT at FROM rate_event")).scalars().all()
        assert remaining == ["2025-06-03T00:00:00Z"]


# ---------------------------------------------------------------------------
# The 03:00 IST scheduled logout
# ---------------------------------------------------------------------------


def _running_job_with_a_task(engine, job_id: str = "running-job") -> None:
    now = iso_at(utc_now())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (job_id, kind, status, params_json, total_tasks, created_at,"
                " started_at) VALUES (:id, 'candle_backfill', 'running', '{}', 1, :now, :now)"
            ),
            {"id": job_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO task (job_id, seq, kind, state, priority, underlying_id,"
                " contract_id, resolution, range_from, range_to, not_before, created_at)"
                " VALUES (:id, 0, 'candle_chunk', 'pending', 100, 1, 1024, '1',"
                " '2025-01-01', '2025-03-27', :now, :now)"
            ),
            {"id": job_id, "now": now},
        )


class TestScheduledLogout:
    def test_the_logout_clears_the_token_and_parks_the_running_job(self, engine):
        async def scenario():
            broker = broker_with_token()
            governor = FakeGovernor()
            supervisor = PipelineSupervisor(
                engine=engine,
                governor=governor,
                token_broker=broker,
                worker_count=1,
                recover_on_start=False,
                auto_reclaim=False,
            )
            services = Services(
                engine=engine,
                token_broker=broker,
                governor=governor,
                supervisor=supervisor,
            )
            _running_job_with_a_task(engine)
            service = build(engine, services=services)
            result = await service.run_now("builtin_token_logout")
            return broker, supervisor, result

        broker, supervisor, result = asyncio.run(scenario())
        assert result.outcome == "completed"
        # The token is gone, which is the point of a scheduled logout.
        assert broker.record() is None
        assert broker.has_valid_token() is False
        with engine.connect() as connection:
            status, reason = connection.execute(
                text("SELECT status, block_reason FROM job WHERE job_id = 'running-job'")
            ).one()
            states = connection.execute(text("SELECT state FROM task")).scalars().all()
        # Parked, not failed. This is the whole requirement.
        assert status == "blocked_auth"
        assert reason is not None
        assert states == ["pending"]

    def test_the_parked_job_resumes_at_the_same_task_after_a_login(self, engine):
        async def scenario():
            broker = broker_with_token()
            governor = FakeGovernor()
            supervisor = PipelineSupervisor(
                engine=engine,
                governor=governor,
                token_broker=broker,
                worker_count=1,
                recover_on_start=False,
                auto_reclaim=False,
            )
            services = Services(
                engine=engine,
                token_broker=broker,
                governor=governor,
                supervisor=supervisor,
            )
            _running_job_with_a_task(engine)
            service = build(engine, services=services)
            await service.run_now("builtin_token_logout")
            # The morning login. The OAuth callback stores the token and tells the supervisor.
            await broker.store_login(access_token="synthetic.second.token")
            await supervisor.on_login()
            return broker

        broker = asyncio.run(scenario())
        assert broker.has_valid_token() is True
        with engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM job WHERE job_id = 'running-job'")
            ).scalar()
            attempts = connection.execute(text("SELECT attempt FROM task")).scalars().all()
        assert status == "queued"
        # The exact task, not the start of the job, and no retry attempt was burned by the logout.
        assert attempts == [0]

    def test_a_logout_with_nothing_stored_is_quiet(self, engine):
        broker = TokenBroker(credentials=FakeCredentials(), tokens=InMemoryTokenStore())
        services = Services(engine=engine, token_broker=broker, governor=FakeGovernor())
        service = build(engine, services=services)
        result = asyncio.run(service.run_now("builtin_token_logout"))
        assert result.outcome == "completed"
        assert result.note == "no token to clear"

    def test_the_logout_asks_the_user_back_in(self, engine):
        broker = broker_with_token()
        services = Services(engine=engine, token_broker=broker, governor=FakeGovernor())
        service = build(engine, services=services)
        asyncio.run(service.run_now("builtin_token_logout"))
        with engine.connect() as connection:
            codes = connection.execute(text("SELECT code FROM notification")).scalars().all()
        assert "needs_reauth" in codes


# ---------------------------------------------------------------------------
# The known gap: token state is a function of the clock, not of the last sweep
# ---------------------------------------------------------------------------


def _record(state: str, expires_at: datetime | None) -> TokenRecord:
    return TokenRecord(
        token_id="t1",
        credential_id="cred-1",
        generation=1,
        fingerprint="0" * 64,
        issued_at="2025-06-03T00:00:00+00:00",
        access_expires_at=None if expires_at is None else expires_at.isoformat(),
        state=state,
    )


class TestTokenStateOnRead:
    def test_a_token_past_its_expiry_reads_as_expired_with_no_sweep(self):
        past = datetime(2025, 6, 3, 6, 0, tzinfo=UTC)
        record = _record(TokenState.ACTIVE, past)
        # The stored column still says active, because nothing has rewritten it.
        assert record.state == TokenState.ACTIVE
        assert record.effective_state(now=past + timedelta(hours=5)) == TokenState.EXPIRED

    def test_inside_the_parking_margin_it_reads_as_expiring(self):
        expiry = datetime(2025, 6, 3, 6, 0, tzinfo=UTC)
        record = _record(TokenState.ACTIVE, expiry)
        assert record.effective_state(now=expiry - timedelta(seconds=60)) == TokenState.EXPIRING

    def test_a_healthy_token_still_reads_as_active(self):
        expiry = datetime(2025, 6, 3, 6, 0, tzinfo=UTC)
        record = _record(TokenState.ACTIVE, expiry)
        assert record.effective_state(now=expiry - timedelta(hours=4)) == TokenState.ACTIVE

    def test_the_clock_never_walks_a_state_backwards(self):
        expiry = datetime(2025, 6, 3, 6, 0, tzinfo=UTC)
        # A row a rejection put into needs_reauth is a decision, not a timestamp.
        assert (
            effective_token_state(
                TokenState.NEEDS_REAUTH, expiry, now=expiry - timedelta(hours=4)
            )
            == TokenState.NEEDS_REAUTH
        )
        assert (
            effective_token_state(TokenState.REVOKED, expiry, now=expiry - timedelta(hours=4))
            == TokenState.REVOKED
        )
        # And an already expired row is not resurrected by a future looking expiry column.
        assert (
            effective_token_state(
                TokenState.EXPIRED, expiry, now=expiry - timedelta(hours=4)
            )
            == TokenState.EXPIRED
        )

    def test_a_token_with_no_expiry_claim_is_left_alone(self):
        record = _record(TokenState.ACTIVE, None)
        assert record.effective_state(now=datetime(2030, 1, 1, tzinfo=UTC)) == TokenState.ACTIVE

    def test_the_broker_reports_the_evaluated_state(self):
        broker = broker_with_token(expires_in_seconds=-1)
        assert broker.record() is not None
        assert broker.token_state() == TokenState.EXPIRED
        assert broker.has_valid_token() is False

    def test_the_broker_reports_none_when_nothing_is_stored(self):
        broker = TokenBroker(credentials=FakeCredentials(), tokens=InMemoryTokenStore())
        assert broker.token_state() == "none"

    def test_bootstrap_no_longer_reports_active_for_an_expired_token(self, engine, tmp_path):
        from expirymanager import bootstrap as bootstrap_module
        from expirymanager import paths as paths_module

        broker = broker_with_token(expires_in_seconds=-3600)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO broker_credential (credential_id, label, app_id, app_secret_enc,"
                    " key_ver, redirect_uri, plan, is_active, created_at, updated_at)"
                    " VALUES ('cred-1', 'Fyers', 'TESTAPP-100', X'00', 1,"
                    " 'http://127.0.0.1:8000/fyers/callback', 'standard', 1, '2025-01-01',"
                    " '2025-01-01')"
                )
            )
        status = bootstrap_module.read_status(
            engine,
            paths=paths_module.ensure(tmp_path / "root"),
            token_broker=broker,
        )
        assert status.token_state == TokenState.EXPIRED
        assert status.needs_reauth is True


# ---------------------------------------------------------------------------
# Lifespan wiring
# ---------------------------------------------------------------------------


class TestLifespanWiring:
    def test_install_registers_only_the_scheduler_slot(self):
        from expirymanager import lifespan as lifespan_module
        from expirymanager.scheduler import service as service_module

        try:
            service_module.install()
            assert lifespan_module.registered_components() == (lifespan_module.SLOT_SCHEDULER,)
        finally:
            for slot in lifespan_module.COMPONENT_SLOTS:
                lifespan_module.unregister_component(slot)

    def test_the_real_lifespan_starts_and_stops_the_scheduler(self, tmp_path, monkeypatch):
        """The registration point, exercised through a real application startup.

        Nothing is mocked out of the startup path. This is the test that would catch a scheduler
        that only works against the doubles above, or one that leaves its thread running after
        shutdown, which is how a test suite starts hanging on the last test.
        """
        from fastapi.testclient import TestClient

        from expirymanager import lifespan as lifespan_module
        from expirymanager.app import create_app
        from expirymanager.scheduler import service as service_module

        root = tmp_path / "expirymanager-home"
        monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
        try:
            service_module.install()
            application = create_app(root=root, serve_static=False)
            with TestClient(application, base_url="https://127.0.0.1:8000") as client:
                assert client.get("/api/v1/bootstrap").status_code == 200
                scheduler = application.state.services.scheduler
                assert isinstance(scheduler, SchedulerService)
                assert scheduler.started is True
                snapshot = scheduler.snapshot()
                assert snapshot.running is True
                # Every builtin trigger is installed from the table and nothing else exists.
                assert set(snapshot.schedule_ids) == set(jobs_def.BUILTIN_SCHEDULE_IDS)
                assert snapshot.next_fire_at is not None
            assert scheduler.started is False
        finally:
            for slot in lifespan_module.COMPONENT_SLOTS:
                lifespan_module.unregister_component(slot)
            sqlite_module.dispose_engine()

    def test_the_factory_refuses_to_build_without_an_engine(self):
        from expirymanager.scheduler import service as service_module

        class State:
            engine = None

        with pytest.raises(RuntimeError):
            service_module.build_scheduler(State())

    def test_a_trigger_whose_fire_raises_does_not_kill_the_scheduler(self, engine):
        service = build(engine, services=Services(engine=engine))
        # A schedule that vanished between the trigger firing and the fire reading it. The
        # coroutine APScheduler calls must swallow it, because an exception out of a job function
        # is logged and forgotten while an exception out of the trigger loop is a dead scheduler.
        asyncio.run(service._fire_from_trigger("no_such_schedule"))
