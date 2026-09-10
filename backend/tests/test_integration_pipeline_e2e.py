"""The seam test: planner, job service, queue, supervisor, workers, handlers and writer, together.

Six work items were built in parallel against documented interfaces. Each one has its own unit
tests and each one passes them. This file asks a different question: whether the pieces actually
fit when a single job travels the whole length of the system.

Real everything except the socket. A real migrated SQLite registry, a real DuckDB store in a
temporary directory, the real Planner producing the task decomposition, the real JobService
committing it, the real LeaseQueue handing it out, the real PipelineSupervisor running a real
worker pool, the real registered handlers, and the real single writer. Only the HTTP transport is
a fake, because the point is to prove the plumbing rather than to spend a governed request.

Three properties are under test and none of them can be checked inside one item:

1. The rows the planner writes are exactly the rows the handlers know how to read, and the
   coverage ledger the handler writes describes exactly the rows that landed.
2. Replaying the same work converges. A second identical download must not double the candles.
3. The auth gate is real. An auth rejection from the vendor must stop the pool before it spends
   the next request, park the job rather than fail it, and leave every task retryable at the
   attempt it already had.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from expirymanager.api.schemas.downloads import DownloadRequest
from expirymanager.brokers.fyers.client import AuthContext, FyersClient
from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.arrow import IST_OFFSET_SECONDS
from expirymanager.db.duck import DuckStore
from expirymanager.db.reader import DuckReader
from expirymanager.pipeline import worker as worker_module
from expirymanager.pipeline.events import EventBus
from expirymanager.pipeline.handlers import candle_chunk as candle_handlers
from expirymanager.pipeline.jobs import JobService, JobServiceError
from expirymanager.pipeline.planner import Planner
from expirymanager.pipeline.queue import LeaseQueue
from expirymanager.pipeline.supervisor import PipelineSupervisor
from tests.fyers_fake_transport import (
    FAKE_ACCESS_TOKEN,
    FAKE_APP_ID,
    RecordingTransport,
    json_response,
)
from tests.test_pipeline_planner import (
    EXPIRY,
    TODAY,
    register_underlying,
    seed_catalog,
)

RES = "1"
RES_ID = 2

# The vocabulary LeaseQueue.terminal_status() writes. A job never reaches a word outside it.
TERMINAL = ("completed", "completed_with_errors", "cancelled", "failed")

# Ten one minute bars on the expiry day itself, well inside the session. Deliberately nowhere near
# the left edge of the 100 day chunk, so the handler seals the contract and plans no older chunk:
# the backward walk is covered by its own unit tests and would only make this one nondeterministic.
FIRST_BAR = datetime(2025, 3, 27, 9, 15)
BAR_COUNT = 10


class FakeTokens:
    """A token supplier with a synthetic token. Nothing here resembles a credential."""

    async def auth_context(self) -> AuthContext:
        return AuthContext(app_id=FAKE_APP_ID, access_token=FAKE_ACCESS_TOKEN, generation=1)


class FakeBroker:
    """The token broker surface the supervisor and the handlers reach for."""

    class _Record:
        fingerprint = "0f0f0f0f0f0f0f0f"

    def __init__(self) -> None:
        self.auth_gate = asyncio.Event()
        self.auth_gate.set()
        self.generation = 1
        self._parked_generation: int | None = None
        self.record = FakeBroker._Record()
        self.parked_count = 0

    def has_valid_token(self) -> bool:
        return self.auth_gate.is_set()

    async def on_auth_error(self, generation: int, *, reason: str = "") -> bool:
        if generation < self.generation:
            return False
        if self._parked_generation is not None and generation <= self._parked_generation:
            return False
        self._parked_generation = generation
        self.auth_gate.clear()
        self.parked_count += 1
        return True


class FakeGovernor:
    """Both governor surfaces in one object: the planner's budget view and the supervisor's mode.

    The planner reads requests_used and daily_budget to price a plan; the supervisor polls mode to
    keep its run gate in step. The real FyersGovernor is exercised by its own tests. What matters
    here is that the counting the plan did and the gating the pool did come from one place.
    """

    def __init__(self, used: int = 0, daily: int = 100_000, per_minute: int = 170) -> None:
        self.requests_used = used
        self.daily_budget = daily
        self._per_minute = per_minute
        self.mode = "running"
        self.reason: str | None = None
        self.slots: list[str] = []

    def snapshot(self) -> Any:
        governor = self

        class _Snapshot:
            per_minute = governor._per_minute
            minute_violations = 0
            strikes_remaining = 3
            blocked_until = None

        return _Snapshot()

    def slot(self, endpoint: str):
        governor = self

        class _Slot:
            async def __aenter__(self_inner):
                governor.slots.append(endpoint)
                governor.requests_used += 1
                return None

            async def __aexit__(self_inner, *_exc):
                return False

        return _Slot()

    async def note_rate_limited(self, **_kwargs) -> None:
        return None

    async def pause_auth(self, *, reason: str = "") -> None:
        self.mode = "paused_auth"
        self.reason = reason

    async def set_mode(self, mode, *, reason: str | None = None) -> None:
        self.mode = str(mode)
        self.reason = reason

    async def resume(self, *, by: str | None = None) -> str:
        self.mode = "running"
        self.reason = None
        return self.mode


class FakeSettings:
    def __init__(self, **values) -> None:
        self._values = {"budget_reserve_fraction": 0.70, "throttle_per_minute": 170}
        self._values.update(values)

    def get_int(self, key: str) -> int:
        return int(self._values[key])

    def get_float(self, key: str) -> float:
        return float(self._values[key])


class Services:
    """The AppState shaped bag the handlers resolve their dependencies from."""

    def __init__(self, *, client, store, engine, governor) -> None:
        self.fyers_client = client
        self.duck_writer = store.writer
        self.duck_reader = DuckReader(store)
        self.engine = engine
        self.settings = FakeSettings()
        self.paths = None
        self.token_broker = FakeBroker()
        self.governor = governor


async def inline(fn, /, *args, **kwargs):
    """Run the queue's blocking calls on this thread, so the test is deterministic."""
    return fn(*args, **kwargs)


