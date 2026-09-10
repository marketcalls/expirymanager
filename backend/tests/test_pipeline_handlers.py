"""The candle and spot handlers, against a fake transport and real databases.

Nothing here touches the network. Every request is answered by an httpx.MockTransport carrying a
recorded response shape, and every credential is synthetic.

The databases are real, and deliberately so. The interesting assertions in this file are about
what is in the tables afterwards: that a chunk replayed after a crash between the DuckDB commit
and the SQLite ack leaves exactly one copy of the rows, that a no_data response cancels the older
chunks in one statement rather than spending requests to relearn they are empty, and that each of
the six retry classes moves the task row exactly one way. A mock of either store would let all
three of those pass while being wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from expirymanager.brokers.fyers.client import AuthContext, FyersClient
from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.arrow import IST_OFFSET_SECONDS
from expirymanager.db.duck import DuckStore
from expirymanager.db.reader import DuckReader
from expirymanager.pipeline import worker as worker_module
from expirymanager.pipeline.handlers import candle_chunk as candle_module
from expirymanager.pipeline.queue import LeaseQueue, iso_at
from expirymanager.pipeline.worker import HandlerContext, Worker
from tests.fyers_fake_transport import (
    FAKE_ACCESS_TOKEN,
    FAKE_APP_ID,
    RecordingTransport,
    candle_body,
    json_response,
)
from tests.test_pipeline_planner import register_underlying, seed_catalog

EXPIRY = date(2025, 3, 27)
RES = "1"
RES_ID = 2
NOW = datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC)

OI_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
NO_OI_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Fakes: only the surface a handler is allowed to reach
# ---------------------------------------------------------------------------


class FakeTokens:
    """A token supplier with a synthetic token. Nothing here resembles a credential."""

    def __init__(self) -> None:
        self.calls = 0

    async def auth_context(self) -> AuthContext:
        self.calls += 1
        return AuthContext(app_id=FAKE_APP_ID, access_token=FAKE_ACCESS_TOKEN, generation=1)


class FakeBroker:
    """Just enough of TokenBroker for the fingerprint provenance."""

    class _Record:
        fingerprint = "0f0f0f0f0f0f0f0f"

    record = _Record()


class RecordingGovernor:
    """Counts slot acquisitions, which is how the tests prove the governed path was used."""

    def __init__(self) -> None:
        self.slots: list[str] = []
        self.rate_limits: list[dict] = []

    def slot(self, endpoint: str):
        governor = self

        class _Slot:
            async def __aenter__(self_inner):
                governor.slots.append(endpoint)
                return None

            async def __aexit__(self_inner, *_exc):
                return False

        return _Slot()

    async def note_rate_limited(self, *, endpoint, http_status, code) -> None:
        self.rate_limits.append({"endpoint": endpoint, "http_status": http_status, "code": code})


class FakeSupervisor:
    """The four calls a worker makes on its supervisor, recorded rather than acted on."""

    def __init__(self) -> None:
        self.auth_failures: list[tuple[int, str, bool]] = []
        self.rate_limits: list[str] = []
        self.settled: list[str] = []

    def gates_open(self) -> bool:
        return True

    def token_generation(self) -> int:
        return 1

    async def on_auth_failure(self, generation, *, reason="", fatal=False) -> None:
        self.auth_failures.append((generation, reason, fatal))

    async def on_rate_limited(self, *, reason="") -> None:
        self.rate_limits.append(reason)

    async def on_task_settled(self, job_id) -> None:
        self.settled.append(job_id)


class FakeDispatcher:
    def __init__(self, owner: str = "worker-test") -> None:
        self.owner = owner

    def task_done(self) -> None:
        return None


async def inline(fn, /, *args, **kwargs):
    """Run the queue's blocking calls on this thread, so a test is deterministic."""
    return fn(*args, **kwargs)


class Services:
    """The AppState shaped bag a handler resolves its dependencies from."""

    def __init__(self, *, client, store, engine, paths=None) -> None:
        self.fyers_client = client
        self.duck_writer = store.writer
        self.duck_reader = DuckReader(store)
        self.engine = engine
        self.settings = None
        self.paths = paths
        self.token_broker = FakeBroker()
        self.governor = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_caches():
    candle_module.clear_caches()
    # The registry is process wide, so each test starts from a known one rather than depending on
    # whether some other module happened to have been imported first.
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


