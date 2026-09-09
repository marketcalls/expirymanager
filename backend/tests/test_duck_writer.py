"""Single writer tests: transaction discipline and idempotent convergence.

These tests use asyncio.run rather than an async test plugin so they stand alone, ahead of the
shared conftest that the verification work item owns.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from expirymanager.db.arrow import IST_OFFSET_SECONDS, candles_to_arrow
from expirymanager.db.duck import DuckStore
from expirymanager.db.writer import (
    CallableWrite,
    CandleChunkWrite,
    CoverageRow,
    WriterError,
    window_bounds,
)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
COLUMNS_JSON = '["timestamp","open","high","low","close","volume","open_interest"]'

CONTRACT = 1024
RES_ID = 2


@pytest.fixture
def store(tmp_path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    yield store
    store.close()


def utc_epoch_for_ist(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp()) - IST_OFFSET_SECONDS


def session_rows(day: date, bars: int, price: float = 100.0):
    """One synthetic trading session, one bar a minute from 09:15 IST."""
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


def chunk(
    store: DuckStore,
    range_from: date,
    range_to: date,
    days,
    bars: int = 5,
    price: float = 100.0,
    digest: str | None = None,
    contract_id: int = CONTRACT,
):
    rows = []
    for day in days:
        rows.extend(session_rows(day, bars, price))
    batch = candles_to_arrow(rows, COLUMNS, contract_id=contract_id, res_id=RES_ID)
    return CandleChunkWrite(
        contract_id=contract_id,
        res_id=RES_ID,
        range_from=range_from,
        range_to=range_to,
        batch=batch,
        coverage=CoverageRow(
            status="ok",
            include_oi=True,
            columns_json=COLUMNS_JSON,
            task_id=1,
            payload_sha256=digest,
            http_status=200,
        ),
    )


async def run_ops(store: DuckStore, *ops):
    await store.writer.start()
    try:
        results = []
        for op in ops:
            results.append(await store.writer.submit(op))
        return results
    finally:
        await store.writer.stop()


def count_rows(store: DuckStore, contract_id: int = CONTRACT) -> int:
    return store.connection.execute(
        "SELECT count(*) FROM candles WHERE contract_id = ?", [contract_id]
    ).fetchone()[0]


def test_window_bounds_are_half_open_on_the_right():
    start, end = window_bounds(date(2025, 3, 1), date(2025, 3, 5))
    assert start == datetime(2025, 3, 1, 0, 0, 0)
    assert end == datetime(2025, 3, 6, 0, 0, 0)


def test_window_bounds_reject_a_reversed_range():
    with pytest.raises(ValueError):
        window_bounds(date(2025, 3, 5), date(2025, 3, 1))


def test_single_chunk_writes_rows_coverage_and_bounds(store):
    days = [date(2025, 3, 3), date(2025, 3, 4)]
    result = asyncio.run(
        run_ops(store, chunk(store, date(2025, 3, 1), date(2025, 3, 5), days))
    )[0]

    assert result.rows_written == 10
    assert result.skipped is False
    assert count_rows(store) == 10

    coverage = store.connection.execute(
        "SELECT status, row_count, first_ts, last_ts, include_oi, http_status "
        "FROM candle_coverage WHERE contract_id = ?",
        [CONTRACT],
    ).fetchone()
    assert coverage[0] == "ok"
    assert coverage[1] == 10
    assert coverage[2] == datetime(2025, 3, 3, 9, 15)
    assert coverage[3] == datetime(2025, 3, 4, 9, 19)
    assert coverage[4] is True
    assert coverage[5] == 200

    bounds = store.connection.execute(
        "SELECT first_ts, last_ts, row_count FROM contract_bounds "
        "WHERE contract_id = ? AND res_id = ?",
        [CONTRACT, RES_ID],
    ).fetchone()
    assert bounds == (datetime(2025, 3, 3, 9, 15), datetime(2025, 3, 4, 9, 19), 10)


def test_writing_the_same_chunk_twice_leaves_the_row_count_unchanged(store):
    days = [date(2025, 3, 3), date(2025, 3, 4)]
    first = chunk(store, date(2025, 3, 1), date(2025, 3, 5), days)
    second = chunk(store, date(2025, 3, 1), date(2025, 3, 5), days)
    results = asyncio.run(run_ops(store, first, second))

    assert count_rows(store) == 10
    assert results[1].rows_deleted == 10
    assert store.connection.execute(
        "SELECT count(*) FROM candle_coverage WHERE contract_id = ?", [CONTRACT]
    ).fetchone()[0] == 1
    duplicates = store.connection.execute(
        "SELECT count(*) FROM (SELECT contract_id, res_id, ts FROM candles "
        "GROUP BY 1,2,3 HAVING count(*) > 1)"
    ).fetchone()[0]
    assert duplicates == 0


def test_a_shorter_chunk_removes_the_surplus_rows(store):
    window = (date(2025, 3, 1), date(2025, 3, 5))
    long_chunk = chunk(store, *window, [date(2025, 3, 3), date(2025, 3, 4)], bars=5)
    short_chunk = chunk(store, *window, [date(2025, 3, 3)], bars=3)
    asyncio.run(run_ops(store, long_chunk, short_chunk))

    assert count_rows(store) == 3
    last_ts = store.connection.execute(
        "SELECT max(ts) FROM candles WHERE contract_id = ?", [CONTRACT]
    ).fetchone()[0]
    assert last_ts == datetime(2025, 3, 3, 9, 17)

    bounds = store.connection.execute(
        "SELECT row_count, last_ts FROM contract_bounds WHERE contract_id = ?", [CONTRACT]
    ).fetchone()
    assert bounds == (3, datetime(2025, 3, 3, 9, 17))


def test_corrections_replace_values_in_place(store):
    window = (date(2025, 3, 1), date(2025, 3, 5))
    original = chunk(store, *window, [date(2025, 3, 3)], bars=2, price=100.0)
    corrected = chunk(store, *window, [date(2025, 3, 3)], bars=2, price=200.0)
    asyncio.run(run_ops(store, original, corrected))

    opens = [
        row[0]
        for row in store.connection.execute(
            "SELECT open FROM candles WHERE contract_id = ? ORDER BY ts", [CONTRACT]
        ).fetchall()
    ]
    assert [str(value) for value in opens] == ["200.0000", "200.0000"]


def test_adjacent_chunks_do_not_clobber_each_other_at_the_seam(store):
    # The classic chunked backfill bug: an inclusive right edge deletes the first day of the
    # next chunk, or an exclusive left edge leaves the last day of the previous one duplicated.
    first = chunk(store, date(2025, 3, 1), date(2025, 3, 5), [date(2025, 3, 5)], bars=4)
    second = chunk(store, date(2025, 3, 6), date(2025, 3, 10), [date(2025, 3, 6)], bars=4)
    asyncio.run(run_ops(store, first, second))
    assert count_rows(store) == 8

    # Re-running the second chunk must not touch the last day of the first.
    again = chunk(store, date(2025, 3, 6), date(2025, 3, 10), [date(2025, 3, 6)], bars=4)
    asyncio.run(run_ops(store, again))
    assert count_rows(store) == 8

    days = [
        row[0]
        for row in store.connection.execute(
            "SELECT DISTINCT CAST(ts AS DATE) FROM candles WHERE contract_id = ? ORDER BY 1",
            [CONTRACT],
        ).fetchall()
    ]
    assert days == [date(2025, 3, 5), date(2025, 3, 6)]


def test_a_no_data_response_records_coverage_and_clears_the_window(store):
    window = (date(2025, 3, 1), date(2025, 3, 5))
    asyncio.run(run_ops(store, chunk(store, *window, [date(2025, 3, 3)], bars=4)))
    assert count_rows(store) == 4

    empty = CandleChunkWrite(
        contract_id=CONTRACT,
        res_id=RES_ID,
        range_from=window[0],
        range_to=window[1],
        batch=None,
        coverage=CoverageRow(
            status="empty", include_oi=True, columns_json=COLUMNS_JSON, task_id=2
        ),
    )
    result = asyncio.run(run_ops(store, empty))[0]

    assert result.rows_written == 0
    assert result.rows_deleted == 4
    assert count_rows(store) == 0
    assert store.connection.execute(
        "SELECT status, row_count FROM candle_coverage WHERE contract_id = ?", [CONTRACT]
    ).fetchone() == ("empty", 0)
    # No bars left, so the health check must still be able to see this contract as uncovered.
    assert store.connection.execute(
        "SELECT count(*) FROM contract_bounds WHERE contract_id = ?", [CONTRACT]
    ).fetchone()[0] == 0


def test_identical_payload_hash_short_circuits_the_write(store):
    window = (date(2025, 3, 1), date(2025, 3, 5))
    days = [date(2025, 3, 3)]
    first = chunk(store, *window, days, bars=4, digest="a" * 64)
    second = chunk(store, *window, days, bars=4, digest="a" * 64)
    results = asyncio.run(run_ops(store, first, second))

    assert results[0].skipped is False
    assert results[1].skipped is True
    assert results[1].rows_deleted == 0
    assert count_rows(store) == 4


def test_a_changed_payload_hash_rewrites(store):
    window = (date(2025, 3, 1), date(2025, 3, 5))
    first = chunk(store, *window, [date(2025, 3, 3)], bars=4, digest="a" * 64)
    second = chunk(store, *window, [date(2025, 3, 3)], bars=2, digest="b" * 64)
    results = asyncio.run(run_ops(store, first, second))

    assert results[1].skipped is False
    assert count_rows(store) == 2


def test_other_contracts_are_untouched(store):
    window = (date(2025, 3, 1), date(2025, 3, 5))
    mine = chunk(store, *window, [date(2025, 3, 3)], bars=4)
    neighbour = chunk(store, *window, [date(2025, 3, 3)], bars=6, contract_id=CONTRACT + 1)
    asyncio.run(run_ops(store, mine, neighbour, chunk(store, *window, [date(2025, 3, 3)], bars=1)))

    assert count_rows(store, CONTRACT) == 1
    assert count_rows(store, CONTRACT + 1) == 6


def test_a_failing_op_rolls_back_and_the_writer_survives(store):
    def explode(cur):
        cur.execute("BEGIN TRANSACTION")
        cur.execute(
            "INSERT INTO candles VALUES "
            "(9999, 2, TIMESTAMP '2025-03-03 09:15:00', 1, 1, 1, 1, 1, NULL)"
        )
        cur.execute("ROLLBACK")
        raise RuntimeError("synthetic failure")

    async def scenario():
        await store.writer.start()
        try:
            with pytest.raises(RuntimeError):
                await store.writer.submit(CallableWrite("explode", explode))
            good = chunk(store, date(2025, 3, 1), date(2025, 3, 5), [date(2025, 3, 3)], bars=2)
            return await store.writer.submit(good)
        finally:
            await store.writer.stop()

    result = asyncio.run(scenario())
    assert result.rows_written == 2
    assert count_rows(store, 9999) == 0
    assert count_rows(store) == 2


def test_submit_before_start_is_refused(store):
    async def scenario():
        await store.writer.submit(CallableWrite("noop", lambda cur: None))

    with pytest.raises(WriterError):
        asyncio.run(scenario())


def test_writes_are_serialised_through_one_task(store):
    """Concurrent submitters converge, and every chunk lands exactly once."""

    async def scenario():
        await store.writer.start()
        try:
            ops = [
                chunk(
                    store,
                    date(2025, 3, 1),
                    date(2025, 3, 5),
                    [date(2025, 3, 3)],
                    bars=4,
                    contract_id=CONTRACT + offset,
                )
                for offset in range(20)
            ]
            await asyncio.gather(*(store.writer.submit(op) for op in ops))
        finally:
            await store.writer.stop()

    asyncio.run(scenario())
    total = store.connection.execute("SELECT count(*) FROM candles").fetchone()[0]
    assert total == 80
    duplicates = store.connection.execute(
        "SELECT count(*) FROM (SELECT contract_id, res_id, ts FROM candles "
        "GROUP BY 1,2,3 HAVING count(*) > 1)"
    ).fetchone()[0]
    assert duplicates == 0


def test_bars_macro_reads_what_the_writer_wrote(store):
    window = (date(2025, 3, 1), date(2025, 3, 5))
    asyncio.run(run_ops(store, chunk(store, *window, [date(2025, 3, 3)], bars=4)))
    rows = store.connection.execute(
        "SELECT * FROM bars(?, ?, TIMESTAMP '2025-03-03 00:00:00', "
        "TIMESTAMP '2025-03-04 00:00:00')",
        [CONTRACT, RES_ID],
    ).fetchall()
    assert len(rows) == 4
    assert rows[0][0] == datetime(2025, 3, 3, 9, 15)


def test_reader_sees_committed_rows(store):
    window = (date(2025, 3, 1), date(2025, 3, 5))

    async def scenario():
        await store.writer.start()
        try:
            await store.writer.submit(chunk(store, *window, [date(2025, 3, 3)], bars=4))
            return await store.reader.fetch_value(
                "SELECT count(*) FROM candles WHERE contract_id = ?", [CONTRACT]
            )
        finally:
            await store.writer.stop()

    assert asyncio.run(scenario()) == 4