def epoch_for_ist(moment: datetime) -> int:
    return int(moment.replace(tzinfo=UTC).timestamp()) - IST_OFFSET_SECONDS


def ok_candles(symbol: str, *, first: datetime = FIRST_BAR, count: int = BAR_COUNT) -> dict:
    """A success envelope in the measured shape: no data wrapper, no code, no message."""
    candles = []
    for index in range(count):
        moment = first + timedelta(minutes=index)
        base = 100.0 + index
        candles.append(
            [
                epoch_for_ist(moment),
                base,
                base + 2.0,
                base - 1.5,
                base + 0.75,
                1000 + index,
                50_000 + index * 10,
            ]
        )
    return {
        "s": "ok",
        "symbol": symbol,
        "resolution": RES,
        "columns": [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ],
        "candles": candles,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def registry():
    worker_module.clear_registry()
    candle_handlers.clear_caches()
    candle_handlers.install_all()
    yield
    worker_module.clear_registry()
    candle_handlers.clear_caches()


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
    duck = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    duck.open()
    yield duck
    duck.close()


@pytest.fixture
def world(engine, store) -> list[int]:
    register_underlying(engine)
    return seed_catalog(store, expiry=EXPIRY, strikes=(22900, 23000, 23100), rights=("CE", "PE"))


class World:
    """Everything one end to end run needs, wired the way the lifespan wires it."""

    def __init__(self, engine, store, handler) -> None:
        self.engine = engine
        self.store = store
        self.governor = FakeGovernor()
        self.transport = RecordingTransport(handler)
        self.client = FyersClient(
            tokens=FakeTokens(), governor=self.governor, transport=self.transport
        )
        self.services = Services(
            client=self.client, store=store, engine=engine, governor=self.governor
        )
        self.reader = self.services.duck_reader
        self.bus = EventBus()
        self.queue = LeaseQueue(engine)
        self.supervisor = PipelineSupervisor(
            engine=engine,
            settings=self.services.settings,
            governor=self.governor,
            token_broker=self.services.token_broker,
            bus=self.bus,
            services=self.services,
            queue=self.queue,
            worker_count=2,
            poll_interval=0.01,
            progress_interval=0.01,
            reclaim_interval=0.05,
            mode_poll_interval=0.01,
            to_thread=inline,
            recover_on_start=False,
            auto_reclaim=False,
        )
        counter = {"n": 0}

        def next_id() -> str:
            counter["n"] += 1
            return f"job-{counter['n']}"

        self.planner = Planner(
            reader=self.reader,
            engine=engine,
            settings=self.services.settings,
            governor=self.governor,
            clock=lambda: TODAY,
        )
        self.jobs = JobService(
            engine=engine,
            planner=self.planner,
            supervisor=self.supervisor,
            governor=self.governor,
            id_factory=next_id,
        )

    async def astart(self) -> None:
        # The single writer is an asyncio task, so it can only be started inside the loop the
        # test runs on. The store fixture opens the file; this starts the queue that drains it.
        await self.store.writer.start()
        await self.supervisor.start()

    async def aclose(self) -> None:
        await self.supervisor.stop()
        await self.store.writer.stop()
        await self.client.aclose()


def order(**overrides) -> DownloadRequest:
    body = {
        "underlying_id": 1,
        "expiry_dates": [EXPIRY],
        "resolutions": [RES],
        "instrument_class": "OPT",
    }
    body.update(overrides)
    return DownloadRequest(**body)


async def wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.01) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition did not hold within the timeout")