@pytest.fixture
def contracts(engine, store) -> list[int]:
    register_underlying(engine)
    return seed_catalog(store, expiry=EXPIRY)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_job(engine, job_id: str = "job-1", *, params: dict | None = None) -> str:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (job_id, kind, status, params_json, priority, created_at)"
                " VALUES (:job_id, 'candle_backfill', 'running', :params, 100, :now)"
            ),
            {
                "job_id": job_id,
                "params": json.dumps(params or {}),
                "now": iso_at(NOW),
            },
        )
    return job_id


def make_chunk_task(
    engine,
    job_id: str,
    *,
    kind: str = "candle_chunk",
    seq: int = 0,
    contract_id: int | None = None,
    symbol: str = "NSE:NIFTY25MAR23000CE",
    resolution: str = RES,
    range_from: date = date(2025, 1, 1),
    range_to: date = EXPIRY,
    include_oi: bool = True,
    params: dict | None = None,
) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO task (job_id, seq, kind, state, priority, underlying_id,"
                    " contract_id, fyers_symbol, expiry_date, resolution, range_from, range_to,"
                    " include_oi, request_params_json, not_before, created_at)"
                    " VALUES (:job_id, :seq, :kind, 'pending', 100, 1, :contract_id, :symbol,"
                    " :expiry, :resolution, :range_from, :range_to, :include_oi, :params,"
                    " :now, :now) RETURNING task_id"
                ),
                {
                    "job_id": job_id,
                    "seq": seq,
                    "kind": kind,
                    "contract_id": contract_id,
                    "symbol": symbol,
                    "expiry": EXPIRY.isoformat(),
                    "resolution": resolution,
                    "range_from": range_from.isoformat(),
                    "range_to": range_to.isoformat(),
                    "include_oi": 1 if include_oi else 0,
                    "params": json.dumps(params) if params else None,
                    "now": iso_at(NOW),
                },
            ).scalar_one()
        )


def make_client(handler, *, governor=None) -> tuple[FyersClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    client = FyersClient(tokens=FakeTokens(), governor=governor, transport=transport)
    return client, transport


def answer(body: dict, status_code: int = 200):
    return lambda _request: json_response(status_code, body)


def epoch_for_ist(moment: datetime) -> int:
    return int(moment.replace(tzinfo=UTC).timestamp()) - IST_OFFSET_SECONDS


def bar(moment: datetime, price: float = 100.0, volume: int = 1000, oi: int | None = 5000):
    row = [epoch_for_ist(moment), price, price + 1, price - 1, price + 0.5, volume]
    if oi is not None:
        row.append(oi)
    return row


def lease_one(queue: LeaseQueue, owner: str = "worker-test"):
    leased = queue.lease(owner=owner, limit=1)
    assert len(leased) == 1
    return leased[0]


def context(task, services, **extras) -> HandlerContext:
    return HandlerContext(task=task, services=services, supervisor=FakeSupervisor(), extras=extras)


async def run_through_worker(engine, queue, services, task, supervisor=None):
    """Drive one task the way production does: worker, registry, queue transition."""
    supervisor = supervisor or FakeSupervisor()
    worker = Worker(
        "solo",
        supervisor=supervisor,
        dispatcher=FakeDispatcher(),
        queue=queue,
        services=services,
        to_thread=inline,
    )
    await worker._run_one(task)
    return supervisor


def read_task(engine, task_id: int) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM task WHERE task_id = :task_id"), {"task_id": task_id}
            ).mappings().one()
        )


def read_job(engine, job_id: str) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM job WHERE job_id = :job_id"), {"job_id": job_id}
            ).mappings().one()
        )


def candle_rows(store, contract_id: int, res_id: int = RES_ID):
    cur = store.cursor()
    try:
        return cur.execute(
            "SELECT ts, open, high, low, close, volume, oi FROM candles"
            " WHERE contract_id = ? AND res_id = ? ORDER BY ts",
            [contract_id, res_id],
        ).fetchall()
    finally:
        cur.close()


