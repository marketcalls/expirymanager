"""Tests for the trading calendar, exchange floors and request windowing."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from expirymanager.brokers.fyers.calendar import (  # noqa: E402
    EXCHANGE_DATA_FLOOR,
    IST,
    MAX_DAYS_PER_REQUEST,
    SECONDS_RESOLUTIONS,
    SECONDS_WINDOW_TRADING_DAYS,
    TradingCalendar,
    chunk_range,
    clamp_to_floor,
    exchange_data_floor,
    ist_day_bounds,
    ist_epoch_bounds,
    partial_candle_cutoff,
    resolution_seconds,
)

UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------- exchange floors


@pytest.mark.parametrize(
    "exchange,floor",
    [("NSE", date(2022, 1, 3)), ("BSE", date(2023, 8, 7)), ("MCX", date(2022, 1, 3))],
)
def test_exchange_data_floors(exchange, floor):
    assert exchange_data_floor(exchange) == floor
    assert EXCHANGE_DATA_FLOOR[exchange] == floor


def test_floor_lookup_is_case_insensitive_and_rejects_unknown():
    assert exchange_data_floor(" nse ") == date(2022, 1, 3)
    with pytest.raises(ValueError):
        exchange_data_floor("NYSE")


def test_clamp_to_floor_moves_an_early_start_forward():
    assert clamp_to_floor("BSE", date(2022, 1, 3)) == date(2023, 8, 7)
    assert clamp_to_floor("BSE", date(2024, 5, 1)) == date(2024, 5, 1)
    assert clamp_to_floor("NSE", date(2021, 12, 31)) == date(2022, 1, 3)


def test_bse_floor_is_later_than_nse():
    """A BSE backfill started at the NSE floor burns budget on guaranteed empty responses."""
    assert exchange_data_floor("BSE") > exchange_data_floor("NSE")


# ---------------------------------------------------------------- resolutions


@pytest.mark.parametrize(
    "code,seconds",
    [("5S", 5), ("1", 60), ("15", 900), ("60", 3600), ("240", 14400), ("1D", 86400)],
)
def test_resolution_seconds(code, seconds):
    assert resolution_seconds(code) == seconds


def test_unknown_resolution_is_refused():
    with pytest.raises(ValueError):
        resolution_seconds("7")


def test_second_resolutions_are_named():
    assert "5S" in SECONDS_RESOLUTIONS
    assert "1" not in SECONDS_RESOLUTIONS


# ---------------------------------------------------------------- IST day boundaries


def test_ist_day_bounds_are_half_open():
    start, end = ist_day_bounds(date(2025, 1, 9))
    assert start == datetime(2025, 1, 9, 0, 0, tzinfo=IST)
    assert end == datetime(2025, 1, 10, 0, 0, tzinfo=IST)
    assert end - start == timedelta(days=1)


def test_ist_midnight_is_the_previous_day_in_utc():
    """IST is UTC plus five and a half hours, so an IST day starts at 18:30 UTC the day before."""
    start, _ = ist_day_bounds(date(2025, 1, 9))
    assert start.astimezone(UTC) == datetime(2025, 1, 8, 18, 30, tzinfo=UTC)
    assert int(start.timestamp()) == 1736361000


def test_ist_epoch_bounds_span_whole_days():
    lo, hi = ist_epoch_bounds(date(2025, 1, 9), date(2025, 1, 9))
    assert hi - lo == 86400
    lo, hi = ist_epoch_bounds(date(2025, 1, 9), date(2025, 1, 11))
    assert hi - lo == 3 * 86400


def test_ist_epoch_bounds_reject_a_reversed_range():
    with pytest.raises(ValueError):
        ist_epoch_bounds(date(2025, 1, 11), date(2025, 1, 9))


# ---------------------------------------------------------------- partial candle offset


def test_partial_candle_cutoff_matches_the_vendor_example():
    """The vendor asks at 12:10:20 and requests to 12:09:20 for completed one minute bars."""
    now = datetime(2025, 3, 26, 12, 10, 20, tzinfo=IST)
    assert partial_candle_cutoff("1", now) == datetime(2025, 3, 26, 12, 9, 20, tzinfo=IST)


@pytest.mark.parametrize("code", ["5S", "1", "15", "60", "240"])
def test_partial_candle_cutoff_backs_off_exactly_one_period(code):
    now = datetime(2025, 3, 26, 12, 10, 20, tzinfo=IST)
    assert now - partial_candle_cutoff(code, now) == timedelta(seconds=resolution_seconds(code))


def test_partial_candle_cutoff_converts_to_ist():
    now = datetime(2025, 3, 26, 6, 40, 20, tzinfo=UTC)
    cutoff = partial_candle_cutoff("1", now)
    assert cutoff.tzinfo is IST
    assert cutoff == datetime(2025, 3, 26, 12, 9, 20, tzinfo=IST)


# ---------------------------------------------------------------- the 95 day chunker


def test_chunker_default_is_95_days():
    assert MAX_DAYS_PER_REQUEST == 95


def test_chunk_of_exactly_the_limit_is_one_request():
    end = date(2025, 3, 27)
    start = end - timedelta(days=94)
    assert chunk_range(start, end) == [(start, end)]


def test_one_day_past_the_limit_becomes_two_chunks():
    end = date(2025, 3, 27)
    start = end - timedelta(days=95)
    chunks = chunk_range(start, end)
    assert chunks == [(date(2024, 12, 23), end), (start, date(2024, 12, 22))]


def test_chunk_seams_are_contiguous_and_cover_the_range_exactly():
    start, end = date(2022, 1, 3), date(2026, 9, 9)
    chunks = chunk_range(start, end, newest_first=False)
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for left, right in zip(chunks, chunks[1:]):
        assert right[0] - left[1] == timedelta(days=1)
    covered = sum((c[1] - c[0]).days + 1 for c in chunks)
    assert covered == (end - start).days + 1


def test_every_chunk_is_within_the_limit():
    chunks = chunk_range(date(2022, 1, 3), date(2026, 9, 9))
    assert all((c[1] - c[0]).days + 1 <= MAX_DAYS_PER_REQUEST for c in chunks)


def test_chunker_is_newest_first_by_default():
    """The planner probes backwards from the expiry, so the first chunk ends on it."""
    chunks = chunk_range(date(2024, 1, 1), date(2025, 1, 9))
    assert chunks[0][1] == date(2025, 1, 9)
    assert chunks == list(reversed(chunk_range(date(2024, 1, 1), date(2025, 1, 9), newest_first=False)))


def test_single_day_range():
    assert chunk_range(date(2025, 1, 9), date(2025, 1, 9)) == [(date(2025, 1, 9),) * 2]


def test_reversed_range_yields_nothing():
    assert chunk_range(date(2025, 1, 10), date(2025, 1, 9)) == []


def test_chunker_rejects_a_non_positive_limit():
    with pytest.raises(ValueError):
        chunk_range(date(2025, 1, 1), date(2025, 2, 1), max_days=0)


def test_a_weekly_option_life_is_a_single_chunk():
    """The reason the planner probes backwards instead of emitting sixteen chunks."""
    expiry = date(2025, 1, 9)
    assert len(chunk_range(expiry - timedelta(days=60), expiry)) == 1


# ---------------------------------------------------------------- trading days


@pytest.fixture
def cal():
    # Republic Day and Holi 2026, both weekdays, plus a BSE-only date to prove the buckets
    # are per exchange.
    return TradingCalendar(
        {
            "NSE": [date(2026, 1, 26), date(2026, 3, 4)],
            "BSE": [date(2026, 1, 26), date(2026, 3, 4), date(2026, 9, 8)],
        }
    )


def test_weekends_are_never_trading_days(cal):
    assert not cal.is_trading_day("NSE", date(2026, 9, 5))
    assert not cal.is_trading_day("NSE", date(2026, 9, 6))
    assert cal.is_trading_day("NSE", date(2026, 9, 7))


def test_holidays_are_per_exchange(cal):
    assert cal.is_trading_day("NSE", date(2026, 9, 8))
    assert not cal.is_trading_day("BSE", date(2026, 9, 8))
    assert not cal.is_trading_day("NSE", date(2026, 1, 26))


def test_unknown_exchange_is_refused(cal):
    with pytest.raises(ValueError):
        cal.is_trading_day("NYSE", date(2026, 9, 9))


def test_trading_days_skips_weekends_and_holidays(cal):
    days = cal.trading_days("NSE", date(2026, 1, 23), date(2026, 1, 28))
    assert days == [date(2026, 1, 23), date(2026, 1, 27), date(2026, 1, 28)]


def test_count_trading_days(cal):
    assert cal.count_trading_days("NSE", date(2026, 9, 7), date(2026, 9, 11)) == 5
    assert cal.count_trading_days("BSE", date(2026, 9, 7), date(2026, 9, 11)) == 4


def test_next_and_previous_trading_day(cal):
    assert cal.next_trading_day("NSE", date(2026, 9, 4)) == date(2026, 9, 7)
    assert cal.previous_trading_day("NSE", date(2026, 9, 7)) == date(2026, 9, 4)
    assert cal.next_trading_day("NSE", date(2026, 9, 7), inclusive=True) == date(2026, 9, 7)
    assert cal.previous_trading_day("NSE", date(2026, 9, 5), inclusive=True) == date(2026, 9, 4)
    assert cal.previous_trading_day("BSE", date(2026, 9, 8), inclusive=True) == date(2026, 9, 7)


def test_shift_trading_days(cal):
    assert cal.shift_trading_days("NSE", date(2026, 9, 9), 0) == date(2026, 9, 9)
    assert cal.shift_trading_days("NSE", date(2026, 9, 9), -1) == date(2026, 9, 8)
    assert cal.shift_trading_days("NSE", date(2026, 9, 9), -2) == date(2026, 9, 7)
    assert cal.shift_trading_days("NSE", date(2026, 9, 9), -3) == date(2026, 9, 4)
    assert cal.shift_trading_days("BSE", date(2026, 9, 9), -1) == date(2026, 9, 7)
    assert cal.shift_trading_days("NSE", date(2026, 9, 11), 1) == date(2026, 9, 14)


def test_holidays_can_be_added_as_iso_strings():
    calendar = TradingCalendar()
    calendar.add_holidays("NSE", ["2026-01-26"])
    assert calendar.holidays("NSE") == frozenset({date(2026, 1, 26)})
    assert not calendar.is_trading_day("NSE", date(2026, 1, 26))


# ---------------------------------------------------------------- the seconds window


def test_seconds_window_spans_exactly_thirty_trading_days(cal):
    start, end = cal.seconds_window("NSE", date(2026, 9, 9))
    assert cal.count_trading_days("NSE", start, end) == SECONDS_WINDOW_TRADING_DAYS
    assert cal.is_trading_day("NSE", start)
    assert end == date(2026, 9, 9)


def test_seconds_window_on_a_weekday_only_calendar():
    """Thirty weekdays back from Wednesday 2026-09-09 is Thursday 2026-07-30."""
    calendar = TradingCalendar()
    assert calendar.seconds_window("NSE", date(2026, 9, 9)) == (
        date(2026, 7, 30),
        date(2026, 9, 9),
    )


def test_a_holiday_pushes_the_window_start_one_day_earlier():
    """More non-trading days inside the window means it reaches further back."""
    plain = TradingCalendar()
    # Thursday 2026-08-20, a weekday inside the window, so removing it costs a trading day.
    with_holiday = TradingCalendar({"NSE": [date(2026, 8, 20)]})
    assert with_holiday.seconds_window("NSE", date(2026, 9, 9))[0] == date(2026, 7, 29)
    assert plain.seconds_window("NSE", date(2026, 9, 9))[0] == date(2026, 7, 30)


def test_seconds_window_ends_on_the_last_trading_day_not_the_weekend(cal):
    _, end = cal.seconds_window("NSE", date(2026, 9, 6))
    assert end == date(2026, 9, 4)


def test_is_within_seconds_window(cal):
    start, end = cal.seconds_window("NSE", date(2026, 9, 9))
    assert cal.is_within_seconds_window("NSE", end, date(2026, 9, 9))
    assert cal.is_within_seconds_window("NSE", start, date(2026, 9, 9))
    assert not cal.is_within_seconds_window(
        "NSE", start - timedelta(days=1), date(2026, 9, 9)
    )


def test_an_old_expiry_is_outside_the_seconds_window(cal):
    """Seconds data does not exist for it, so requesting it burns budget for nothing."""
    assert not cal.is_within_seconds_window("NSE", date(2025, 1, 9), date(2026, 9, 9))