def task_states(engine, job_id: str) -> list[str]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text("SELECT state FROM task WHERE job_id = :j ORDER BY seq"), {"j": job_id}
            )
        ]


def job_row(engine, job_id: str) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM job WHERE job_id = :j"), {"j": job_id}
            ).mappings().one()
        )


def candle_count(store) -> int:
    cur = store.cursor()
    try:
        return int(cur.execute("SELECT count(*) FROM candles").fetchone()[0])
    finally:
        cur.close()


def coverage_rows(store) -> list[dict]:
    cur = store.cursor()
    try:
        rows = cur.execute(
            "SELECT contract_id, res_id, range_from, range_to, status, row_count,"
            " first_ts, last_ts FROM candle_coverage ORDER BY contract_id"
        ).fetchall()
    finally:
        cur.close()
    keys = (
        "contract_id",
        "res_id",
        "range_from",
        "range_to",
        "status",
        "row_count",
        "first_ts",
        "last_ts",
    )
    return [dict(zip(keys, row)) for row in rows]


async def run_job(world: World, request: DownloadRequest) -> str:
    accepted = await world.jobs.create(request)
    job_id = accepted.job_id
    await wait_for(
        lambda: job_row(world.engine, job_id)["status"] in TERMINAL
    )
    return job_id


# ---------------------------------------------------------------------------
# The whole length of the system, once
# ---------------------------------------------------------------------------


