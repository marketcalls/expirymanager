"""The read surface: bars, chain, ATM, the spot self join, catalog listings and coverage.

One fixture builds a small but realistic store: one underlying with spot bars, one expiry with
five strikes of calls and puts, and one minute bars for every one of them. Every query here is
then asserted against known values rather than against itself.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from expirymanager.db.arrow import IST_OFFSET_SECONDS, candles_to_arrow
from expirymanager.db.duck import DuckStore
from expirymanager.db.queries import bars as bars_q
from expirymanager.db.queries import catalog as catalog_q
from expirymanager.db.queries import coverage as coverage_q
from expirymanager.db.writer import CoverageRow
from expirymanager.db.writes import (
    ContractRow,
    UnderlyingRow,
    upsert_candle_chunk,
    upsert_contracts,
    upsert_underlying,
)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
COLUMNS_JSON = '["timestamp","open","high","low","close","volume","open_interest"]'

EXPIRY = date(2025, 3, 27)
DAY = date(2025, 3, 26)
RES_ID = 2
STRIKES = (22800, 22900, 23000, 23100, 23200)
SPOT_CLOSE = 23040.0
BARS = 6


def utc_epoch_for_ist(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp()) - IST_OFFSET_SECONDS


def bar_time(index: int) -> datetime:
    minute = 15 + index
    return datetime(DAY.year, DAY.month, DAY.day, 9 + minute // 60, minute % 60)


def rows_for(close: float, volume_base: int, oi_base: int):
    return [
        [
            utc_epoch_for_ist(bar_time(index)),
            close,
            close + 1,
            close - 1,
            close,
            volume_base + index,
            oi_base + index,
        ]
        for index in range(BARS)
    ]


def underlying_row() -> UnderlyingRow:
    return UnderlyingRow(
        underlying_id=1,
        fyers_symbol="NSE:NIFTY50-INDEX",
        root="NIFTY",
        exchange="NSE",
        exchange_code=10,
        segment="CM",
        segment_code=10,
        instrument_kind="INDEX",
        display_name="Nifty 50",
        data_from=date(2022, 1, 3),
    )


def option_row(strike: int, right: str) -> ContractRow:
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


def coverage_row(task_id: int) -> CoverageRow:
    return CoverageRow(
        status="ok", include_oi=True, columns_json=COLUMNS_JSON, task_id=task_id
    )


async def build(store) -> dict:
    # Start and stop inside one loop. The writer task binds to the loop that started it, so
    # stopping it from a second asyncio.run cancels rather than drains.
    await store.writer.start()
    try:
        return await _load(store, store.writer)
    finally:
        await store.writer.stop()


async def _load(store, writer) -> dict:
    registered = await upsert_underlying(writer, underlying_row())
    await upsert_candle_chunk(
        writer,
        contract_id=registered.spot_contract_id,
        res_id=RES_ID,
        range_from=DAY,
        range_to=DAY,
        coverage=coverage_row(1),
        batch=candles_to_arrow(
            rows_for(SPOT_CLOSE, 0, 0), COLUMNS, registered.spot_contract_id, RES_ID
        ),
    )
    contracts = await upsert_contracts(
        writer,
        underlying_id=1,
        expiry_date=EXPIRY,
        rows=[option_row(strike, right) for strike in STRIKES for right in ("CE", "PE")],
    )
    task_id = 2
    for strike in STRIKES:
        for right in ("CE", "PE"):
            symbol = f"NSE:NIFTY25MAR{strike}{right}"
            contract_id = contracts.contract_ids[symbol]
            close = float(abs(strike - 23000) // 100 + 1) * (10 if right == "CE" else 20)
            await upsert_candle_chunk(
                writer,
                contract_id=contract_id,
                res_id=RES_ID,
                range_from=DAY,
                range_to=DAY,
                coverage=coverage_row(task_id),
                batch=candles_to_arrow(
                    rows_for(close, strike, strike * 10), COLUMNS, contract_id, RES_ID
                ),
            )
            task_id += 1
    return {"spot_contract_id": registered.spot_contract_id, "contracts": contracts}


@pytest.fixture
def loaded(tmp_path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    context = asyncio.run(build(store))
    yield (store, context)
    store.close()


def query(store, coro_factory):
    return asyncio.run(coro_factory(store.reader))


# -- bars ------------------------------------------------------------------


def test_bars_come_back_in_fyers_column_order_and_utc_seconds(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    page = query(
        store,
        lambda reader: bars_q.bars(
            reader,
            contract_id=contract_id,
            res_id=RES_ID,
            start=datetime(2025, 3, 26),
            end=datetime(2025, 3, 27),
        ),
    )
    assert page.columns == bars_q.FYERS_COLUMNS
    assert page.row_count == BARS
    assert page.candles[0][0] == utc_epoch_for_ist(bar_time(0))
    assert page.candles[0][4] == pytest.approx(10.0)
    assert page.candles[-1][0] == utc_epoch_for_ist(bar_time(BARS - 1))


def test_the_window_is_half_open_on_the_right(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    page = query(
        store,
        lambda reader: bars_q.bars(
            reader,
            contract_id=contract_id,
            res_id=RES_ID,
            start=bar_time(0),
            end=bar_time(BARS - 1),
        ),
    )
    assert page.row_count == BARS - 1


def test_dropping_open_interest_drops_the_column_too(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    page = query(
        store,
        lambda reader: bars_q.bars(
            reader,
            contract_id=contract_id,
            res_id=RES_ID,
            start=datetime(2025, 3, 26),
            end=datetime(2025, 3, 27),
            include_oi=False,
        ),
    )
    assert page.columns == bars_q.FYERS_COLUMNS_NO_OI
    assert len(page.candles[0]) == 6


def test_bars_before_pages_backwards_and_returns_ascending(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    page = query(
        store,
        lambda reader: bars_q.bars_before(
            reader, contract_id=contract_id, res_id=RES_ID, before=bar_time(4), count=2
        ),
    )
    stamps = [row[0] for row in page.candles]
    assert stamps == [utc_epoch_for_ist(bar_time(2)), utc_epoch_for_ist(bar_time(3))]


def test_a_range_over_the_cap_is_refused_with_the_maximum_named(loaded, monkeypatch):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    monkeypatch.setattr(bars_q, "MAX_CANDLES_PER_RESPONSE", 2)
    with pytest.raises(bars_q.RangeTooLarge) as excinfo:
        query(
            store,
            lambda reader: bars_q.bars(
                reader,
                contract_id=contract_id,
                res_id=RES_ID,
                start=datetime(2025, 3, 26),
                end=datetime(2025, 3, 27),
            ),
        )
    assert excinfo.value.available == BARS


def test_the_open_interest_series_carries_only_points_that_have_it(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    points = query(
        store,
        lambda reader: bars_q.oi_series(
            reader,
            contract_id=contract_id,
            res_id=RES_ID,
            start=datetime(2025, 3, 26),
            end=datetime(2025, 3, 27),
        ),
    )
    assert len(points) == BARS
    assert points[0][1] == 23000 * 10


# -- the spot self join ----------------------------------------------------


def test_spot_bars_serve_through_the_same_path_as_a_contract(loaded):
    store, _ = loaded
    page = query(
        store,
        lambda reader: bars_q.spot_bars(
            reader,
            underlying_id=1,
            res_id=RES_ID,
            start=datetime(2025, 3, 26),
            end=datetime(2025, 3, 27),
        ),
    )
    assert page.contract_id == 1
    assert page.symbol == "NSE:NIFTY50-INDEX"
    assert page.row_count == BARS
    assert page.candles[0][4] == pytest.approx(SPOT_CLOSE)


def test_spot_at_reads_the_last_close_at_or_before_a_moment(loaded):
    store, _ = loaded
    value = query(
        store,
        lambda reader: bars_q.spot_at(
            reader, underlying_id=1, res_id=RES_ID, moment=bar_time(3)
        ),
    )
    assert value == pytest.approx(SPOT_CLOSE)


def test_spot_before_the_first_bar_has_no_value(loaded):
    store, _ = loaded
    value = query(
        store,
        lambda reader: bars_q.spot_at(
            reader, underlying_id=1, res_id=RES_ID, moment=datetime(2025, 3, 25)
        ),
    )
    assert value is None


# -- ATM and chain ---------------------------------------------------------


def test_atm_picks_the_strike_nearest_the_spot(loaded):
    store, context = loaded
    pick = query(
        store,
        lambda reader: bars_q.atm(
            reader,
            underlying_id=1,
            expiry_date=EXPIRY,
            res_id=RES_ID,
            moment=bar_time(3),
        ),
    )
    assert pick.spot == pytest.approx(SPOT_CLOSE)
    assert pick.atm_strike == pytest.approx(23000.0)
    assert pick.ce_contract_id == context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    assert pick.pe_contract_id == context["contracts"].contract_ids["NSE:NIFTY25MAR23000PE"]


def test_atm_moves_with_the_spot(loaded):
    store, _ = loaded
    store.connection.execute(
        "UPDATE candles SET close = 23180 WHERE contract_id = 1 AND ts = ?", [bar_time(3)]
    )
    pick = query(
        store,
        lambda reader: bars_q.atm(
            reader,
            underlying_id=1,
            expiry_date=EXPIRY,
            res_id=RES_ID,
            moment=bar_time(3),
        ),
    )
    assert pick.atm_strike == pytest.approx(23200.0)


def test_the_chain_returns_one_row_per_strike_carrying_both_rights(loaded):
    store, _ = loaded
    slice_ = query(
        store,
        lambda reader: bars_q.chain(
            reader,
            underlying_id=1,
            expiry_date=EXPIRY,
            res_id=RES_ID,
            moment=bar_time(2),
        ),
    )
    assert [row.strike for row in slice_.rows] == [float(s) for s in STRIKES]
    assert all(row.ce is not None and row.pe is not None for row in slice_.rows)
    assert all(row.lot_size == 75 for row in slice_.rows)
    assert slice_.atm_strike == pytest.approx(23000.0)
    assert slice_.spot == pytest.approx(SPOT_CLOSE)
    assert slice_.ts == utc_epoch_for_ist(bar_time(2))

    atm_row = next(row for row in slice_.rows if row.strike == 23000.0)
    assert atm_row.ce.close == pytest.approx(10.0)
    assert atm_row.pe.close == pytest.approx(20.0)
    assert atm_row.ce.fyers_symbol == "NSE:NIFTY25MAR23000CE"


def test_the_chain_at_a_timestamp_with_no_bar_is_empty_but_still_reports_spot(loaded):
    store, _ = loaded
    slice_ = query(
        store,
        lambda reader: bars_q.chain(
            reader,
            underlying_id=1,
            expiry_date=EXPIRY,
            res_id=RES_ID,
            moment=datetime(2025, 3, 26, 14, 0),
        ),
    )
    assert slice_.rows == []
    assert slice_.spot == pytest.approx(SPOT_CLOSE)


def test_the_chain_band_narrows_to_the_strikes_around_atm(loaded):
    store, _ = loaded
    rows = query(
        store,
        lambda reader: bars_q.chain_window(
            reader,
            underlying_id=1,
            expiry_date=EXPIRY,
            res_id=RES_ID,
            start=bar_time(0),
            end=bar_time(BARS),
            strikes_each_side=1,
        ),
    )
    strikes = sorted({row[1] for row in rows})
    assert strikes == [22900.0, 23000.0, 23100.0]


def test_the_daily_series_is_aggregated_from_one_minute_bars(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    rows = query(store, lambda reader: bars_q.daily_bars(reader, contract_id=contract_id))
    assert len(rows) == 1
    assert rows[0][0] == DAY
    assert rows[0][7] == BARS


# -- catalog ---------------------------------------------------------------


def test_the_underlying_listing_carries_its_spot_rollup(loaded):
    store, _ = loaded
    items = query(store, lambda reader: catalog_q.list_underlyings(reader))
    assert len(items) == 1
    row = items[0]
    assert row["fyers_symbol"] == "NSE:NIFTY50-INDEX"
    assert row["spot_bars"] == BARS
    assert row["spot_last_ts"] == bar_time(BARS - 1)
    assert row["expiry_count"] == 1
    assert row["contract_count"] == len(STRIKES) * 2


def test_the_expiry_listing_rolls_coverage_up_without_touching_candles(loaded):
    store, _ = loaded
    page = query(
        store,
        lambda reader: catalog_q.list_expiries(reader, underlying_id=1, res_id=RES_ID),
    )
    assert len(page.items) == 1
    row = page.items[0]
    assert row["expiry_date"] == EXPIRY
    assert row["contract_count"] == len(STRIKES) * 2
    assert row["contracts_with_data"] == len(STRIKES) * 2
    assert row["chunks_ok"] == len(STRIKES) * 2
    assert row["rows"] == len(STRIKES) * 2 * BARS


def test_the_contract_listing_filters_and_pages_on_the_id(loaded):
    store, _ = loaded
    first = query(
        store,
        lambda reader: catalog_q.list_contracts(
            reader,
            filters=catalog_q.ContractFilters(underlying_id=1, option_type="CE"),
            sort="strike",
            limit=2,
        ),
    )
    assert len(first.items) == 2
    assert first.next_cursor is not None
    assert [row["strike"] for row in first.items] == [22800.0, 22900.0]

    second = query(
        store,
        lambda reader: catalog_q.list_contracts(
            reader,
            filters=catalog_q.ContractFilters(underlying_id=1, option_type="CE"),
            sort="strike",
            limit=2,
            cursor=first.next_cursor,
        ),
    )
    assert [row["strike"] for row in second.items] == [23000.0, 23100.0]


def test_an_unknown_sort_column_is_refused(loaded):
    store, _ = loaded
    with pytest.raises(ValueError):
        query(
            store,
            lambda reader: catalog_q.list_contracts(reader, sort="strike; DROP TABLE candles"),
        )


def test_contract_bounds_leave_in_utc_seconds(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    result = query(store, lambda reader: catalog_q.contract_bounds(reader, contract_id))
    assert result["fyers_symbol"] == "NSE:NIFTY25MAR23000CE"
    resolution = result["resolutions"][0]
    assert resolution["fyers_code"] == "1"
    assert resolution["first_ts"] == utc_epoch_for_ist(bar_time(0))
    assert resolution["last_ts"] == utc_epoch_for_ist(bar_time(BARS - 1))
    assert resolution["rows"] == BARS


def test_a_contract_detail_carries_bounds_and_coverage(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000PE"]
    detail = query(store, lambda reader: catalog_q.get_contract(reader, contract_id))
    assert detail["fyers_symbol"] == "NSE:NIFTY25MAR23000PE"
    assert detail["underlying_symbol"] == "NSE:NIFTY50-INDEX"
    assert detail["resolutions"][0]["rows"] == BARS
    assert detail["coverage"][0]["chunks_ok"] == 1


# -- coverage --------------------------------------------------------------


def test_merge_ranges_joins_touching_chunks():
    ranges = [
        (date(2025, 1, 1), date(2025, 1, 10)),
        (date(2025, 1, 11), date(2025, 1, 20)),
        (date(2025, 2, 1), date(2025, 2, 5)),
    ]
    assert coverage_q.merge_ranges(ranges) == [
        (date(2025, 1, 1), date(2025, 1, 20)),
        (date(2025, 2, 1), date(2025, 2, 5)),
    ]


def test_missing_windows_finds_the_hole_in_the_middle():
    held = [(date(2025, 1, 1), date(2025, 1, 10)), (date(2025, 1, 21), date(2025, 1, 31))]
    assert coverage_q.missing_windows(held, date(2025, 1, 1), date(2025, 1, 31)) == [
        (date(2025, 1, 11), date(2025, 1, 20))
    ]


def test_missing_windows_covers_both_ends():
    held = [(date(2025, 1, 10), date(2025, 1, 20))]
    assert coverage_q.missing_windows(held, date(2025, 1, 1), date(2025, 1, 31)) == [
        (date(2025, 1, 1), date(2025, 1, 9)),
        (date(2025, 1, 21), date(2025, 1, 31)),
    ]


def test_a_fully_held_range_asks_for_nothing():
    held = [(date(2025, 1, 1), date(2025, 1, 31))]
    assert coverage_q.missing_windows(held, date(2025, 1, 5), date(2025, 1, 20)) == []


def test_missing_windows_splits_on_the_calendar_day_limit():
    pieces = coverage_q.missing_windows([], date(2025, 1, 1), date(2025, 4, 30), max_days=100)
    assert pieces == [
        (date(2025, 1, 1), date(2025, 4, 10)),
        (date(2025, 4, 11), date(2025, 4, 30)),
    ]
    for start, end in pieces:
        assert (end - start).days + 1 <= 100


def test_held_ranges_ignore_a_failed_chunk(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    store.connection.execute(
        "INSERT INTO candle_coverage VALUES (?, ?, DATE '2025-03-20', DATE '2025-03-21', "
        "'error', 0, NULL, NULL, TRUE, ?, NULL, NULL, 500, NULL, NULL, NULL, NULL, 99, NULL, "
        "now())",
        [contract_id, RES_ID, COLUMNS_JSON],
    )
    held = query(
        store,
        lambda reader: coverage_q.held_ranges(
            reader, contract_id=contract_id, res_id=RES_ID
        ),
    )
    assert [item.range_from for item in held] == [DAY]

    missing = query(
        store,
        lambda reader: coverage_q.missing_for_contract(
            reader,
            contract_id=contract_id,
            res_id=RES_ID,
            want_from=date(2025, 3, 20),
            want_to=DAY,
        ),
    )
    assert missing == [(date(2025, 3, 20), date(2025, 3, 25))]


def test_the_coverage_grid_reports_one_cell_per_expiry_and_resolution(loaded):
    store, _ = loaded
    cells = query(
        store, lambda reader: coverage_q.coverage_grid(reader, underlying_id=1, res_id=RES_ID)
    )
    assert len(cells) == 1
    cell = cells[0]
    assert cell["expiry_date"] == EXPIRY
    assert cell["contracts_covered"] == len(STRIKES) * 2
    assert cell["contracts_total"] == len(STRIKES) * 2
    assert cell["rows"] == len(STRIKES) * 2 * BARS


def test_the_per_contract_summary_orders_by_strike_and_right(loaded):
    store, _ = loaded
    rows = query(
        store,
        lambda reader: coverage_q.expiry_chunk_summary(
            reader, underlying_id=1, expiry_date=EXPIRY, res_id=RES_ID
        ),
    )
    assert [(row["strike"], row["option_type"]) for row in rows[:4]] == [
        (22800.0, "CE"),
        (22800.0, "PE"),
        (22900.0, "CE"),
        (22900.0, "PE"),
    ]
    assert all(row["rows_held"] == BARS for row in rows)


def test_a_contract_with_no_chunk_is_listed_for_the_planner(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR22800CE"]
    store.connection.execute(
        "DELETE FROM candle_coverage WHERE contract_id = ?", [contract_id]
    )
    rows = query(
        store,
        lambda reader: coverage_q.contracts_needing_data(
            reader, underlying_id=1, expiry_date=EXPIRY, res_id=RES_ID
        ),
    )
    assert [row["contract_id"] for row in rows] == [contract_id]


def test_a_gap_between_two_chunks_is_reported(loaded):
    store, context = loaded
    contract_id = context["contracts"].contract_ids["NSE:NIFTY25MAR23000CE"]
    store.connection.execute(
        "INSERT INTO candle_coverage VALUES (?, ?, DATE '2025-03-01', DATE '2025-03-05', "
        "'ok', 0, NULL, NULL, TRUE, ?, NULL, NULL, 200, NULL, NULL, NULL, NULL, 98, NULL, now())",
        [contract_id, RES_ID, COLUMNS_JSON],
    )
    gaps = query(
        store, lambda reader: coverage_q.coverage_gaps(reader, underlying_id=1, res_id=RES_ID)
    )
    assert len(gaps) == 1
    assert gaps[0]["gap_after"] == date(2025, 3, 5)
    assert gaps[0]["gap_before"] == DAY
