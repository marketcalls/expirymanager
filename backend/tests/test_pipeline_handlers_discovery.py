"""The discovery, symbol master, chain snapshot and export handlers.

Same rules as the candle tests: a fake transport, synthetic credentials, real databases. The
assertions that matter here are the ones about what the level below inherits. A discovery that
writes the contracts and then stops leaves a job whose preview promised candles it never fetched,
so the cascade into candle_chunk tasks is tested by reading the task table rather than by reading
a return value.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from expirymanager.brokers.fyers.client import AuthContext, FyersClient
from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.duck import DuckStore
from expirymanager.db.exports import ExportScope, ExportSpec
from expirymanager.db.reader import DuckReader
from expirymanager.pipeline import worker as worker_module
from expirymanager.pipeline.events import EVENT_EXPORT_READY, EventBus
from expirymanager.pipeline.handlers import candle_chunk as candle_module
from expirymanager.pipeline.handlers import chain_snapshot as chain_module
from expirymanager.pipeline.handlers import contract_discovery as contract_module
from expirymanager.pipeline.handlers import expiry_discovery as expiry_module
from expirymanager.pipeline.handlers import export as export_module
from expirymanager.pipeline.handlers import symbol_master as master_handler
from expirymanager.pipeline.queue import LeaseQueue, iso_at
from expirymanager.pipeline.worker import HandlerContext
from tests.fyers_fake_transport import (
    FAKE_ACCESS_TOKEN,
    FAKE_APP_ID,
    RecordingTransport,
    json_response,
    standard_body,
)
from tests.test_pipeline_handlers import (
    FakeBroker,
    FakeDispatcher,
    FakeSupervisor,
    Services,
    inline,
    read_task,
    run_through_worker,
)
from tests.test_pipeline_planner import register_underlying, seed_catalog

EXPIRY = date(2025, 3, 27)
NOW = datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC)
NIFTY = "NSE:NIFTY50-INDEX"


class FakeTokens:
    async def auth_context(self) -> AuthContext:
        return AuthContext(app_id=FAKE_APP_ID, access_token=FAKE_ACCESS_TOKEN, generation=1)


@pytest.fixture(autouse=True)
def registry():
    candle_module.clear_caches()
    worker_module.clear_registry()
    candle_module.install_all()
    yield
    worker_module.clear_registry()
    candle_module.clear_caches()


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
def queue(engine) -> LeaseQueue:
    return LeaseQueue(engine)


def make_client(handler) -> tuple[FyersClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    return FyersClient(tokens=FakeTokens(), transport=transport), transport


def answer(body: dict, status_code: int = 200):
    return lambda _request: json_response(status_code, body)


def make_job(engine, job_id: str = "job-1", *, kind: str = "candle_backfill", params=None) -> str:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (job_id, kind, status, params_json, priority, created_at)"
                " VALUES (:job_id, :kind, 'running', :params, 100, :now)"
            ),
            {
                "job_id": job_id,
                "kind": kind,
                "params": json.dumps(params if params is not None else {}),
                "now": iso_at(NOW),
            },
        )
    return job_id


def make_task(
    engine,
    job_id: str,
    kind: str,
    *,
    seq: int = 0,
    symbol: str = NIFTY,
    expiry: date | None = None,
    range_from: date | None = None,
    range_to: date | None = None,
    params: dict | None = None,
) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO task (job_id, seq, kind, state, priority, underlying_id,"
                    " fyers_symbol, expiry_date, range_from, range_to, include_oi,"
                    " request_params_json, not_before, created_at)"
                    " VALUES (:job_id, :seq, :kind, 'pending', 100, 1, :symbol, :expiry,"
                    " :range_from, :range_to, 1, :params, :now, :now) RETURNING task_id"
                ),
                {
                    "job_id": job_id,
                    "seq": seq,
                    "kind": kind,
                    "symbol": symbol,
                    "expiry": expiry.isoformat() if expiry else None,
                    "range_from": range_from.isoformat() if range_from else None,
                    "range_to": range_to.isoformat() if range_to else None,
                    "params": json.dumps(params) if params else None,
                    "now": iso_at(NOW),
                },
            ).scalar_one()
        )


def lease_one(queue: LeaseQueue):
    [task] = queue.lease(owner="worker-test", limit=1)
    return task


def duck_rows(store, sql: str, params=None):
    cur = store.cursor()
    try:
        return cur.execute(sql, list(params or [])).fetchall()
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# expiry_dates
# ---------------------------------------------------------------------------


EXPIRY_PAYLOAD = {
    "symbol": "NIFTY",
    "from_date": "2025-01-01",
    "to_date": "2025-03-31",
    "expiry_dates": {
        "futures": ["2025-01-30", "2025-02-27", "2025-03-27"],
        "options": ["2025-01-30", "2025-02-27", "2025-03-20", "2025-03-27"],
    },
}


class TestExpiryDiscovery:
    async def test_it_writes_every_returned_expiry_with_the_cycle_it_can_derive(
        self, engine, store, queue
    ):
        register_underlying(engine)
        await store.writer.start()
        try:
            job = make_job(engine, kind="expiry_discovery")
            task_id = make_task(
                engine,
                job,
                "expiry_dates",
                range_from=date(2025, 1, 1),
                range_to=date(2025, 3, 31),
            )
            client, transport = make_client(answer(standard_body(data=EXPIRY_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        rows = duck_rows(
            store,
            "SELECT expiry_date, has_futures, has_options, expiry_cycle_derived,"
            " is_last_of_month, source_range_from, source_range_to, discovered_task_id"
            " FROM dim_expiry WHERE underlying_id = 1 ORDER BY expiry_date",
        )
        assert [row[0] for row in rows] == [
            date(2025, 1, 30),
            date(2025, 2, 27),
            date(2025, 3, 20),
            date(2025, 3, 27),
        ]
        # 2025-03-20 is a weekly: it is not the last expiry in its month.
        weekly = next(row for row in rows if row[0] == date(2025, 3, 20))
        assert (weekly[1], weekly[2], weekly[3], weekly[4]) == (False, True, "W", False)
        monthly = next(row for row in rows if row[0] == date(2025, 3, 27))
        assert (monthly[1], monthly[2], monthly[3], monthly[4]) == (True, True, "M", True)
        assert monthly[5] == date(2025, 1, 1)
        assert monthly[6] == date(2025, 3, 31)
        assert monthly[7] == task_id

        row = read_task(engine, task_id)
        assert (row["state"], row["row_count"]) == ("done", 4)
        query = dict(transport.last_request.url.params)
        assert query["date_format"] == "1"
        assert query["range_from"] == "2025-01-01"

    async def test_it_records_the_authoritative_root_echo(self, engine, store, queue):
        register_underlying(engine)
        await store.writer.start()
        try:
            job = make_job(engine, kind="expiry_discovery")
            make_task(engine, job, "expiry_dates")
            client, _ = make_client(answer(standard_body(data=EXPIRY_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT resolved_root_echo, resolved_at FROM underlying_registry"
                    " WHERE underlying_id = 1"
                )
            ).one()
        assert row[0] == "NIFTY"
        assert row[1]

    async def test_a_window_wider_than_366_days_is_refused_without_a_request(
        self, engine, store, queue
    ):
        """The endpoint errors rather than truncating, so the guard has to be local."""
        register_underlying(engine)
        await store.writer.start()
        try:
            job = make_job(engine, kind="expiry_discovery")
            task_id = make_task(
                engine,
                job,
                "expiry_dates",
                range_from=date(2024, 1, 1),
                range_to=date(2025, 3, 31),
            )
            client, transport = make_client(answer(standard_body(data=EXPIRY_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert row["state"] == "failed"
        assert "366" in row["last_error_text"]
        assert transport.requests == []

    async def test_a_discovery_that_finds_nothing_is_empty_rather_than_failed(
        self, engine, store, queue
    ):
        register_underlying(engine)
        await store.writer.start()
        try:
            job = make_job(engine, kind="expiry_discovery")
            task_id = make_task(engine, job, "expiry_dates")
            payload = {"symbol": "NIFTY", "expiry_dates": {"futures": [], "options": []}}
            client, _ = make_client(answer(standard_body(data=payload)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        assert read_task(engine, task_id)["state"] == "empty"


# ---------------------------------------------------------------------------
# underlying_symbols
# ---------------------------------------------------------------------------


CONTRACTS_PAYLOAD = {
    "symbol": "NIFTY",
    "expiry_date": EXPIRY.isoformat(),
    "contracts": {
        "futures": ["NSE:NIFTY25MARFUT"],
        "options": [
            "NSE:NIFTY25MAR23000CE",
            "NSE:NIFTY25MAR23000PE",
            "NSE:NIFTY25MAR23100CE",
            "NSE:NIFTY25MAR23100PE",
        ],
    },
}


class TestContractDiscovery:
    async def test_it_writes_the_chain_and_allocates_a_contiguous_id_block(
        self, engine, store, queue
    ):
        register_underlying(engine)
        await store.writer.start()
        try:
            job = make_job(engine, kind="contract_discovery")
            task_id = make_task(engine, job, "underlying_symbols", expiry=EXPIRY)
            client, transport = make_client(answer(standard_body(data=CONTRACTS_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        rows = duck_rows(
            store,
            "SELECT contract_id, fyers_symbol, kind, option_type, CAST(strike AS DOUBLE),"
            " expiry_date, source_array, source_endpoint, discovered_task_id"
            " FROM dim_contract WHERE underlying_id = 1 AND kind <> 'SPOT'"
            " ORDER BY contract_id",
        )
        assert len(rows) == 5
        # Futures first, then strike ascending with CE before PE, and ids are contiguous.
        assert [row[1] for row in rows] == [
            "NSE:NIFTY25MARFUT",
            "NSE:NIFTY25MAR23000CE",
            "NSE:NIFTY25MAR23000PE",
            "NSE:NIFTY25MAR23100CE",
            "NSE:NIFTY25MAR23100PE",
        ]
        ids = [row[0] for row in rows]
        assert ids == list(range(ids[0], ids[0] + 5))
        assert rows[0][6] == "futures"
        assert rows[1][6] == "options"
        assert rows[0][7] == "underlying-symbols"
        assert rows[0][8] == task_id
        # The requested expiry is authoritative and is written on every row.
        assert {row[5] for row in rows} == {EXPIRY}

        assert read_task(engine, task_id)["state"] == "done"
        assert read_task(engine, task_id)["row_count"] == 5
        assert dict(transport.last_request.url.params)["expiry_date"] == EXPIRY.isoformat()

    async def test_it_enqueues_the_candle_chunks_the_preview_priced(
        self, engine, store, queue
    ):
        """A discovery that writes contracts and stops leaves a job that fetches nothing."""
        register_underlying(engine)
        sheet = {
            "underlying_id": 1,
            "expiry_dates": [EXPIRY.isoformat()],
            "resolutions": ["1"],
            "instrument_class": "OPT",
            "option_types": ["CE", "PE"],
            "include_oi": True,
        }
        await store.writer.start()
        try:
            job = make_job(engine, kind="contract_discovery", params=sheet)
            task_id = make_task(engine, job, "underlying_symbols", expiry=EXPIRY)
            client, _ = make_client(answer(standard_body(data=CONTRACTS_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            planned = connection.execute(
                text(
                    "SELECT kind, fyers_symbol, resolution, range_to, parent_task_id, seq"
                    " FROM task WHERE state = 'pending' ORDER BY seq"
                )
            ).mappings().all()
        # Four options, one resolution, one backward probing chunk each. The future is excluded
        # because the sheet asked for options only.
        assert len(planned) == 4
        assert {row["kind"] for row in planned} == {"candle_chunk"}
        assert {row["resolution"] for row in planned} == {"1"}
        assert {row["range_to"] for row in planned} == {EXPIRY.isoformat()}
        assert {row["parent_task_id"] for row in planned} == {task_id}
        assert [row["seq"] for row in planned] == [1, 2, 3, 4]

        with engine.connect() as connection:
            job_row = connection.execute(
                text("SELECT total_tasks, est_requests FROM job WHERE job_id = :job"),
                {"job": job},
            ).one()
        assert job_row[0] == 4

    async def test_a_replayed_discovery_does_not_enqueue_the_chunks_twice(
        self, engine, store, queue
    ):
        register_underlying(engine)
        sheet = {
            "underlying_id": 1,
            "expiry_dates": [EXPIRY.isoformat()],
            "resolutions": ["1"],
            "instrument_class": "OPT",
        }
        await store.writer.start()
        try:
            job = make_job(engine, kind="contract_discovery", params=sheet)
            make_task(engine, job, "underlying_symbols", expiry=EXPIRY)
            client, _ = make_client(answer(standard_body(data=CONTRACTS_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            task = lease_one(queue)
            ctx = HandlerContext(task=task, services=services, supervisor=FakeSupervisor())
            await contract_module.handle_underlying_symbols(ctx)
            await contract_module.handle_underlying_symbols(ctx)
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            pending = connection.execute(
                text("SELECT count(*) FROM task WHERE state = 'pending'")
            ).scalar_one()
        assert pending == 4

    async def test_a_job_with_no_sheet_discovers_and_enqueues_nothing(
        self, engine, store, queue
    ):
        """A schedule fire that only wanted discovery has nothing to say about resolutions."""
        register_underlying(engine)
        await store.writer.start()
        try:
            job = make_job(engine, kind="contract_discovery", params={"scope": "nightly"})
            make_task(engine, job, "underlying_symbols", expiry=EXPIRY)
            client, _ = make_client(answer(standard_body(data=CONTRACTS_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            pending = connection.execute(
                text("SELECT count(*) FROM task WHERE state = 'pending'")
            ).scalar_one()
        assert pending == 0
        assert len(duck_rows(store, "SELECT 1 FROM dim_contract WHERE kind <> 'SPOT'")) == 5

    async def test_one_unparseable_symbol_does_not_lose_the_rest_of_the_chain(
        self, engine, store, queue
    ):
        register_underlying(engine)
        payload = json.loads(json.dumps(CONTRACTS_PAYLOAD))
        payload["contracts"]["options"].append("garbage-not-a-symbol")
        await store.writer.start()
        try:
            job = make_job(engine, kind="contract_discovery")
            task_id = make_task(engine, job, "underlying_symbols", expiry=EXPIRY)
            client, _ = make_client(answer(standard_body(data=payload)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert row["state"] == "done"
        assert row["row_count"] == 5
        assert "garbage-not-a-symbol" in row["last_error_text"]

    async def test_an_mcx_underlying_is_refused_before_a_request_is_spent(
        self, engine, store, queue
    ):
        await store.writer.start()
        try:
            job = make_job(engine, kind="contract_discovery")
            task_id = make_task(
                engine, job, "underlying_symbols", symbol="MCX:CRUDEOIL", expiry=EXPIRY
            )
            client, transport = make_client(answer(standard_body(data=CONTRACTS_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        assert read_task(engine, task_id)["state"] == "failed"
        assert transport.requests == []


# ---------------------------------------------------------------------------
# chain_snapshot
# ---------------------------------------------------------------------------


CHAIN_PAYLOAD = {
    "callOi": 138681800,
    "putOi": 120000000,
    "indiavixData": {"ltp": 13.25},
    "expiryData": [
        {"date": "27-03-2025", "expiry": "1743062400", "expiry_flag": "M"},
        {"date": "03-04-2025", "expiry": "1743667200", "expiry_flag": "W"},
    ],
    "optionsChain": [
        {
            "symbol": NIFTY,
            "option_type": "",
            "strike_price": -1,
            "ltp": 23114.5,
            "fp": 23146,
        },
        {
            "symbol": "NSE:NIFTY25MAR23000CE",
            "option_type": "CE",
            "strike_price": 23000,
            "ltp": 266.0,
            "bid": 266.15,
            "ask": 267.2,
            "volume": 15859935,
            "oi": 726700,
            "prev_oi": 723645,
            "greeks": {"delta": 0.56, "gamma": 0.0007, "theta": -28.52, "vega": 9.51, "iv": 23.7},
        },
        {
            "symbol": "NSE:NIFTY25MAR23000PE",
            "option_type": "PE",
            "strike_price": 23000,
            "ltp": 192.8,
            "bid": 193.0,
            "ask": 193.65,
            "volume": 82921150,
            "oi": 1453010,
            "prev_oi": 957580,
            "greeks": {"delta": -0.44, "gamma": 0.0007, "theta": -28.48, "vega": 9.51, "iv": 23.7},
        },
    ],
}


class TestChainSnapshot:
    async def test_it_records_the_legs_and_upgrades_the_expiry_cycle_to_the_api_answer(
        self, engine, store, queue
    ):
        register_underlying(engine)
        seed_catalog(store, expiry=EXPIRY)
        await store.writer.start()
        try:
            job = make_job(engine, kind="chain_snapshot")
            task_id = make_task(engine, job, "chain_snapshot", params={"strikecount": 5})
            client, transport = make_client(answer(standard_body(data=CHAIN_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        rows = duck_rows(
            store,
            "SELECT expiry_date, expiry_flag, CAST(strike AS DOUBLE), option_type, fyers_symbol,"
            " CAST(ltp AS DOUBLE), oi, prev_oi, CAST(delta AS DOUBLE), CAST(iv AS DOUBLE),"
            " CAST(fp AS DOUBLE), CAST(india_vix AS DOUBLE), task_id"
            " FROM chain_snapshot ORDER BY option_type",
        )
        assert len(rows) == 2
        call = rows[0]
        assert (call[0], call[1], call[2], call[3]) == (EXPIRY, "M", 23000.0, "CE")
        assert call[4] == "NSE:NIFTY25MAR23000CE"
        assert call[6] == 726700
        assert call[8] == 0.56
        assert call[9] == 23.7
        # The underlying's forward price and the separate VIX quote ride along on every row.
        assert call[10] == 23146.0
        assert call[11] == 13.25
        assert call[12] == task_id

        [cycle] = duck_rows(
            store,
            "SELECT expiry_cycle_derived, expiry_cycle_source, is_last_of_month FROM dim_expiry"
            " WHERE underlying_id = 1 AND expiry_date = ?",
            [EXPIRY],
        )
        assert cycle == ("M", "api", True)

        assert read_task(engine, task_id)["state"] == "done"
        assert dict(transport.last_request.url.params)["strikecount"] == "5"
        assert dict(transport.last_request.url.params)["greeks"] == "1"

    async def test_an_expiry_the_catalog_does_not_know_is_not_invented(
        self, engine, store, queue
    ):
        """The chain lists forward expiries that have not expired and have no dim_expiry row."""
        register_underlying(engine)
        seed_catalog(store, expiry=EXPIRY)
        await store.writer.start()
        try:
            job = make_job(engine, kind="chain_snapshot")
            make_task(engine, job, "chain_snapshot")
            client, _ = make_client(answer(standard_body(data=CHAIN_PAYLOAD)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        rows = duck_rows(store, "SELECT expiry_date FROM dim_expiry WHERE underlying_id = 1")
        assert [row[0] for row in rows] == [EXPIRY]

    def test_the_expiry_date_is_read_as_day_month_year(self):
        """The one place in this API that is not yyyy-mm-dd."""
        assert chain_module.parse_expiry_data(CHAIN_PAYLOAD) == [
            (date(2025, 3, 27), "M"),
            (date(2025, 4, 3), "W"),
        ]

    def test_the_underlying_row_is_found_by_its_marker_and_not_by_position(self):
        legs, underlying = chain_module.parse_chain_rows(CHAIN_PAYLOAD)
        assert [leg["option_type"] for leg in legs] == ["CE", "PE"]
        assert underlying["symbol"] == NIFTY


# ---------------------------------------------------------------------------
# symbol_master
# ---------------------------------------------------------------------------


MASTER_FILE = {
    "NSE:SBIN25MAR320CE": {
        "fyToken": "1011250327320000",
        "symbolTicker": "NSE:SBIN25MAR320CE",
        "exSymName": "SBIN",
        "underSym": "SBIN",
        "exInstType": 14,
        "minLotSize": 750,
        "tickSize": 0.05,
        "strikePrice": 320.0,
        "optType": "CE",
        "expiryDate": 1743062400,
        "symbolDesc": "SBIN 27 MAR 25 320 CE",
        "exSeries": "",
        "qtyFreeze": 0,
        "qtyMultiplier": 1,
        "faceValue": 1,
        "isin": "",
        "tradingSession": "0915-1530",
        "underFyTok": "10100000003045",
    }
}


class TestSymbolMaster:
    async def test_it_runs_the_snapshot_and_spends_no_part_of_the_daily_budget(
        self, engine, store, queue, tmp_path, monkeypatch
    ):
        import httpx

        def public_file(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=json.dumps(MASTER_FILE).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )

        transport = RecordingTransport(public_file)
        public_client = httpx.AsyncClient(transport=transport)

        original = master_handler.master.run_symbol_master_snapshot

        async def run(**kwargs):
            kwargs.setdefault("client", public_client)
            kwargs["rebuild_roots"] = False
            kwargs["audit_parser"] = False
            return await original(**kwargs)

        monkeypatch.setattr(master_handler.master, "run_symbol_master_snapshot", run)

        class Paths:
            tmp_dir = tmp_path

        await store.writer.start()
        try:
            job = make_job(engine, kind="symbol_master")
            task_id = make_task(
                engine, job, "symbol_master", params={"files": ["NSE_FO"]}, symbol=None
            )
            client, api_transport = make_client(answer(standard_body()))
            services = Services(client=client, store=store, engine=engine, paths=Paths())
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
            await public_client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert row["state"] == "done"
        assert row["row_count"] == 1
        # The masters are public objects on a CDN, so the trading API is never called for them.
        assert api_transport.requests == []
        assert "public.fyers.in" in str(transport.last_request.url)
        assert "authorization" not in {
            name.lower() for name in transport.last_request.headers.keys()
        }

        with engine.connect() as connection:
            used = connection.execute(
                text("SELECT requests_used FROM job WHERE job_id = :job"), {"job": job}
            ).scalar_one()
        assert used == 0

        rows = duck_rows(store, "SELECT fytoken, min_lot_size FROM dim_instrument_master")
        assert rows == [("1011250327320000", 750)]

    def test_an_unknown_master_file_is_refused_rather_than_downloaded(self):
        from expirymanager.pipeline.worker import HandlerError

        with pytest.raises(HandlerError):
            master_handler.files_from_params(json.dumps({"files": ["NSE_XX"]}))

    def test_the_default_run_order_puts_cash_before_derivatives(self):
        order = master_handler.files_from_params(None)
        assert order.index("NSE_CM") < order.index("NSE_FO")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class TestExport:
    def test_no_export_handler_is_registered_because_it_makes_no_request(self):
        """task.kind has no export member, and that is the schema telling the truth."""
        assert "export" not in worker_module.registered_kinds()
        assert not hasattr(export_module, "install")

    async def test_a_successful_export_writes_the_file_the_manifest_and_the_row(
        self, engine, store, queue, tmp_path
    ):
        register_underlying(engine)
        contracts = seed_catalog(store, expiry=EXPIRY)
        _seed_candles(store, contracts[0])
        exports_dir = tmp_path / "exports"
        exports_dir.mkdir()
        bus = EventBus()
        params = {"format": "csv", "underlying_id": 1}
        spec = export_module.spec_from_params(params)

        await store.writer.start()
        try:
            export_id = export_module.create_export_row(engine, spec, params=params)
            run = await export_module.run_export_job(
                engine=engine,
                store=store,
                writer=store.writer,
                exports_dir=exports_dir,
                spec=spec,
                export_id=export_id,
                bus=bus,
            )
        finally:
            await store.writer.stop()

        assert run.ok
        assert run.row_count == 2
        assert Path(run.path).exists()

        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM export_job WHERE export_id = :id"), {"id": export_id}
            ).mappings().one()
        assert row["status"] == "ready"
        assert row["row_count"] == 2
        assert row["file_path"] == run.path
        assert row["finished_at"]

        manifest = duck_rows(
            store, "SELECT export_id, kind, row_count FROM export_manifest"
        )
        assert manifest == [(export_id, "csv_single", 2)]

        frames = [frame for frame in bus.history() if frame.event == EVENT_EXPORT_READY]
        assert len(frames) == 1
        assert frames[0].data["export_id"] == export_id

    async def test_a_refused_spec_leaves_the_row_failed_with_the_reason(
        self, engine, store, tmp_path
    ):
        exports_dir = tmp_path / "exports"
        exports_dir.mkdir()
        # Bypass spec_from_params so an invalid combination reaches the runner, which is what a
        # spec built once and run later looks like.
        spec = ExportSpec(format="csv", layout="hive", scope=ExportScope())
        await store.writer.start()
        try:
            export_id = export_module.create_export_row(engine, spec, params={})
            run = await export_module.run_export_job(
                engine=engine,
                store=store,
                writer=store.writer,
                exports_dir=exports_dir,
                spec=spec,
                export_id=export_id,
            )
        finally:
            await store.writer.stop()

        assert run.status == "failed"
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT status, error_text FROM export_job WHERE export_id = :id"),
                {"id": export_id},
            ).one()
        assert row[0] == "failed"
        assert "hive" in row[1]


def _seed_candles(store, contract_id: int) -> None:
    cur = store.cursor()
    try:
        cur.execute(
            "INSERT INTO candles VALUES (?, 2, TIMESTAMP '2025-03-26 09:15:00', 1.0, 2.0, 0.5,"
            " 1.5, 100, 200), (?, 2, TIMESTAMP '2025-03-26 09:16:00', 1.5, 2.5, 1.0, 2.0, 110,"
            " 210)",
            [contract_id, contract_id],
        )
    finally:
        cur.close()