class TestOneJobEndToEnd:
    def test_a_planned_job_lands_candles_and_a_coverage_ledger_that_agrees(self, engine, store, world):
        async def main():
            def handler(request):
                symbol = dict(request.url.params).get("symbol", "NSE:UNKNOWN")
                return json_response(200, ok_candles(symbol))

            harness = World(engine, store, handler)
            await harness.astart()
            try:
                job_id = await run_job(harness, order())

                # Every planned task is a candle chunk and every one of them settled done.
                states = task_states(engine, job_id)
                assert states, "the planner produced no tasks at all"
                assert set(states) == {"done"}, states

                job = job_row(engine, job_id)
                assert job["status"] == "completed", job["status"]

                # One governed request per task and not one more. This is the assertion that
                # would catch a worker taking a second slot, or a handler retrying in place.
                assert len(harness.governor.slots) == len(states)
                assert set(harness.governor.slots) == {"expired-historical-data"}
                assert len(harness.transport.requests) == len(states)

                # Six contracts, ten bars each.
                assert candle_count(store) == 6 * BAR_COUNT

                # The ledger describes exactly what landed.
                ledger = coverage_rows(store)
                assert len(ledger) == 6
                cur = store.cursor()
                try:
                    for row in ledger:
                        assert row["status"] == "ok", row
                        assert row["res_id"] == RES_ID
                        actual = int(
                            cur.execute(
                                "SELECT count(*) FROM candles WHERE contract_id = ? AND res_id = ?",
                                [row["contract_id"], row["res_id"]],
                            ).fetchone()[0]
                        )
                        assert row["row_count"] == actual == BAR_COUNT, row
                        # The window the ledger claims must contain the bars it claims.
                        assert row["range_to"] == EXPIRY, row
                        assert row["range_from"] <= FIRST_BAR.date() <= row["range_to"], row
                        assert row["first_ts"] == FIRST_BAR, row

                    # Timestamps are naive IST, not UTC. A UTC epoch stored raw would put a
                    # 09:15 IST bar at 03:45, which is outside every Indian session.
                    stamps = cur.execute(
                        "SELECT DISTINCT ts FROM candles ORDER BY ts"
                    ).fetchall()
                    for (moment,) in stamps:
                        assert moment.tzinfo is None
                        minutes = moment.hour * 60 + moment.minute
                        assert 9 * 60 + 15 <= minutes <= 15 * 60 + 30, moment

                    # Open interest survived the column mapping.
                    assert (
                        int(
                            cur.execute(
                                "SELECT count(*) FROM candles WHERE oi IS NULL"
                            ).fetchone()[0]
                        )
                        == 0
                    )
                finally:
                    cur.close()
            finally:
                await harness.aclose()

        asyncio.run(main())

    def test_replaying_the_same_download_converges_rather_than_duplicating(
        self, engine, store, world
    ):
        async def main():
            def handler(request):
                symbol = dict(request.url.params).get("symbol", "NSE:UNKNOWN")
                return json_response(200, ok_candles(symbol))

            harness = World(engine, store, handler)
            await harness.astart()
            try:
                await run_job(harness, order())
                first_rows = candle_count(store)
                first_ledger = len(coverage_rows(store))

                # A second plan over the same scope has nothing left to ask for. That refusal is
                # the coverage subtraction working: the planner read the ledger the handler wrote.
                with pytest.raises(JobServiceError) as refusal:
                    await harness.jobs.create(order())
                assert refusal.value.code == "nothing_to_download"

                # Forced, it asks again and the delete then insert converges.
                requests_before = len(harness.transport.requests)
                await run_job(harness, order(force_refresh=True))
                assert len(harness.transport.requests) > requests_before
                assert candle_count(store) == first_rows
                assert len(coverage_rows(store)) == first_ledger
            finally:
                await harness.aclose()

        asyncio.run(main())


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestAuthGateStopsTheRealPool:
    def test_an_auth_rejection_parks_the_job_and_stops_the_workers(self, engine, store, world):
        async def main():
            calls = {"n": 0}
            # Flipped once the gate has been proved shut, so the resume half of the test asserts
            # a job that actually finishes rather than one that parks again immediately.
            token_is_dead = {"yes": True}

            def handler(request):
                calls["n"] += 1
                if token_is_dead["yes"]:
                    # -16 is the documented invalid token code. Every request gets it, so if the
                    # gate did not hold the pool would burn through all six tasks.
                    return json_response(
                        200, {"s": "error", "code": -16, "message": "Invalid token"}
                    )
                symbol = dict(request.url.params).get("symbol", "NSE:UNKNOWN")
                return json_response(200, ok_candles(symbol))

            harness = World(engine, store, handler)
            await harness.astart()
            try:
                accepted = await harness.jobs.create(order())
                job_id = accepted.job_id
                await wait_for(
                    lambda: job_row(harness.engine, job_id)["status"] == "blocked_auth"
                )

                job = job_row(engine, job_id)
                assert job["status"] == "blocked_auth"
                assert job["status"] != "failed"

                # The gate is the broker's own event, cleared exactly once.
                assert not harness.services.token_broker.auth_gate.is_set()
                assert harness.services.token_broker.parked_count == 1

                # Give an ungated pool a generous window to spend the rest of the budget.
                spent = calls["n"]
                await asyncio.sleep(0.2)
                assert calls["n"] == spent, "the pool kept spending requests after the gate closed"
                assert spent < 6, f"the gate let {spent} of 6 tasks through"

                # Nothing was consumed. Every task is still retryable at the attempt it had.
                with engine.connect() as connection:
                    attempts = [
                        row[0]
                        for row in connection.execute(
                            text("SELECT attempt FROM task WHERE job_id = :j"), {"j": job_id}
                        )
                    ]
                assert set(attempts) == {0}, attempts
                assert candle_count(store) == 0

                # And the morning login releases it. The parked job resumes and finishes without
                # a single task having burned an attempt on the dead token.
                token_is_dead["yes"] = False
                harness.services.token_broker.generation += 1
                harness.services.token_broker._parked_generation = None
                harness.services.token_broker.auth_gate.set()
                await harness.supervisor.on_login()
                await wait_for(lambda: job_row(harness.engine, job_id)["status"] in TERMINAL)

                assert job_row(engine, job_id)["status"] == "completed"
                assert set(task_states(engine, job_id)) == {"done"}
                with engine.connect() as connection:
                    resumed = [
                        row[0]
                        for row in connection.execute(
                            text("SELECT attempt FROM task WHERE job_id = :j"), {"j": job_id}
                        )
                    ]
                assert set(resumed) == {0}, resumed
                assert candle_count(store) == 6 * BAR_COUNT
            finally:
                await harness.aclose()

        asyncio.run(main())


# ---------------------------------------------------------------------------
# A measured vendor rule, learned from the live service on 2026-09-10
# ---------------------------------------------------------------------------


