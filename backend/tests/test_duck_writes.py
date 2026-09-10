"""Catalog writes and the candle chunk builder, through the single writer.

These tests use asyncio.run rather than an async test plugin, matching test_duck_writer.py, so
they stand alone ahead of the shared conftest the verification work item owns.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from expirymanager.db.arrow import IST_OFFSET_SECONDS, candles_to_arrow
from expirymanager.db.duck import DuckStore
from expirymanager.db.ids import BLOCK_SIZE, FIRST_BLOCK_BASE
from expirymanager.db.writer import CoverageRow
from expirymanager.db.writes import (
    ContractRow,
    ExpiryRow,
    UnderlyingRow,
    delete_underlying,
    finish_ingest_run,
    start_ingest_run,
    upsert_candle_chunk,
    upsert_contracts,
    upsert_expiries,
    upsert_underlying,
)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
COLUMNS_JSON = '["timestamp","open","high","low","close","volume","open_interest"]'

EXPIRY = date(2025, 3, 27)
RES_ID = 2


@pytest.fixture
def store(tmp_path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    yield store
    store.close()


def nifty(underlying_id: int = 1, symbol: str = "NSE:NIFTY50-INDEX") -> UnderlyingRow:
    return UnderlyingRow(
        underlying_id=underlying_id,
        fyers_symbol=symbol,
        root="NIFTY",
        exchange="NSE",
        exchange_code=10,
        segment="CM",
        segment_code=10,
        instrument_kind="INDEX",
        display_name="Nifty 50",
        data_from=date(2022, 1, 3),
    )


def option(strike: int, right: str) -> ContractRow:
    return ContractRow(
        fyers_symbol=f"NSE:NIFTY25MAR{strike}{right}",
        kind="OPT",
        instrument_class="OPTIDX",
        exchange="NSE",
        exchange_code=10,
        segment="FO",
        segment_code=11,
        root="NIFTY",
        source_endpoint="expired_contracts",
        parse_method="regex",
        parse_confidence="exact",
        strike=Decimal(strike),
        strike_raw=str(strike),
        option_type=right,
        expiry_date=EXPIRY,
        lot_size=75,
    )


def chain_rows(strikes):
    return [option(strike, right) for strike in strikes for right in ("CE", "PE")]


def utc_epoch_for_ist(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp()) - IST_OFFSET_SECONDS


def session(day: date, bars: int, price: float = 100.0):
    rows = []
    for index in range(bars):
        minute = 15 + index
        moment = datetime(day.year, day.month, day.day, 9 + minute // 60, minute % 60)
        rows.append(
            [
                utc_epoch_for_ist(moment),
                price,
                price + 1,
                price - 1,
                price + 0.5,
                1000 + index,
                50000 + index,
            ]
        )
    return rows


def coverage(task_id: int = 1, digest: str | None = None, status: str = "ok") -> CoverageRow:
    return CoverageRow(
        status=status,
        include_oi=True,
        columns_json=COLUMNS_JSON,
        task_id=task_id,
        payload_sha256=digest,
    )


async def with_writer(store, body):
    await store.writer.start()
    try:
        return await body(store.writer)
    finally:
        await store.writer.stop()


def run(store, body):
    return asyncio.run(with_writer(store, body))


# -- underlyings -----------------------------------------------------------


def test_registering_an_underlying_allocates_a_reserved_spot_id_and_a_spot_contract(store):
    async def body(writer):
        return await upsert_underlying(writer, nifty())

    result = run(store, body)
    assert result.spot_contract_id == 1
    assert result.created is True

    row = store.connection.execute(
        "SELECT kind, expiry_id, fyers_symbol FROM dim_contract WHERE contract_id = 1"
    ).fetchone()
    assert row == ("SPOT", None, "NSE:NIFTY50-INDEX")


def test_a_second_registration_keeps_the_same_spot_id(store):
    async def body(writer):
        first = await upsert_underlying(writer, nifty())
        second = await upsert_underlying(writer, nifty())
        return (first, second)

    first, second = run(store, body)
    assert second.spot_contract_id == first.spot_contract_id
    assert second.created is False
    assert (
        store.connection.execute("SELECT count(*) FROM dim_underlying").fetchone()[0] == 1
    )


def test_two_underlyings_take_consecutive_spot_ids(store):
    async def body(writer):
        await upsert_underlying(writer, nifty(1, "NSE:NIFTY50-INDEX"))
        return await upsert_underlying(writer, nifty(2, "NSE:NIFTYBANK-INDEX"))

    assert run(store, body).spot_contract_id == 2


# -- expiries and contracts ------------------------------------------------


def test_a_contract_batch_lands_in_one_contiguous_block(store):
    async def body(writer):
        await upsert_underlying(writer, nifty())
        await upsert_expiries(writer, [ExpiryRow(underlying_id=1, expiry_date=EXPIRY)])
        return await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([22900, 23000, 23100])
        )

    result = run(store, body)
    assert result.allocation.lo == FIRST_BLOCK_BASE
    assert result.allocation.size == BLOCK_SIZE
    assert sorted(result.contract_ids.values()) == list(range(1024, 1030))

    rows = store.connection.execute(
        "SELECT contract_id, strike, option_type FROM dim_contract "
        "WHERE expiry_date = ? ORDER BY contract_id",
        [EXPIRY],
    ).fetchall()
    assert [(int(r[1]), r[2]) for r in rows] == [
        (22900, "CE"),
        (22900, "PE"),
        (23000, "CE"),
        (23000, "PE"),
        (23100, "CE"),
        (23100, "PE"),
    ]


def test_the_expiry_rollup_is_derived_from_the_contracts(store):
    async def body(writer):
        await upsert_underlying(writer, nifty())
        await upsert_contracts(
            writer,
            underlying_id=1,
            expiry_date=EXPIRY,
            rows=chain_rows([22900, 23000, 23100]),
        )

    run(store, body)
    row = store.connection.execute(
        "SELECT contract_count, options_count, futures_count, min_strike, max_strike, "
        "       strike_step, contract_id_lo, contract_id_hi, has_options "
        "  FROM dim_expiry WHERE expiry_date = ?",
        [EXPIRY],
    ).fetchone()
    assert row[0] == 6
    assert row[1] == 6
    assert row[2] == 0
    assert (int(row[3]), int(row[4]), int(row[5])) == (22900, 23100, 100)
    assert (row[6], row[7]) == (1024, 2047)
    assert row[8] is True

    underlying = store.connection.execute(
        "SELECT expiry_count, contract_count, first_expiry, last_expiry FROM dim_underlying"
    ).fetchone()
    assert underlying[:2] == (1, 6)
    assert underlying[2] == underlying[3] == EXPIRY


def test_a_rediscovery_keeps_every_existing_contract_id(store):
    async def body(writer):
        await upsert_underlying(writer, nifty())
        first = await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([23000, 23100])
        )
        second = await upsert_contracts(
            writer,
            underlying_id=1,
            expiry_date=EXPIRY,
            rows=chain_rows([22900, 23000, 23100]),
        )
        return (first, second)

    first, second = run(store, body)
    for symbol, contract_id in first.contract_ids.items():
        assert second.contract_ids[symbol] == contract_id
    assert second.renumbered == {}
    assert second.allocation.lo == first.allocation.lo


def test_a_short_rediscovery_never_removes_contracts_already_known(store):
    """A chain that comes back short is a partial response, not a chain that shrank.

    Expired option chains do not lose strikes. Treating a short response as a deletion would
    throw away contracts whose bars cost governed requests that cannot be recovered, so the
    batch only ever adds and updates.
    """

    async def body(writer):
        await upsert_underlying(writer, nifty())
        await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([22900, 23000, 23100])
        )
        await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([23000])
        )

    run(store, body)
    row = store.connection.execute(
        "SELECT contract_count, min_strike, max_strike FROM dim_expiry WHERE expiry_date = ?",
        [EXPIRY],
    ).fetchone()
    assert row[0] == 6
    assert (int(row[1]), int(row[2])) == (22900, 23100)


def test_two_expiries_never_share_an_id_block(store):
    april = date(2025, 4, 24)

    async def body(writer):
        await upsert_underlying(writer, nifty())
        march_result = await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([23000])
        )
        april_result = await upsert_contracts(
            writer, underlying_id=1, expiry_date=april, rows=chain_rows([23000])
        )
        return (march_result, april_result)

    march_result, april_result = run(store, body)
    assert april_result.allocation.lo == march_result.allocation.hi + 1
    assert set(march_result.contract_ids.values()).isdisjoint(april_result.contract_ids.values())


def test_a_block_overflow_moves_the_bars_with_the_contracts(store):
    """The one path that renumbers. Every candle must follow, or the download is lost."""

    async def body(writer):
        await upsert_underlying(writer, nifty())
        first = await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([23000])
        )
        moved_id = first.contract_ids["NSE:NIFTY25MAR23000CE"]
        rows = session(date(2025, 3, 26), 10)
        await upsert_candle_chunk(
            writer,
            contract_id=moved_id,
            res_id=RES_ID,
            range_from=date(2025, 3, 26),
            range_to=date(2025, 3, 26),
            coverage=coverage(),
            batch=candles_to_arrow(rows, COLUMNS, moved_id, RES_ID),
        )
        # 600 strikes is 1200 contracts, which does not fit the 1024 block already allocated.
        second = await upsert_contracts(
            writer,
            underlying_id=1,
            expiry_date=EXPIRY,
            rows=chain_rows(range(20000, 26000, 10)),
        )
        return (moved_id, second)

    moved_id, second = run(store, body)
    new_id = second.contract_ids["NSE:NIFTY25MAR23000CE"]

    assert second.allocation.previous is not None
    assert second.allocation.size == 2048
    assert new_id != moved_id
    assert second.renumbered[moved_id] == new_id

    con = store.connection
    assert con.execute(
        "SELECT count(*) FROM candles WHERE contract_id = ?", [moved_id]
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM candles WHERE contract_id = ?", [new_id]
    ).fetchone()[0] == 10
    assert con.execute(
        "SELECT count(*) FROM candle_coverage WHERE contract_id = ?", [new_id]
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT row_count FROM contract_bounds WHERE contract_id = ?", [new_id]
    ).fetchone()[0] == 10


# -- the candle chunk builder ---------------------------------------------


def test_the_chunk_builder_enqueues_the_writer_transaction(store):
    async def body(writer):
        await upsert_underlying(writer, nifty())
        result = await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([23000])
        )
        contract_id = result.contract_ids["NSE:NIFTY25MAR23000CE"]
        rows = session(date(2025, 3, 26), 5)
        return (
            contract_id,
            await upsert_candle_chunk(
                writer,
                contract_id=contract_id,
                res_id=RES_ID,
                range_from=date(2025, 3, 26),
                range_to=date(2025, 3, 26),
                coverage=coverage(digest="abc"),
                batch=candles_to_arrow(rows, COLUMNS, contract_id, RES_ID),
            ),
        )

    contract_id, result = run(store, body)
    assert result.rows_written == 5
    assert result.skipped is False
    assert (
        store.connection.execute(
            "SELECT count(*) FROM candles WHERE contract_id = ?", [contract_id]
        ).fetchone()[0]
        == 5
    )


def test_rewriting_a_chunk_converges_and_a_shorter_one_removes_the_surplus(store):
    async def body(writer):
        await upsert_underlying(writer, nifty())
        result = await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([23000])
        )
        contract_id = result.contract_ids["NSE:NIFTY25MAR23000CE"]
        day = date(2025, 3, 26)
        for bars, digest in ((10, "one"), (10, "two"), (4, "three")):
            await upsert_candle_chunk(
                writer,
                contract_id=contract_id,
                res_id=RES_ID,
                range_from=day,
                range_to=day,
                coverage=coverage(digest=digest),
                batch=candles_to_arrow(session(day, bars), COLUMNS, contract_id, RES_ID),
            )
        return contract_id

    contract_id = run(store, body)
    con = store.connection
    assert con.execute(
        "SELECT count(*) FROM candles WHERE contract_id = ?", [contract_id]
    ).fetchone()[0] == 4
    assert con.execute(
        "SELECT count(*) FROM (SELECT contract_id, res_id, ts FROM candles "
        "GROUP BY 1,2,3 HAVING count(*) > 1)"
    ).fetchone()[0] == 0


# -- deletion and runs -----------------------------------------------------


def test_deleting_an_underlying_can_keep_or_purge_its_bars(store):
    async def prepare(writer):
        await upsert_underlying(writer, nifty())
        result = await upsert_contracts(
            writer, underlying_id=1, expiry_date=EXPIRY, rows=chain_rows([23000])
        )
        contract_id = result.contract_ids["NSE:NIFTY25MAR23000CE"]
        await upsert_candle_chunk(
            writer,
            contract_id=contract_id,
            res_id=RES_ID,
            range_from=date(2025, 3, 26),
            range_to=date(2025, 3, 26),
            coverage=coverage(),
            batch=candles_to_arrow(
                session(date(2025, 3, 26), 6), COLUMNS, contract_id, RES_ID
            ),
        )
        return contract_id

    async def body(writer):
        contract_id = await prepare(writer)
        deleted = await delete_underlying(writer, 1, purge_data=True)
        return (contract_id, deleted)

    contract_id, deleted = run(store, body)
    assert deleted == 6
    con = store.connection
    assert con.execute("SELECT count(*) FROM dim_underlying").fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM candles WHERE contract_id = ?", [contract_id]
    ).fetchone()[0] == 0


def test_an_ingest_run_opens_and_closes(store):
    async def body(writer):
        run_id = await start_ingest_run(
            writer, job_id="job-1", job_kind="backfill", app_version="0.0.0-test"
        )
        await finish_ingest_run(
            writer, run_id, status="succeeded", requests_made=12, rows_written=340
        )
        return run_id

    run_id = run(store, body)
    row = store.connection.execute(
        "SELECT status, requests_made, rows_written, finished_at FROM ingest_run "
        "WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert row[0] == "succeeded"
    assert (row[1], row[2]) == (12, 340)
    assert row[3] is not None