def coverage_rows(store, contract_id: int):
    cur = store.cursor()
    try:
        return cur.execute(
            "SELECT range_from, range_to, status, row_count, include_oi, columns_json,"
            " payload_sha256, http_status, latency_ms, response_bytes, token_fingerprint, task_id"
            " FROM candle_coverage WHERE contract_id = ? ORDER BY range_from",
            [contract_id],
        ).fetchall()
    finally:
        cur.close()


def sealed_at(store, contract_id: int):
    cur = store.cursor()
    try:
        return cur.execute(
            "SELECT sealed_at FROM dim_contract WHERE contract_id = ?", [contract_id]
        ).fetchone()[0]
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestCandleChunkHappyPath:
    async def test_an_ok_response_writes_rows_coverage_and_full_provenance(
        self, engine, store, queue, contracts
    ):
        contract_id = contracts[0]
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(engine, job, contract_id=contract_id)
            candles = [
                bar(datetime(2025, 3, 26, 9, 15), 730.0, 359100, 5131225),
                bar(datetime(2025, 3, 26, 9, 16), 706.0, 246225, 5019500),
            ]
            client, transport = make_client(
                answer(candle_body(columns=OI_COLUMNS, candles=candles))
            )
            services = Services(client=client, store=store, engine=engine)
            task = lease_one(queue)
            await run_through_worker(engine, queue, services, task)
            await client.aclose()
        finally:
            await store.writer.stop()

        rows = candle_rows(store, contract_id)
        assert len(rows) == 2
        # Epoch seconds land as naive IST, which is the epoch plus a fixed 19800.
        assert rows[0][0] == datetime(2025, 3, 26, 9, 15)
        assert float(rows[0][1]) == 730.0
        assert rows[0][6] == 5131225

        [coverage] = coverage_rows(store, contract_id)
        assert coverage[2] == "ok"
        assert coverage[3] == 2
        assert json.loads(coverage[5]) == OI_COLUMNS
        assert coverage[10] == FakeBroker.record.fingerprint
        assert coverage[11] == task_id

        row = read_task(engine, task_id)
        assert row["state"] == "done"
        assert row["fyers_s"] == "ok"
        assert row["row_count"] == 2
        assert row["http_status"] == 200
        assert json.loads(row["columns_json"]) == OI_COLUMNS
        assert row["payload_sha256"] and len(row["payload_sha256"]) == 64
        assert row["schema_version"] == 1
        assert row["response_bytes"] > 0
        assert row["latency_ms"] is not None
        assert row["first_ts"].startswith("2025-03-26 09:15")
        assert row["last_ts"].startswith("2025-03-26 09:16")
        assert row["token_fingerprint"] == FakeBroker.record.fingerprint

        assert read_job(engine, job)["rows_written"] == 2
        assert read_job(engine, job)["requests_used"] == 1
        # date_format=1 and the clamped range go on the wire, and nothing else does.
        query = dict(transport.last_request.url.params)
        assert query["date_format"] == "1"
        assert query["range_to"] == EXPIRY.isoformat()
        assert query["include_oi"] == "1"
        assert "token" not in transport.last_request.url.query.decode()

    async def test_fields_are_mapped_through_the_returned_columns_array(
        self, engine, store, queue, contracts
    ):
        """The seventh element is open_interest only when it was asked for, so order is data."""
        contract_id = contracts[0]
        shuffled = ["timestamp", "close", "open", "volume", "high", "open_interest", "low"]
        moment = datetime(2025, 3, 26, 9, 15)
        candles = [[epoch_for_ist(moment), 4.0, 1.0, 99, 2.0, 12345, 3.0]]
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(engine, job, contract_id=contract_id)
            client, _ = make_client(answer(candle_body(columns=shuffled, candles=candles)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        [row] = candle_rows(store, contract_id)
        assert [float(row[1]), float(row[2]), float(row[3]), float(row[4])] == [1.0, 2.0, 3.0, 4.0]
        assert row[5] == 99
        assert row[6] == 12345

    async def test_a_six_column_response_leaves_open_interest_null(
        self, engine, store, queue, contracts
    ):
        contract_id = contracts[0]
        candles = [bar(datetime(2025, 3, 26, 9, 15), oi=None)]
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(engine, job, contract_id=contract_id, include_oi=False)
            client, transport = make_client(
                answer(candle_body(columns=NO_OI_COLUMNS, candles=candles))
            )
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        [row] = candle_rows(store, contract_id)
        assert row[6] is None
        assert "include_oi" not in dict(transport.last_request.url.params)

    async def test_every_request_takes_exactly_one_governor_slot(
        self, engine, store, queue, contracts
    ):
        """The governor is the only outbound gate, and it is reached only through the client."""
        governor = RecordingGovernor()
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(engine, job, contract_id=contracts[0])
            client, _ = make_client(answer(candle_body()), governor=governor)
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        assert governor.slots == ["expired-historical-data"]

    async def test_an_unchanged_payload_short_circuits_the_second_write(
        self, engine, store, queue, contracts
    ):
        contract_id = contracts[0]
        body = candle_body(candles=[bar(datetime(2025, 3, 26, 9, 15))])
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(engine, job, contract_id=contract_id, seq=0)
            make_chunk_task(engine, job, contract_id=contract_id, seq=1)
            client, _ = make_client(answer(body))
            services = Services(client=client, store=store, engine=engine)
            first = lease_one(queue)
            await run_through_worker(engine, queue, services, first)
            second = lease_one(queue)
            await run_through_worker(engine, queue, services, second)
            await client.aclose()
        finally:
            await store.writer.stop()

        assert len(candle_rows(store, contract_id)) == 1
        assert read_task(engine, second.task_id)["state"] == "done"
        # The row count still describes the response; rows_written is what the write actually did.
        assert read_job(engine, job)["rows_written"] == 1


# ---------------------------------------------------------------------------
# no_data, which is a success
# ---------------------------------------------------------------------------


class TestNoData:
    async def test_no_data_is_a_success_that_records_an_empty_coverage_row(
        self, engine, store, queue, contracts
    ):
        contract_id = contracts[0]
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(engine, job, contract_id=contract_id)
            client, _ = make_client(answer(candle_body(status="no_data")))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert row["state"] == "empty"
        assert row["fyers_s"] == "no_data"
        assert row["row_count"] == 0
        assert row["last_error_text"] is None

        [coverage] = coverage_rows(store, contract_id)
        assert coverage[2] == "empty"
        assert coverage[3] == 0
        assert candle_rows(store, contract_id) == []

    async def test_no_data_marks_the_older_chunks_skipped_and_seals_the_contract(
        self, engine, store, queue, contracts
    ):
        """This is where most of the request saving comes from on a non probing plan."""
        contract_id = contracts[0]
        await store.writer.start()
        try:
            job = make_job(engine)
            newest = make_chunk_task(
                engine,
                job,
                contract_id=contract_id,
                seq=0,
                range_from=date(2025, 3, 1),
                range_to=EXPIRY,
            )
            older = make_chunk_task(
                engine,
                job,
                contract_id=contract_id,
                seq=1,
                range_from=date(2024, 11, 21),
                range_to=date(2025, 2, 28),
            )
            oldest = make_chunk_task(
                engine,
                job,
                contract_id=contract_id,
                seq=2,
                range_from=date(2024, 8, 13),
                range_to=date(2024, 11, 20),
            )
            other_resolution = make_chunk_task(
                engine,
                job,
                contract_id=contract_id,
                seq=3,
                resolution="5",
                range_from=date(2024, 11, 21),
                range_to=date(2025, 2, 28),
            )
            client, transport = make_client(answer(candle_body(status="no_data")))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        assert read_task(engine, newest)["state"] == "empty"
        assert read_task(engine, older)["state"] == "skipped"
        assert read_task(engine, oldest)["state"] == "skipped"
        # A different resolution is a different availability question and is left alone.
        assert read_task(engine, other_resolution)["state"] == "pending"
        # One request answered four chunks worth of window.
        assert len(transport.requests) == 1
        # The seal waits for the other resolution, which is still open for this contract.
        assert sealed_at(store, contract_id) is None

    async def test_the_contract_is_sealed_once_nothing_else_is_queued_for_it(
        self, engine, store, queue, contracts
    ):
        contract_id = contracts[0]
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(engine, job, contract_id=contract_id)
            client, _ = make_client(answer(candle_body(status="no_data")))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        assert sealed_at(store, contract_id) is not None


# ---------------------------------------------------------------------------
# The six retry classes
# ---------------------------------------------------------------------------


ERROR_CASES = [
    # body, http status, expected task state, expected attempt, what the supervisor heard
    ({"s": "error", "code": -50, "message": "Invalid input"}, 422, "failed", 0, None),
    ({"s": "error", "code": -300, "message": "invalid symbol"}, 400, "failed", 0, None),
    ({"s": "error", "code": -352, "message": "invalid app id"}, 400, "failed", 0, None),
    ({"s": "error", "message": "server error"}, 500, "pending", 1, None),
    ({"s": "error", "code": -8, "message": "token expired"}, 200, "pending", 0, "auth"),
    ({"s": "error", "code": -16, "message": "cannot authenticate"}, 200, "pending", 0, "auth"),
    ({"s": "error", "code": -15, "message": "invalid token"}, 200, "pending", 0, "auth_fatal"),
    ({"s": "error", "message": "unauthorised"}, 401, "pending", 0, "auth_fatal"),
    ({"s": "error", "code": -429, "message": "rate limit"}, 200, "pending", 0, "rate"),
    ({"s": "error", "message": "too many requests"}, 429, "pending", 0, "rate"),
]


class TestErrorClassification:
    @pytest.mark.parametrize("body,http_status,state,attempt,signal", ERROR_CASES)
    async def test_each_error_class_takes_exactly_one_queue_transition(
        self, engine, store, queue, contracts, body, http_status, state, attempt, signal
    ):
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(engine, job, contract_id=contracts[0])
            client, _ = make_client(answer(body, http_status))
            services = Services(client=client, store=store, engine=engine)
            supervisor = await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert row["state"] == state
        assert row["attempt"] == attempt
        assert row["fyers_code"] == body.get("code")
        assert row["last_error_text"]
        # Nothing was written: a failed request has no candles and no coverage row.
        assert coverage_rows(store, contracts[0]) == []

        if signal == "auth":
            assert supervisor.auth_failures and supervisor.auth_failures[0][2] is False
        elif signal == "auth_fatal":
            assert supervisor.auth_failures and supervisor.auth_failures[0][2] is True
        elif signal == "rate":
            assert supervisor.rate_limits
        else:
            assert not supervisor.auth_failures
            assert not supervisor.rate_limits

    async def test_an_invalid_symbol_error_reports_what_actually_went_on_the_wire(
        self, engine, store, queue, contracts
    ):
        """The documented cause of -300 is a character that was not percent encoded."""
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(
                engine, job, contract_id=contracts[0], symbol="NSE:M&M25MAR3000CE"
            )
            client, transport = make_client(
                answer({"s": "error", "code": -300, "message": "invalid symbol"}, 400)
            )
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        assert "M%26M" in read_task(engine, task_id)["last_error_text"]
        assert "M%26M" in transport.last_request.url.query.decode()

    async def test_a_transport_failure_is_transient_and_consumes_one_attempt(
        self, engine, store, queue, contracts
    ):
        def explode(_request):
            raise httpx.ConnectError("connection reset")

        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(engine, job, contract_id=contracts[0])
            client, _ = make_client(explode)
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert (row["state"], row["attempt"]) == ("pending", 1)
        # The retry waits in the table, never in a worker holding a lease.
        assert row["not_before"] > iso_at(datetime.now(UTC))

    async def test_a_response_that_cannot_be_mapped_is_fatal_rather_than_retried(
        self, engine, store, queue, contracts
    ):
        """Three more governed requests would relearn the same thing about the same payload."""
        body = {
            "s": "ok",
            "symbol": "NSE:NIFTY25MAR23000CE",
            "resolution": RES,
            "schema_version": 1,
            "columns": ["timestamp", "open", "high", "low", "close", "vol"],
            "candles": [[1742960700, 1.0, 2.0, 0.5, 1.5, 10]],
        }
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(engine, job, contract_id=contracts[0])
            client, _ = make_client(answer(body))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert (row["state"], row["attempt"]) == ("failed", 0)
        assert "volume" in row["last_error_text"]


# ---------------------------------------------------------------------------
# MCX, which the endpoint does not serve at all
# ---------------------------------------------------------------------------


class TestMcx:
    async def test_an_mcx_symbol_fails_before_a_request_is_spent(
        self, engine, store, queue, contracts
    ):
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(
                engine, job, contract_id=contracts[0], symbol="MCX:CRUDEOIL25MARFUT"
            )
            client, transport = make_client(answer(candle_body()))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert row["state"] == "failed"
        assert "MCX is not served" in row["last_error_text"]
        assert "422" in row["last_error_text"]
        assert transport.requests == []


# ---------------------------------------------------------------------------
# The crash between the commit and the ack
# ---------------------------------------------------------------------------


class TestReplay:
    async def test_a_crash_between_the_commit_and_the_ack_replays_without_duplicating_rows(
        self, engine, store, queue, contracts
    ):
        """Commit first, ack second. The replay costs one request and nothing else."""
        contract_id = contracts[0]
        candles = [
            bar(datetime(2025, 3, 26, 9, 15)),
            bar(datetime(2025, 3, 26, 9, 16), 101.0),
        ]
        body = candle_body(candles=candles)
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(engine, job, contract_id=contract_id)
            client, transport = make_client(answer(body))
            services = Services(client=client, store=store, engine=engine)

            # First run: the handler is called directly and its outcome is thrown away, which is
            # exactly what a hard kill after the DuckDB commit looks like from SQLite's side.
            first = lease_one(queue)
            outcome = await candle_module.handle_candle_chunk(context(first, services))
            assert outcome.state == "done"
            assert len(candle_rows(store, contract_id)) == 2
            assert read_task(engine, task_id)["state"] == "leased"

            # The lease lapses and the row comes back without burning an attempt.
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE task SET lease_expires_at = :past WHERE task_id = :task_id"
                    ),
                    {"past": iso_at(NOW - timedelta(hours=1)), "task_id": task_id},
                )
            assert queue.reclaim_expired_leases() == 1

            second = lease_one(queue)
            await run_through_worker(engine, queue, services, second)
            await client.aclose()
        finally:
            await store.writer.stop()

        # The window was rewritten, not appended to.
        assert len(candle_rows(store, contract_id)) == 2
        assert len(coverage_rows(store, contract_id)) == 1
        row = read_task(engine, task_id)
        assert (row["state"], row["attempt"]) == ("done", 0)
        assert len(transport.requests) == 2

    async def test_an_ack_after_the_lease_was_reclaimed_writes_nothing(
        self, engine, store, queue, contracts
    ):
        """The owner scoped ack is what stops two workers racing on the same row."""
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(engine, job, contract_id=contracts[0])
            client, _ = make_client(answer(candle_body()))
            services = Services(client=client, store=store, engine=engine)
            task = lease_one(queue, owner="worker-a")
            queue.release([task_id], owner="worker-a")
            stolen = lease_one(queue, owner="worker-b")
            assert stolen.task_id == task_id

            worker = Worker(
                "solo",
                supervisor=FakeSupervisor(),
                dispatcher=FakeDispatcher("worker-a"),
                queue=queue,
                services=services,
                to_thread=inline,
            )
            await worker._run_one(task)
            await client.aclose()
        finally:
            await store.writer.stop()

        # The data landed anyway, because the DuckDB write is idempotent and owner blind.
        assert len(candle_rows(store, contracts[0])) == 2
        # The row still belongs to worker-b.
        row = read_task(engine, task_id)
        assert (row["state"], row["lease_owner"]) == ("leased", "worker-b")