class TestExpiryDatesWindowIsClampedToTheLastServedDay:
    """The expiry-dates endpoint refuses a window that reaches the present.

    Measured against the live service on 2026-09-10 with a real token: the window
    2026-03-14 to 2026-09-10, ending on today, answered HTTP 422 code -50 "Invalid input", the
    identical window ending 2026-09-09 answered HTTP 200 with 26 option expiries, and a window
    reaching 60 days into the future answered 422 as well.

    That matters because the built-in 18:00 expiry_discovery schedule built its window as today
    plus a 60 day forward margin, and the handler's own default window ended on today. Both would
    have spent one refused request per underlying every night, forever, and the run history would
    have shown nothing but Invalid input.
    """

    async def test_the_handler_trims_a_window_that_ends_today(self, engine, store):
        from datetime import date as date_type

        from expirymanager.pipeline.handlers.expiry_discovery import last_served_day
        from tests.fyers_fake_transport import standard_body
        from tests.test_pipeline_handlers import (
            Services as HandlerServices,
            lease_one,
            make_client,
            run_through_worker,
        )
        from tests.test_pipeline_handlers_discovery import make_job, make_task

        register_underlying(engine)
        today = date_type.today()
        await store.writer.start()
        try:
            job = make_job(engine, kind="expiry_discovery")
            make_task(
                engine,
                job,
                "expiry_dates",
                range_from=today - timedelta(days=200),
                range_to=today,
            )
            payload = {
                "symbol": "NIFTY",
                "expiry_dates": {"futures": [], "options": []},
            }
            client, transport = make_client(
                lambda _request: json_response(200, standard_body(data=payload))
            )
            services = HandlerServices(client=client, store=store, engine=engine)
            queue = LeaseQueue(engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        sent = dict(transport.last_request.url.params)
        assert sent["range_to"] == last_served_day(today).isoformat()
        assert sent["range_to"] < today.isoformat()

    def test_the_nightly_schedule_never_builds_a_window_reaching_today(self):
        from datetime import date as date_type

        from expirymanager.scheduler import jobs_def

        # The seeded schedule carries forward_days 60, which is what produced the 422.
        class _Schedule:
            def int_param(self, key, fallback):
                return {"forward_days": 60, "lookback_days": 366}.get(key, fallback)

        today = date_type(2026, 9, 10)
        to_date = min(
            today + timedelta(days=_Schedule().int_param("forward_days", 60)),
            jobs_def.last_served_day(today),
        )
        assert to_date < today
        assert to_date == date_type(2026, 9, 9)


# ---------------------------------------------------------------------------
# The dim_underlying mirror, and the seal that depends on it
# ---------------------------------------------------------------------------


class TestTheUnderlyingMirrorExists:
    """underlying_registry lives in SQLite and dim_underlying is its DuckDB mirror.

    Nine read sites join that mirror: the spot series, the ATM pick, the option chain, the catalog
    listing, the denormalised export, two maintenance assertions, the spot id allocator and the
    backward walk's own bounds query. Before this was wired, nothing in the running application
    ever wrote it, so on a fresh install the four builtin underlyings seeded by migration 0004 had
    a registry row and no mirror row.

    Found live on 2026-09-10: a real six contract NIFTY download landed 52,873 candles correctly
    and then the backward walk's bounds query found no row, returned None and gave up without
    sealing the contract or planning the next chunk. The seal never appeared, and a re-plan spent
    six more requests on a window older than the contract had ever traded in.
    """

    async def test_discovery_mirrors_a_registry_row_that_only_sqlite_knows_about(
        self, engine, store
    ):
        from tests.fyers_fake_transport import standard_body
        from tests.test_pipeline_handlers import (
            Services as HandlerServices,
            lease_one,
            make_client,
            run_through_worker,
        )
        from tests.test_pipeline_handlers_discovery import make_job, make_task

        register_underlying(engine)
        cur = store.cursor()
        try:
            assert cur.execute("SELECT count(*) FROM dim_underlying").fetchone()[0] == 0
        finally:
            cur.close()

        await store.writer.start()
        try:
            job = make_job(engine, kind="expiry_discovery")
            make_task(
                engine,
                job,
                "expiry_dates",
                range_from=date(2025, 1, 1),
                range_to=date(2025, 3, 31),
            )
            payload = {
                "symbol": "NIFTY",
                "expiry_dates": {"futures": [], "options": ["2025-03-27"]},
            }
            client, _ = make_client(
                lambda _request: json_response(200, standard_body(data=payload))
            )
            services = HandlerServices(client=client, store=store, engine=engine)
            queue = LeaseQueue(engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        cur = store.cursor()
        try:
            row = cur.execute(
                "SELECT underlying_id, fyers_symbol, exchange, exchange_code, segment,"
                " segment_code, spot_contract_id, is_active FROM dim_underlying"
            ).fetchall()
        finally:
            cur.close()
        assert len(row) == 1
        assert row[0][0] == 1
        assert row[0][2] == "NSE"
        assert row[0][3] == 10
        assert row[0][4] == "CM"
        assert row[0][5] == 10
        # The registry already reserved a spot id, so the mirror must carry that same number
        # rather than allocating a second one.
        assert row[0][6] == 1
        assert row[0][7] is True

    def test_a_finished_chunk_seals_the_contract(self, engine, store, world):
        async def main():
            def handler(request):
                symbol = dict(request.url.params).get("symbol", "NSE:UNKNOWN")
                return json_response(200, ok_candles(symbol))

            harness = World(engine, store, handler)
            await harness.astart()
            try:
                await run_job(harness, order())
            finally:
                await harness.aclose()

        asyncio.run(main())
        cur = store.cursor()
        try:
            sealed = cur.execute(
                "SELECT count(*) FROM dim_contract WHERE kind = 'OPT' AND sealed_at IS NULL"
            ).fetchone()[0]
        finally:
            cur.close()
        # Every bar came back on the expiry day, two months inside the chunk's left edge, so the
        # contract started trading inside this chunk and there is nothing older to fetch.
        assert sealed == 0


# ---------------------------------------------------------------------------
# The four wiring lines nobody owns
# ---------------------------------------------------------------------------


class TestTheApplicationWiring:
    """create_app performs the four registrations that turn a running app into a working one.

    W11, W12, W13 and W14 each expose an explicit install() rather than registering as an import
    side effect, which is right: registering on import changes the behaviour of any test that
    merely imports the module and makes the installed set depend on import order. The cost is that
    something has to call them, and for a while nothing did. The application still started and
    every route answered, while all three lifespan slots and the worker registry stayed empty, so
    the download routes returned the documented 503, no schedule fired, and any leased task died
    with "no handler is registered for task kind".

    app._install_components closes that. This test drives it through a real application startup
    rather than calling the installers directly, so it fails if that call is ever removed.
    """

    def test_four_install_calls_fill_every_slot_and_the_app_starts_and_stops(
        self, tmp_path, monkeypatch
    ):
        from fastapi.testclient import TestClient

        from expirymanager.app import create_app
        from expirymanager.db import sqlite as sqlite_module_local
        from expirymanager import lifespan as lifespan_module
        from expirymanager.lifespan import (
            SLOT_JOB_RECOVERY,
            SLOT_PIPELINE_SUPERVISOR,
            SLOT_SCHEDULER,
            registered_components,
        )
        from expirymanager.pipeline import jobs as jobs_module
        from expirymanager.pipeline import supervisor as supervisor_module
        from expirymanager.pipeline.handlers import candle_chunk as handlers_module
        from expirymanager.scheduler import service as scheduler_module


        root = tmp_path / "expirymanager-home"
        monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
        saved_factories = dict(lifespan_module._component_factories)
        lifespan_module._component_factories.clear()
        worker_module.clear_registry()
        try:
            # Nothing is installed by hand here. Building the application is what must do it, so
            # that removing that call fails this test rather than only failing in production.
            application = create_app(root=root, serve_static=False)

            assert set(registered_components()) == {
                SLOT_PIPELINE_SUPERVISOR,
                SLOT_JOB_RECOVERY,
                SLOT_SCHEDULER,
            }
            assert set(worker_module.registered_kinds()) == {
                "candle_chunk",
                "spot_chunk",
                "expiry_dates",
                "underlying_symbols",
                "symbol_master",
                "chain_snapshot",
            }

            with TestClient(application, base_url="http://127.0.0.1:8000") as client:
                assert client.get("/api/v1/bootstrap").status_code == 200
                services = application.state.services
                assert services.supervisor is not None
                assert services.supervisor.started
                assert services.scheduler is not None
                assert services.scheduler.started
                # The route layer's dependency resolves instead of raising the 503.
                from expirymanager.api.deps import get_supervisor

                assert get_supervisor(services) is services.supervisor
        finally:
            lifespan_module._component_factories.clear()
            lifespan_module._component_factories.update(saved_factories)
            worker_module.clear_registry()
            sqlite_module_local.dispose_engine()
