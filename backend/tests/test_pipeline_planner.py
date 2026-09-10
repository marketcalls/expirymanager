"""Planner arithmetic, against a real SQLite registry and a real DuckDB catalog.

Every later screen trusts these numbers, so the assertions here are about exact counts rather
than about shapes. Two properties get the most attention because they are the ones that cost real
money when they are wrong:

- A chunk already held is never requested again, and a chunk that is not held is never skipped.
- A plan costs zero Fyers requests. There is no client anywhere in this file, so a planner that
  reached for one would fail with an AttributeError rather than quietly pass.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from expirymanager.api.schemas.downloads import DownloadRequest, PlanRequest, StrikeScope
from expirymanager.brokers.fyers.calendar import MAX_DAYS_PER_REQUEST
from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.duck import DuckStore
from expirymanager.db.reader import DuckReader
from expirymanager.pipeline.planner import (
    BYTES_PER_ROW,
    DEFAULT_PER_MINUTE,
    Planner,
    PlannerError,
)

EXPIRY = date(2025, 3, 27)
TODAY = datetime(2025, 6, 1, 10, 0, 0)
NIFTY_SYMBOL = "NSE:NIFTY50-INDEX"


class FakeGovernor:
    """Only the four attributes the planner reads. No sockets, by construction."""

    def __init__(self, used: int = 0, daily: int = 100_000, per_minute: int = 170) -> None:
        self.requests_used = used
        self.daily_budget = daily
        self._per_minute = per_minute

    def snapshot(self):
        governor = self

        class _Snapshot:
            per_minute = governor._per_minute

        return _Snapshot()


class FakeSettings:
    def __init__(self, **values) -> None:
        self._values = {"budget_reserve_fraction": 0.70, "throttle_per_minute": 170}
        self._values.update(values)

    def get_int(self, key: str) -> int:
        return int(self._values[key])

    def get_float(self, key: str) -> float:
        return float(self._values[key])


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


def register_underlying(
    engine,
    *,
    underlying_id: int = 1,
    symbol: str | None = None,
    exchange: str = "NSE",
    data_from: date = date(2022, 1, 3),
    option_life_days: int = 200,
    future_life_days: int = 400,
    spot_contract_id: int = 1,
    is_active: bool = True,
) -> None:
    # Migration 0004 already seeds four builtin underlyings, so this replaces rather than
    # inserts. A test that quietly ran against the seeded row instead of its own would be
    # asserting somebody else's life window.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR REPLACE INTO underlying_registry (underlying_id, fyers_symbol, root,"
                " exchange,"
                " segment, instrument_kind, display_name, data_from, default_resolutions,"
                " include_oi, option_life_days, future_life_days, spot_contract_id, is_active,"
                " created_at, updated_at)"
                " VALUES (:id, :symbol, 'NIFTY', :exchange, 'CM', 'INDEX', 'Nifty 50',"
                " :data_from, '[\"1\"]', 1, :option_life, :future_life, :spot, :active,"
                " '2025-01-01T00:00:00.000000+00:00', '2025-01-01T00:00:00.000000+00:00')"
            ),
            {
                "id": underlying_id,
                "symbol": symbol or symbol_for(underlying_id),
                "exchange": exchange,
                "data_from": data_from.isoformat(),
                "option_life": option_life_days,
                "future_life": future_life_days,
                "spot": spot_contract_id,
                "active": 1 if is_active else 0,
            },
        )


def symbol_for(underlying_id: int) -> str:
    return NIFTY_SYMBOL if underlying_id == 1 else f"NSE:TESTU{underlying_id}-INDEX"


def clear_holidays(engine, exchange: str = "NSE") -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM market_holiday WHERE exchange = :exchange"),
            {"exchange": exchange},
        )


def seed_holidays(engine, exchange: str = "NSE", days: list[date] | None = None) -> None:
    with engine.begin() as connection:
        for day in days or [date(2025, 3, 14)]:
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO market_holiday (exchange, holiday_date, source)"
                    " VALUES (:exchange, :day, 'test')"
                ),
                {"exchange": exchange, "day": day.isoformat()},
            )


def seed_catalog(
    store,
    *,
    underlying_id: int = 1,
    exchange: str = "NSE",
    expiry: date = EXPIRY,
    strikes: tuple[int, ...] = (22900, 23000, 23100),
    rights: tuple[str, ...] = ("CE", "PE"),
    discovered: bool = True,
    with_future: bool = False,
    spot_contract_id: int = 1,
) -> list[int]:
    """Write dim_underlying, dim_expiry and dim_contract straight through a cursor."""
    cur = store.cursor()
    try:
        cur.execute(
            "INSERT INTO dim_underlying (underlying_id, fyers_symbol, root, exchange,"
            " exchange_code, segment, segment_code, instrument_kind, display_name,"
            " spot_contract_id, data_from, is_active, synced_at)"
            " VALUES (?, ?, 'NIFTY', ?, ?, 'CM', 10, 'INDEX', 'Nifty 50', ?, DATE '2022-01-03',"
            " TRUE, now()) ON CONFLICT (underlying_id) DO NOTHING",
            [
                underlying_id,
                symbol_for(underlying_id),
                exchange,
                10 if exchange == "NSE" else 12,
                spot_contract_id,
            ],
        )
        tag = expiry.strftime("%y%b").upper()
        contracts: list[tuple[int, str, str, float | None, str | None]] = []
        # Distinct id space per underlying AND per expiry, so seeding two expiries into one
        # store does not silently collide and leave the second one empty.
        contract_id = (
            1024 + underlying_id * 4096 + (expiry - date(2025, 1, 1)).days * 32
        )
        if with_future:
            contracts.append(
                (contract_id, f"NSE:NIFTY{tag}FUT{underlying_id}", "FUT", None, None)
            )
            contract_id += 1
        for strike in strikes:
            for right in rights:
                contracts.append(
                    (
                        contract_id,
                        f"NSE:NIFTY{tag}{strike}{right}-{underlying_id}",
                        "OPT",
                        float(strike),
                        right,
                    )
                )
                contract_id += 1
        cur.execute(
            "INSERT INTO dim_expiry (expiry_id, underlying_id, expiry_date,"
            " has_options, contract_count, options_count, contract_id_lo, contract_id_hi,"
            " discovered_at, contracts_discovered_at)"
            " VALUES (?, ?, ?, TRUE, ?, ?, ?, ?, now(), ?)"
            " ON CONFLICT (expiry_id) DO NOTHING",
            [
                underlying_id * 1000 + expiry.month * 32 + expiry.day,
                underlying_id,
                expiry,
                len(contracts),
                len(contracts),
                contracts[0][0] if contracts else None,
                contracts[-1][0] if contracts else None,
                datetime(2025, 4, 1, 0, 0, 0) if discovered else None,
            ],
        )
        for cid, symbol, kind, strike, right in contracts:
            cur.execute(
                "INSERT INTO dim_contract (contract_id, underlying_id, expiry_id,"
                " fyers_symbol, kind, instrument_class, exchange, exchange_code, segment,"
                " segment_code, root, expiry_date, strike, option_type, source_endpoint,"
                " parse_method, parse_confidence, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, 'OPTIDX', ?, ?, 'FO', 11, 'NIFTY', ?, ?, ?,"
                " 'expired-symbols', 'regex', 'high', now(), now())"
                " ON CONFLICT (contract_id) DO NOTHING",
                [
                    cid,
                    underlying_id,
                    underlying_id * 1000 + expiry.month * 32 + expiry.day,
                    symbol,
                    kind,
                    exchange,
                    10 if exchange == "NSE" else 12,
                    expiry,
                    strike,
                    right,
                ],
            )
        return [item[0] for item in contracts]
    finally:
        cur.close()


def seal(store, contract_id: int) -> None:
    cur = store.cursor()
    try:
        cur.execute(
            "UPDATE dim_contract SET sealed_at = now() WHERE contract_id = ?", [contract_id]
        )
    finally:
        cur.close()


def cover(
    store,
    contract_id: int,
    res_id: int,
    range_from: date,
    range_to: date,
    *,
    status: str = "ok",
    row_count: int = 1000,
) -> None:
    cur = store.cursor()
    try:
        cur.execute(
            "INSERT OR REPLACE INTO candle_coverage (contract_id, res_id, range_from, range_to,"
            " status, row_count, include_oi, columns_json, task_id, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, TRUE, '[]', 1, now())",
            [contract_id, res_id, range_from, range_to, status, row_count],
        )
    finally:
        cur.close()


def spot_bar(store, contract_id: int, moment: datetime, close: float) -> None:
    cur = store.cursor()
    try:
        cur.execute(
            "INSERT INTO candles (contract_id, res_id, ts, open, high, low, close, volume)"
            " VALUES (?, 2, ?, ?, ?, ?, ?, 0)",
            [contract_id, moment, close, close, close, close],
        )
    finally:
        cur.close()


def make_planner(reader, engine, *, used: int = 0, daily: int = 100_000) -> Planner:
    return Planner(
        reader=reader,
        engine=engine,
        settings=FakeSettings(),
        governor=FakeGovernor(used=used, daily=daily),
        clock=lambda: TODAY,
    )


def sheet(**overrides) -> PlanRequest:
    body = {
        "underlying_id": 1,
        "expiry_dates": [EXPIRY],
        "resolutions": ["1"],
        "instrument_class": "OPT",
    }
    body.update(overrides)
    return PlanRequest(**body)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The window and the chunker
# ---------------------------------------------------------------------------


def test_a_fresh_contract_plans_exactly_one_chunk_ending_on_the_expiry_date(
    engine, store, reader
):
    register_underlying(engine)
    seed_holidays(engine)
    contracts = seed_catalog(store)
    plan = run(make_planner(reader, engine).plan(sheet()))

    # Six contracts, one resolution, one chunk each. Backward probing is the whole point: the
    # 200 day life window would otherwise be three chunks per contract.
    assert plan.preview.tasks_total == len(contracts)
    assert {task.range_to for task in plan.tasks} == {EXPIRY}
    assert {task.range_from for task in plan.tasks} == {
        EXPIRY - timedelta(days=MAX_DAYS_PER_REQUEST - 1)
    }
    assert {task.kind for task in plan.tasks} == {"candle_chunk"}
    assert plan.preview.requests_estimated == len(contracts)


def test_the_exchange_floor_clamps_the_start_of_the_window(engine, store, reader):
    # A BSE expiry three weeks after the BSE floor. The 200 day life window reaches back into
    # 2023 where BSE serves nothing, and requesting it would be a guaranteed empty response that
    # still costs a governed request.
    early_expiry = date(2023, 8, 24)
    register_underlying(engine, underlying_id=2, exchange="BSE", data_from=date(2022, 1, 3))
    seed_catalog(store, underlying_id=2, exchange="BSE", expiry=early_expiry, strikes=(80000,))
    plan = run(
        make_planner(reader, engine).plan(
            sheet(underlying_id=2, expiry_dates=[early_expiry])
        )
    )
    assert plan.tasks
    assert {task.range_from for task in plan.tasks} == {date(2023, 8, 7)}


def test_the_registry_data_from_clamps_the_start_when_it_is_later_than_the_floor(
    engine, store, reader
):
    register_underlying(engine, data_from=date(2025, 1, 15))
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(make_planner(reader, engine).plan(sheet()))
    assert [task.range_from for task in plan.tasks] == [date(2025, 1, 15)]


def test_a_shorter_life_window_shortens_the_chunk(engine, store, reader):
    register_underlying(engine, option_life_days=10)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(make_planner(reader, engine).plan(sheet()))
    assert [(task.range_from, task.range_to) for task in plan.tasks] == [
        (EXPIRY - timedelta(days=10), EXPIRY)
    ]


def test_an_explicit_request_range_narrows_the_window_further(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(
        make_planner(reader, engine).plan(
            sheet(range_from=date(2025, 3, 1), range_to=date(2025, 3, 20))
        )
    )
    assert [(task.range_from, task.range_to) for task in plan.tasks] == [
        (date(2025, 3, 1), date(2025, 3, 20))
    ]


# ---------------------------------------------------------------------------
# Coverage subtraction and sealing
# ---------------------------------------------------------------------------


def test_an_all_covered_plan_costs_zero_tasks(engine, store, reader):
    # A ten day life window is exactly one chunk, so "all covered" means the contract's whole
    # tradeable life is held and there is nothing older left to probe for.
    register_underlying(engine, option_life_days=10)
    contracts = seed_catalog(store)
    for contract_id in contracts:
        cover(store, contract_id, 2, EXPIRY - timedelta(days=10), EXPIRY)
    plan = run(make_planner(reader, engine).plan(sheet()))

    assert plan.preview.tasks_total == 0
    assert plan.preview.requests_estimated == 0
    assert plan.preview.chunks_skipped_covered == len(contracts)
    assert plan.preview.eta_seconds == 0


def test_a_partially_covered_plan_skips_exactly_the_held_chunks(engine, store, reader):
    register_underlying(engine, option_life_days=10)
    contracts = seed_catalog(store)
    covered = contracts[:2]
    for contract_id in covered:
        cover(store, contract_id, 2, EXPIRY - timedelta(days=10), EXPIRY)
    plan = run(make_planner(reader, engine).plan(sheet()))

    assert plan.preview.chunks_skipped_covered == len(covered)
    assert plan.preview.tasks_total == len(contracts) - len(covered)
    assert {task.contract_id for task in plan.tasks} == set(contracts) - set(covered)


def test_a_covered_newest_chunk_moves_the_probe_one_chunk_older(engine, store, reader):
    register_underlying(engine)
    contracts = seed_catalog(store, strikes=(23000,), rights=("CE",))
    newest_from = EXPIRY - timedelta(days=MAX_DAYS_PER_REQUEST - 1)
    cover(store, contracts[0], 2, newest_from, EXPIRY)
    plan = run(make_planner(reader, engine).plan(sheet()))

    assert plan.preview.chunks_skipped_covered == 1
    assert len(plan.tasks) == 1
    assert plan.tasks[0].range_to == newest_from - timedelta(days=1)


def test_a_partially_covered_chunk_is_requested_whole_and_not_counted_as_skipped(
    engine, store, reader
):
    # One request either way, and re-requesting a covered day is idempotent at the writer, so the
    # chunk grid stays anchored on the expiry date instead of fragmenting into slivers.
    register_underlying(engine)
    contracts = seed_catalog(store, strikes=(23000,), rights=("CE",))
    cover(store, contracts[0], 2, EXPIRY - timedelta(days=10), EXPIRY)
    plan = run(make_planner(reader, engine).plan(sheet()))

    assert plan.preview.chunks_skipped_covered == 0
    assert [(task.range_from, task.range_to) for task in plan.tasks] == [
        (EXPIRY - timedelta(days=MAX_DAYS_PER_REQUEST - 1), EXPIRY)
    ]


def test_an_empty_chunk_stops_the_probe_because_older_data_cannot_exist(engine, store, reader):
    register_underlying(engine, option_life_days=400)
    contracts = seed_catalog(store, strikes=(23000,), rights=("CE",))
    newest_from = EXPIRY - timedelta(days=MAX_DAYS_PER_REQUEST - 1)
    cover(store, contracts[0], 2, newest_from, EXPIRY, status="empty", row_count=0)
    plan = run(make_planner(reader, engine).plan(sheet()))

    assert plan.tasks == ()
    assert plan.preview.chunks_skipped_covered == 1


def test_a_sealed_contract_is_skipped_and_counted(engine, store, reader):
    register_underlying(engine)
    contracts = seed_catalog(store)
    seal(store, contracts[0])
    seal(store, contracts[1])
    plan = run(make_planner(reader, engine).plan(sheet()))

    assert plan.preview.contracts_sealed_skipped == 2
    assert plan.preview.tasks_total == len(contracts) - 2
    assert plan.preview.contracts_planned == len(contracts) - 2


def test_force_refresh_ignores_both_the_seal_and_the_coverage(engine, store, reader):
    register_underlying(engine)
    contracts = seed_catalog(store)
    seal(store, contracts[0])
    for contract_id in contracts:
        cover(store, contract_id, 2, EXPIRY - timedelta(days=MAX_DAYS_PER_REQUEST - 1), EXPIRY)
    plan = run(make_planner(reader, engine).plan(sheet(force_refresh=True)))

    assert plan.preview.tasks_total == len(contracts)
    assert plan.preview.chunks_skipped_covered == 0
    assert plan.preview.contracts_sealed_skipped == 0


# ---------------------------------------------------------------------------
# Resolution availability
# ---------------------------------------------------------------------------


def test_a_second_resolution_outside_the_thirty_trading_day_window_plans_nothing(
    engine, store, reader
):
    register_underlying(engine)
    seed_holidays(engine)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(make_planner(reader, engine).plan(sheet(resolutions=["5S"])))

    assert plan.tasks == ()
    assert any(item.code == "seconds_window_closed" for item in plan.preview.warning_details)


def test_a_second_resolution_inside_the_window_is_planned(engine, store, reader):
    register_underlying(engine)
    seed_holidays(engine)
    recent = date(2025, 5, 29)
    seed_catalog(store, expiry=recent, strikes=(23000,), rights=("CE",))
    plan = run(
        make_planner(reader, engine).plan(
            sheet(expiry_dates=[recent], resolutions=["5S"])
        )
    )
    assert [task.resolution for task in plan.tasks] == ["5S"]


def test_the_seconds_window_degradation_is_visible_when_no_holidays_are_loaded(
    engine, store, reader
):
    register_underlying(engine)
    clear_holidays(engine)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(make_planner(reader, engine).plan(sheet()))
    codes = {item.code for item in plan.preview.warning_details}
    assert "holiday_calendar_empty" in codes


def test_an_unknown_resolution_is_refused_by_name(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store)
    with pytest.raises(PlannerError) as caught:
        run(make_planner(reader, engine).plan(sheet(resolutions=["7"])))
    assert caught.value.code == "unknown_resolution"
    assert caught.value.status_code == 400


# ---------------------------------------------------------------------------
# Scope filters
# ---------------------------------------------------------------------------


def test_the_instrument_class_and_option_type_filters_narrow_the_contract_set(
    engine, store, reader
):
    register_underlying(engine)
    seed_catalog(store, with_future=True)
    both = run(make_planner(reader, engine).plan(sheet(instrument_class="BOTH")))
    calls = run(
        make_planner(reader, engine).plan(sheet(instrument_class="OPT", option_types=["CE"]))
    )
    futures = run(make_planner(reader, engine).plan(sheet(instrument_class="FUT")))

    assert both.preview.tasks_total == 7
    assert calls.preview.tasks_total == 3
    assert futures.preview.tasks_total == 1
    assert futures.tasks[0].fyers_symbol.endswith("FUT1")


def test_an_explicit_strike_scope_selects_only_those_strikes(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store)
    plan = run(
        make_planner(reader, engine).plan(
            sheet(strike_scope=StrikeScope(mode="explicit", strikes=[23000]))
        )
    )
    assert plan.preview.tasks_total == 2
    assert all("23000" in task.fyers_symbol for task in plan.tasks)


def test_an_atm_band_without_spot_history_names_the_remedy(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store)
    with pytest.raises(PlannerError) as caught:
        run(
            make_planner(reader, engine).plan(
                sheet(strike_scope=StrikeScope(mode="atm_band", steps=1))
            )
        )
    assert caught.value.code == "strike_scope_needs_spot"
    assert caught.value.detail["remedy"] == "underlying_history"


def test_an_atm_band_uses_the_spot_close_on_the_expiry_day(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store, strikes=(22800, 22900, 23000, 23100, 23200))
    spot_bar(store, 1, datetime(2025, 3, 27, 15, 29), 23010.0)
    plan = run(
        make_planner(reader, engine).plan(
            sheet(strike_scope=StrikeScope(mode="atm_band", steps=1))
        )
    )
    # 23000 is the ATM strike, one step each side gives 22900, 23000 and 23100, two rights each.
    assert plan.preview.tasks_total == 6
    assert {task.fyers_symbol.split("MAR")[1][:5] for task in plan.tasks} == {
        "22900",
        "23000",
        "23100",
    }


# ---------------------------------------------------------------------------
# Discovery and the estimate behind it
# ---------------------------------------------------------------------------


def test_an_undiscovered_expiry_plans_one_discovery_task_and_estimates_behind_it(
    engine, store, reader
):
    register_underlying(engine)
    seed_catalog(store)
    later = date(2025, 4, 24)
    seed_catalog(store, expiry=later, strikes=(23000,), discovered=False)
    plan = run(make_planner(reader, engine).plan(sheet(expiry_dates=[EXPIRY, later])))

    discovery = [task for task in plan.tasks if task.kind == "underlying_symbols"]
    assert len(discovery) == 1
    assert discovery[0].expiry_date == later
    assert discovery[0].seq == 0
    # Six real chunks for the discovered expiry, plus six estimated behind the discovery.
    assert plan.preview.discovery_tasks == 1
    assert plan.preview.estimated_downstream_requests == 6
    assert plan.preview.tasks_total == 7
    assert plan.preview.requests_estimated == 13


def test_a_sheet_with_nothing_discovered_at_all_is_refused_with_the_discovery_count(
    engine, store, reader
):
    register_underlying(engine)
    seed_catalog(store, discovered=False)
    with pytest.raises(PlannerError) as caught:
        run(make_planner(reader, engine).plan(sheet()))
    assert caught.value.code == "no_contracts_discovered"
    assert caught.value.detail["discovery_tasks"] == 1


def test_an_expiry_absent_from_the_catalog_warns_rather_than_silently_vanishing(
    engine, store, reader
):
    register_underlying(engine)
    seed_catalog(store)
    plan = run(
        make_planner(reader, engine).plan(sheet(expiry_dates=[EXPIRY, date(2025, 4, 24)]))
    )
    assert any(item.code == "expiry_not_discovered" for item in plan.preview.warning_details)
    assert plan.preview.tasks_total == 6


# ---------------------------------------------------------------------------
# Preview arithmetic
# ---------------------------------------------------------------------------


def test_the_eta_uses_the_governors_real_per_minute_target(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store)
    plan = run(make_planner(reader, engine).plan(sheet()))
    expected = -(-plan.preview.requests_estimated * 60 // DEFAULT_PER_MINUTE)
    assert plan.preview.eta_seconds == expected


def test_bytes_follow_rows_at_the_measured_rate(engine, store, reader):
    register_underlying(engine)
    seed_holidays(engine)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(make_planner(reader, engine).plan(sheet()))
    assert plan.preview.rows_estimated > 0
    assert plan.preview.bytes_estimated == round(plan.preview.rows_estimated * BYTES_PER_ROW)


def test_observed_chunk_sizes_replace_the_nominal_row_estimate(engine, store, reader):
    register_underlying(engine, option_life_days=10)
    contracts = seed_catalog(store)
    # One contract already fetched, so the planner has a real observation to work from.
    cover(store, contracts[0], 2, EXPIRY - timedelta(days=10), EXPIRY, row_count=1500)
    plan = run(make_planner(reader, engine).plan(sheet()))

    assert plan.preview.tasks_total == 5
    assert plan.preview.rows_estimated == 5 * 1500
    assert not any(
        item.code == "rows_estimated_is_nominal" for item in plan.preview.warning_details
    )


def test_the_budget_fields_reflect_what_the_governor_has_already_spent(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store)
    plan = run(make_planner(reader, engine, used=12_400).plan(sheet()))

    assert plan.preview.budget_used_today == 12_400
    assert plan.preview.budget_remaining_today == 87_600
    assert plan.preview.budget_after == 87_600 - plan.preview.requests_estimated
    assert plan.preview.reserve_applied is False
    assert plan.preview.exceeds_budget is False


def test_a_plan_beyond_the_remaining_budget_says_so_with_the_reason(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store)
    plan = run(make_planner(reader, engine, used=99_998).plan(sheet()))

    assert plan.preview.exceeds_budget is True
    assert plan.preview.budget_allowance == 2
    assert any(item.code == "exceeds_budget" for item in plan.preview.warning_details)
    assert any("only 2 remain" in message for message in plan.preview.warnings)


def test_the_sweep_reserve_holds_back_a_share_of_the_day(engine, store, reader):
    # 70 percent of 100,000 is 70,000. A sweep that has already spent 69,998 has two requests of
    # allowance left even though 30,002 remain in the day, because the rest is reserved for
    # downloads the user starts by hand.
    register_underlying(engine)
    seed_catalog(store)
    plan = run(make_planner(reader, engine, used=69_998).plan(sheet(), sweep=True))

    assert plan.preview.reserve_applied is True
    assert plan.preview.budget_allowance == 2
    assert plan.preview.budget_remaining_today == 30_002
    assert plan.preview.exceeds_budget is True

    interactive = run(make_planner(reader, engine, used=69_998).plan(sheet()))
    assert interactive.preview.exceeds_budget is False


# ---------------------------------------------------------------------------
# Refusals and the spot leg
# ---------------------------------------------------------------------------


def test_mcx_is_refused_with_the_measured_reason(engine, store, reader):
    register_underlying(engine, underlying_id=3, exchange="MCX", symbol="MCX:CRUDEOIL-COM")
    with pytest.raises(PlannerError) as caught:
        run(make_planner(reader, engine).plan(sheet(underlying_id=3)))
    assert caught.value.code == "mcx_not_served"
    assert "422" in caught.value.message


def test_an_unregistered_underlying_is_a_404(engine, store, reader):
    with pytest.raises(PlannerError) as caught:
        run(make_planner(reader, engine).plan(sheet(underlying_id=99)))
    assert caught.value.status_code == 404


def test_the_spot_leg_plans_every_missing_chunk_because_a_spot_series_is_continuous(
    engine, store, reader
):
    register_underlying(engine)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(
        make_planner(reader, engine).plan(
            sheet(include_spot=True, range_from=date(2025, 1, 1))
        )
    )
    spot_tasks = [task for task in plan.tasks if task.kind == "spot_chunk"]
    assert plan.preview.spot_tasks == len(spot_tasks) == 1
    assert spot_tasks[0].contract_id == 1
    assert spot_tasks[0].include_oi is False
    assert (spot_tasks[0].range_from, spot_tasks[0].range_to) == (
        date(2025, 1, 1),
        EXPIRY,
    )


def test_probe_backward_off_emits_every_chunk_of_the_life_window(engine, store, reader):
    register_underlying(engine, option_life_days=200)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(make_planner(reader, engine).plan(sheet(), probe_backward=False))
    # 201 inclusive days at 100 days per request is three chunks.
    assert len(plan.tasks) == 3
    assert plan.tasks[0].range_to == EXPIRY
    assert plan.tasks[-1].range_from == EXPIRY - timedelta(days=200)


def test_a_plan_never_reaches_the_network(engine, store, reader):
    # The planner is constructed with no client at all, so the only way this passes is if every
    # number came from local state.
    register_underlying(engine)
    seed_catalog(store)
    planner = Planner(reader=reader, engine=engine, clock=lambda: TODAY)
    plan = run(planner.plan(sheet()))
    assert plan.preview.tasks_total == 6
    assert plan.preview.budget_remaining_today == 100_000


def test_the_request_params_carry_the_documented_history_parameters(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(make_planner(reader, engine).plan(sheet()))
    params = plan.tasks[0].request_params
    assert params["date_format"] == 1
    assert params["oi_flag"] == 1
    assert params["resolution"] == "1"
    assert params["range_to"] == EXPIRY.isoformat()


def test_a_download_request_is_a_plan_request_and_carries_the_gate(engine, store, reader):
    body = DownloadRequest(
        underlying_id=1, expiry_dates=[EXPIRY], resolutions=["1"], confirm_requests=6
    )
    assert isinstance(body, PlanRequest)
    assert body.confirm_requests == 6
    assert body.defer_to_tomorrow is False


@pytest.mark.parametrize(
    "scope",
    [
        {"mode": "atm_band"},
        {"mode": "explicit"},
        {"mode": "all", "steps": 3},
        {"mode": "all", "strikes": [23000]},
    ],
)
def test_a_malformed_strike_scope_is_refused_at_the_edge(scope):
    with pytest.raises(ValueError):
        StrikeScope(**scope)


def test_the_spot_leg_skips_a_chunk_it_already_holds(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    cover(store, 1, 2, date(2025, 1, 1), EXPIRY)
    plan = run(
        make_planner(reader, engine).plan(
            sheet(include_spot=True, range_from=date(2025, 1, 1))
        )
    )
    assert plan.preview.spot_tasks == 0
    assert plan.preview.chunks_skipped_covered >= 1


def test_the_spot_leg_emits_older_chunks_first(engine, store, reader):
    register_underlying(engine)
    seed_catalog(store, strikes=(23000,), rights=("CE",))
    plan = run(
        make_planner(reader, engine).plan(
            sheet(include_spot=True, range_from=date(2024, 6, 1))
        )
    )
    spot_tasks = [task for task in plan.tasks if task.kind == "spot_chunk"]
    assert len(spot_tasks) > 1
    assert spot_tasks[0].range_from < spot_tasks[-1].range_from
    assert spot_tasks[-1].range_to == EXPIRY


def test_a_seconds_window_never_reaches_back_past_the_thirty_trading_days(
    engine, store, reader
):
    # 5S data is retained for 30 trading days only. Without this clamp the 200 day life window
    # would emit seven 30 day chunks of guaranteed empty responses per contract per run.
    register_underlying(engine, option_life_days=200)
    recent = date(2025, 5, 29)
    seed_catalog(store, expiry=recent, strikes=(23000,), rights=("CE",))
    planner = make_planner(reader, engine)
    plan = run(planner.plan(sheet(expiry_dates=[recent], resolutions=["5S"])))

    calendar, _ = planner._load_calendar("NSE")
    window_start, _ = calendar.seconds_window("NSE", TODAY.date())
    # One chunk, because backward probing emits only the newest. It starts no earlier than the
    # window and spans no more than the 30 calendar days ref_resolution allows for 5S, where the
    # 30 trading day window is 42 calendar days wide.
    assert len(plan.tasks) == 1
    assert plan.tasks[0].range_to == recent
    assert plan.tasks[0].range_from >= window_start
    assert (plan.tasks[0].range_to - plan.tasks[0].range_from).days == 29

    full = run(
        planner.plan(
            sheet(expiry_dates=[recent], resolutions=["5S"]), probe_backward=False
        )
    )
    assert min(task.range_from for task in full.tasks) == window_start


def test_a_seconds_resolution_chunks_at_its_own_smaller_limit(engine, store, reader):
    register_underlying(engine)
    planner = make_planner(reader, engine)
    specs = {item.fyers_code: item for item in planner._load_resolutions(["5S", "1"])}
    assert specs["5S"].chunk_days == 30
    assert specs["1"].chunk_days == MAX_DAYS_PER_REQUEST