# ---------------------------------------------------------------------------
# Backward probing
# ---------------------------------------------------------------------------


class TestBackwardProbing:
    async def test_a_chunk_full_to_its_left_edge_plans_exactly_one_more(
        self, engine, store, queue, contracts
    ):
        contract_id = contracts[0]
        left_edge = date(2025, 1, 1)
        # A candle on the first trading day at or after the left edge means the contract was
        # already trading when the chunk opened, so there is more history behind it.
        candles = [bar(datetime(2025, 1, 1, 9, 15)), bar(datetime(2025, 3, 26, 15, 29))]
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(engine, job, contract_id=contract_id, range_from=left_edge)
            client, _ = make_client(answer(candle_body(candles=candles)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            planned = connection.execute(
                text(
                    "SELECT range_from, range_to, state, parent_task_id, seq FROM task"
                    " WHERE job_id = :job AND state = 'pending'"
                ),
                {"job": job},
            ).mappings().all()
        assert len(planned) == 1
        assert planned[0]["range_to"] == (left_edge - timedelta(days=1)).isoformat()
        assert planned[0]["range_from"] == (left_edge - timedelta(days=86)).isoformat()
        assert planned[0]["seq"] == 1
        # The denominator the progress bar divides by keeps up with the growing plan.
        assert read_job(engine, job)["total_tasks"] == 1
        assert sealed_at(store, contract_id) is None

    async def test_a_life_that_started_inside_the_chunk_seals_and_plans_nothing(
        self, engine, store, queue, contracts
    ):
        contract_id = contracts[0]
        candles = [bar(datetime(2025, 3, 20, 9, 15)), bar(datetime(2025, 3, 26, 15, 29))]
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(
                engine, job, contract_id=contract_id, range_from=date(2025, 1, 1)
            )
            client, _ = make_client(answer(candle_body(candles=candles)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            pending = connection.execute(
                text("SELECT count(*) FROM task WHERE job_id = :job AND state = 'pending'"),
                {"job": job},
            ).scalar_one()
        assert pending == 0
        assert sealed_at(store, contract_id) is not None

    async def test_the_walk_stops_at_the_exchange_floor(self, engine, store, queue, contracts):
        """NSE serves nothing before 2022-01-03, so a chunk that opens there has no successor."""
        contract_id = contracts[0]
        left_edge = date(2022, 1, 3)
        candles = [bar(datetime(2022, 1, 3, 9, 15))]
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(
                engine,
                job,
                contract_id=contract_id,
                range_from=left_edge,
                range_to=date(2022, 4, 12),
            )
            client, _ = make_client(answer(candle_body(candles=candles)))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            pending = connection.execute(
                text("SELECT count(*) FROM task WHERE job_id = :job AND state = 'pending'"),
                {"job": job},
            ).scalar_one()
        assert pending == 0
        assert sealed_at(store, contract_id) is not None

    async def test_a_replayed_chunk_does_not_plan_its_successor_twice(
        self, engine, store, queue, contracts
    ):
        contract_id = contracts[0]
        left_edge = date(2025, 1, 1)
        candles = [bar(datetime(2025, 1, 1, 9, 15)), bar(datetime(2025, 3, 26, 15, 29))]
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(engine, job, contract_id=contract_id, range_from=left_edge)
            client, _ = make_client(answer(candle_body(candles=candles)))
            services = Services(client=client, store=store, engine=engine)
            task = lease_one(queue)
            await candle_module.handle_candle_chunk(context(task, services))
            await candle_module.handle_candle_chunk(context(task, services))
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            planned = connection.execute(
                text("SELECT count(*) FROM task WHERE job_id = :job AND state = 'pending'"),
                {"job": job},
            ).scalar_one()
        assert planned == 1
        assert read_job(engine, job)["total_tasks"] == 1


# ---------------------------------------------------------------------------
# The spot series
# ---------------------------------------------------------------------------


class TestSpotChunk:
    async def test_the_spot_handler_uses_the_live_history_endpoint(
        self, engine, store, queue, contracts
    ):
        """A different path, a different oi flag name, and no columns array to trust."""
        body = {
            "s": "ok",
            "symbol": "NSE:NIFTY50-INDEX",
            "resolution": RES,
            "candles": [
                [epoch_for_ist(datetime(2025, 3, 26, 9, 15)), 23000.0, 23050.0, 22990.0, 23010.0, 0]
            ],
        }
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(
                engine,
                job,
                kind="spot_chunk",
                contract_id=1,
                symbol="NSE:NIFTY50-INDEX",
                include_oi=False,
                range_from=date(2025, 3, 1),
                range_to=date(2025, 3, 26),
            )
            client, transport = make_client(answer(body))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        assert transport.last_request.url.path == "/data/history"
        query = dict(transport.last_request.url.params)
        assert query["cont_flag"] == "1"
        assert "oi_flag" not in query
        assert query["date_format"] == "1"

        rows = candle_rows(store, 1)
        assert len(rows) == 1
        assert rows[0][0] == datetime(2025, 3, 26, 9, 15)
        assert rows[0][6] is None
        row = read_task(engine, task_id)
        assert row["state"] == "done"
        # The columns array was assumed from the documented order, and that is recorded verbatim.
        assert json.loads(row["columns_json"]) == NO_OI_COLUMNS

    async def test_the_spot_handler_never_seals_and_never_probes_backward(
        self, engine, store, queue, contracts
    ):
        body = {
            "s": "ok",
            "symbol": "NSE:NIFTY50-INDEX",
            "resolution": RES,
            "candles": [
                [epoch_for_ist(datetime(2025, 3, 1, 9, 15)), 1.0, 2.0, 0.5, 1.5, 0]
            ],
        }
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(
                engine,
                job,
                kind="spot_chunk",
                contract_id=1,
                symbol="NSE:NIFTY50-INDEX",
                include_oi=False,
                range_from=date(2025, 3, 1),
                range_to=date(2025, 3, 26),
            )
            client, _ = make_client(answer(body))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        with engine.connect() as connection:
            pending = connection.execute(
                text("SELECT count(*) FROM task WHERE job_id = :job AND state = 'pending'"),
                {"job": job},
            ).scalar_one()
        assert pending == 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_install_all_binds_every_task_kind_that_makes_a_request(self):
        worker_module.clear_registry()
        try:
            candle_module.install_all()
            assert set(worker_module.registered_kinds()) == {
                "expiry_dates",
                "underlying_symbols",
                "candle_chunk",
                "spot_chunk",
                "symbol_master",
                "chain_snapshot",
            }
            # Idempotent, so a second wiring call is not an error.
            candle_module.install_all()
        finally:
            worker_module.clear_registry()

    def test_the_holiday_calendar_degradation_is_visible_rather_than_silent(
        self, engine, caplog
    ):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM market_holiday WHERE exchange = 'NSE'"))
        candle_module.clear_caches()
        with caplog.at_level("WARNING"):
            calendar = candle_module.trading_calendar(engine, "NSE")
        assert calendar.holidays("NSE") == frozenset()
        assert any("weekends only" in record.message for record in caplog.records)

    def test_a_loaded_holiday_calendar_is_used(self, engine):
        candle_module.clear_caches()
        calendar = candle_module.trading_calendar(engine, "NSE")
        assert calendar.holidays("NSE")


class TestCommitBeforeAck:
    async def test_the_rows_are_already_durable_when_the_ack_runs(
        self, engine, store, queue, contracts
    ):
        """Commit first, ack second. The reverse order loses data on a crash between them."""
        contract_id = contracts[0]
        seen: list[int] = []
        original = queue.ack

        def spy(task, outcome, *, owner=None):
            # Read DuckDB from inside the ack, which is the only moment that can tell the two
            # orderings apart.
            seen.append(len(candle_rows(store, contract_id)))
            return original(task, outcome, owner=owner)

        queue.ack = spy
        await store.writer.start()
        try:
            job = make_job(engine)
            make_chunk_task(engine, job, contract_id=contract_id)
            client, _ = make_client(answer(candle_body()))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        assert seen == [2]

    async def test_an_ok_response_with_no_candles_still_records_its_window(
        self, engine, store, queue, contracts
    ):
        """A window that answered ok with nothing in it must never be paid for twice."""
        contract_id = contracts[0]
        body = {
            "s": "ok",
            "symbol": "NSE:NIFTY25MAR23000CE",
            "resolution": RES,
            "schema_version": 1,
            "candles": [],
        }
        await store.writer.start()
        try:
            job = make_job(engine)
            task_id = make_chunk_task(engine, job, contract_id=contract_id)
            client, _ = make_client(answer(body))
            services = Services(client=client, store=store, engine=engine)
            await run_through_worker(engine, queue, services, lease_one(queue))
            await client.aclose()
        finally:
            await store.writer.stop()

        row = read_task(engine, task_id)
        assert (row["state"], row["row_count"]) == ("done", 0)
        [coverage] = coverage_rows(store, contract_id)
        assert (coverage[2], coverage[3]) == ("ok", 0)
        assert sealed_at(store, contract_id) is not None
