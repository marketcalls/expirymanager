"""IST trading days, exchange availability floors and request windowing.

Four things live here, all of them shared by the planner, the scheduler and the exporter:

1. The per-exchange data availability floor. Requesting history before it burns rate-limit
   budget on a guaranteed empty response.
2. Trading days, computed against the market_holiday table rather than against a weekday rule.
   NSE and BSE expiry weekdays have changed several times since 2022, so a hardcoded rule
   silently produces wrong dates for older data.
3. The 30 trading day window inside which second resolutions exist at all.
4. The 100 calendar day request chunker, and the half-open IST day boundaries that turn a date
   range into the UTC epoch bounds the candle table is keyed on.

Everything that reasons about a market day does it in Asia/Kolkata. Everything stored is UTC.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

__all__ = [
    "EXCHANGES",
    "EXCHANGE_DATA_FLOOR",
    "IST",
    "MAX_DAYS_PER_REQUEST",
    "RESOLUTION_SECONDS",
    "SECONDS_RESOLUTIONS",
    "SECONDS_WINDOW_TRADING_DAYS",
    "TradingCalendar",
    "chunk_range",
    "clamp_to_floor",
    "exchange_data_floor",
    "ist_day_bounds",
    "ist_epoch_bounds",
    "partial_candle_cutoff",
    "resolution_seconds",
]

IST = ZoneInfo("Asia/Kolkata")

EXCHANGES = ("NSE", "BSE", "MCX")

# The first date each exchange serves through the expired F&O endpoints. BSE is nearly twenty
# months later than NSE because BSE derivatives history only starts with the SENSEX weekly
# relaunch, so a BSE backfill that starts at the NSE floor wastes about 400 requests per
# contract on empty responses.
EXCHANGE_DATA_FLOOR: Mapping[str, date] = {
    "NSE": date(2022, 1, 3),
    "BSE": date(2023, 8, 7),
    "MCX": date(2022, 1, 3),
}

# 100, settled by probing the live API rather than inferred from the docs, which never say whether
# the documented limit counts calendar or trading days. Measured against
# NSE:NIFTY2541722900CE on 2026-09-09: a span of 100 calendar days between range_from and range_to
# answers 200, and 101 answers HTTP 422 with code -50 "Invalid input". So the limit is calendar
# days, the boundary is a hard error rather than a silent truncation, and the safe maximum is
# exactly 100. The earlier value of 95 bought margin against an ambiguity that no longer exists,
# at a cost of about five percent more requests: roughly 3,000 wasted calls on a full NIFTY
# backfill, against a daily budget of 100,000.
#
# Note the unit: chunk_range counts days INCLUSIVE, while the probe measured the span between
# range_from and range_to. The API accepted a span of 100, which is 101 inclusive days, so 100
# inclusive leaves exactly one day of margin. That margin is deliberate and nearly free: it costs
# under one percent of requests and covers the boundary being evaluated in a different timezone
# on their side, where being one day over is a hard 422 rather than a truncation.
MAX_DAYS_PER_REQUEST = 100

SECONDS_WINDOW_TRADING_DAYS = 30

# Mirrors ref_resolution. Kept here so the chunker and the partial candle offset do not need a
# database round trip inside a planning loop.
RESOLUTION_SECONDS: Mapping[str, int] = {
    "5S": 5, "10S": 10, "15S": 15, "30S": 30, "45S": 45,
    "1": 60, "2": 120, "3": 180, "5": 300, "10": 600, "15": 900, "20": 1200,
    "30": 1800, "45": 2700, "60": 3600, "120": 7200, "180": 10800, "240": 14400,
    "D": 86400, "1D": 86400, "1W": 604800, "1M": 2592000,
}
SECONDS_RESOLUTIONS = frozenset({"5S", "10S", "15S", "30S", "45S"})

_SATURDAY = 5


def exchange_data_floor(exchange: str) -> date:
    """First date for which the vendor serves data on this exchange."""
    try:
        return EXCHANGE_DATA_FLOOR[exchange.strip().upper()]
    except KeyError:
        raise ValueError(f"unknown exchange {exchange!r}") from None


def clamp_to_floor(exchange: str, day: date) -> date:
    """Move a range start forward to the exchange floor when it sits before it."""
    floor = exchange_data_floor(exchange)
    return floor if day < floor else day


def resolution_seconds(resolution: str) -> int:
    """Length of one candle for a Fyers resolution code."""
    try:
        return RESOLUTION_SECONDS[resolution.strip().upper()]
    except KeyError:
        raise ValueError(f"unknown resolution {resolution!r}") from None


def ist_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Half-open [start, end) covering one IST calendar day, as aware datetimes.

    Half-open and not inclusive, because the vendor's intraday timestamps are candle opens: an
    inclusive right edge at 23:59:59 would drop nothing today but would double count the
    midnight bar the moment a session ever crosses it.
    """
    start = datetime.combine(day, time.min, tzinfo=IST)
    return start, start + timedelta(days=1)


def ist_epoch_bounds(range_from: date, range_to: date) -> tuple[int, int]:
    """UTC epoch seconds for the half-open span [00:00 IST of from, 00:00 IST of to + 1)."""
    if range_to < range_from:
        raise ValueError(f"range_to {range_to} precedes range_from {range_from}")
    start, _ = ist_day_bounds(range_from)
    _, end = ist_day_bounds(range_to)
    return int(start.timestamp()), int(end.timestamp())


def partial_candle_cutoff(resolution: str, now: datetime | None = None) -> datetime:
    """Latest range_to that cannot return a still-forming bar.

    The vendor rule: to receive only completed candles, set range_to at least one resolution
    period before the current time. Their worked example is a 12:10:20 request asking to
    12:09:20 for the completed 12:08 and 12:09 one minute candles.
    """
    moment = now.astimezone(IST) if now is not None else datetime.now(IST)
    return moment - timedelta(seconds=resolution_seconds(resolution))


def chunk_range(
    range_from: date,
    range_to: date,
    *,
    max_days: int = MAX_DAYS_PER_REQUEST,
    newest_first: bool = True,
) -> list[tuple[date, date]]:
    """Split an inclusive date range into pieces of at most ``max_days`` calendar days.

    Newest first by default, because the planner probes backwards: it emits the chunk ending
    on the expiry date, and only plans an older one if that chunk came back full to its left
    edge. A weekly option lives for days, so this turns sixteen requests per contract into one.
    """
    if max_days < 1:
        raise ValueError(f"max_days must be positive, got {max_days}")
    if range_to < range_from:
        return []

    chunks: list[tuple[date, date]] = []
    end = range_to
    while end >= range_from:
        start = max(range_from, end - timedelta(days=max_days - 1))
        chunks.append((start, end))
        if start == range_from:
            break
        end = start - timedelta(days=1)
    return chunks if newest_first else list(reversed(chunks))


class TradingCalendar:
    """Trading days per exchange, from weekends plus the market_holiday table.

    Holidays are injected rather than hardcoded: the authoritative list lives in
    sqlite.market_holiday, and dim_trading_day is ultimately derived from observed spot bars.
    With no holidays loaded this degrades to a weekday calendar, which counts too few
    non-trading days and therefore makes the 30 trading day seconds window start later than
    reality. That direction is the safe one (a contract is treated as outside the window rather
    than inside it), but the holiday table should always be loaded in production.
    """

    def __init__(self, holidays: Mapping[str, Iterable[date]] | None = None) -> None:
        self._holidays: dict[str, set[date]] = {ex: set() for ex in EXCHANGES}
        for exchange, days in (holidays or {}).items():
            self.add_holidays(exchange, days)

    def _bucket(self, exchange: str) -> set[date]:
        key = exchange.strip().upper()
        if key not in self._holidays:
            raise ValueError(f"unknown exchange {exchange!r}")
        return self._holidays[key]

    def add_holidays(self, exchange: str, days: Iterable[date]) -> None:
        bucket = self._bucket(exchange)
        for day in days:
            bucket.add(day if isinstance(day, date) else date.fromisoformat(str(day)))

    def holidays(self, exchange: str) -> frozenset[date]:
        return frozenset(self._bucket(exchange))

    @staticmethod
    def is_weekend(day: date) -> bool:
        return day.weekday() >= _SATURDAY

    def is_trading_day(self, exchange: str, day: date) -> bool:
        return not self.is_weekend(day) and day not in self._bucket(exchange)

    def trading_days(self, exchange: str, start: date, end: date) -> list[date]:
        """Inclusive list of trading days in [start, end]."""
        holidays = self._bucket(exchange)
        out: list[date] = []
        day = start
        while day <= end:
            if not self.is_weekend(day) and day not in holidays:
                out.append(day)
            day += timedelta(days=1)
        return out

    def count_trading_days(self, exchange: str, start: date, end: date) -> int:
        return len(self.trading_days(exchange, start, end))

    def next_trading_day(self, exchange: str, day: date, *, inclusive: bool = False) -> date:
        candidate = day if inclusive else day + timedelta(days=1)
        while not self.is_trading_day(exchange, candidate):
            candidate += timedelta(days=1)
        return candidate

    def previous_trading_day(self, exchange: str, day: date, *, inclusive: bool = False) -> date:
        candidate = day if inclusive else day - timedelta(days=1)
        while not self.is_trading_day(exchange, candidate):
            candidate -= timedelta(days=1)
        return candidate

    def shift_trading_days(self, exchange: str, day: date, count: int) -> date:
        """Move ``count`` trading days from ``day``. Negative goes backwards."""
        if count == 0:
            return day
        step = 1 if count > 0 else -1
        current = day
        for _ in range(abs(count)):
            current = (
                self.next_trading_day(exchange, current)
                if step > 0
                else self.previous_trading_day(exchange, current)
            )
        return current

    def iter_trading_days(self, exchange: str, start: date, end: date) -> Iterator[date]:
        yield from self.trading_days(exchange, start, end)

    def seconds_window(self, exchange: str, asof: date | None = None) -> tuple[date, date]:
        """Inclusive [start, end] spanning the last 30 trading days on or before ``asof``.

        Second resolutions exist only inside this window, and the data is genuinely lost once
        it closes, so the seconds capture job is scheduled against exactly this range.
        """
        end = self.previous_trading_day(exchange, asof or date.today(), inclusive=True)
        start = self.shift_trading_days(exchange, end, -(SECONDS_WINDOW_TRADING_DAYS - 1))
        return start, end

    def is_within_seconds_window(
        self, exchange: str, day: date, asof: date | None = None
    ) -> bool:
        """Whether a second resolution can be requested for an expiry on ``day``."""
        start, end = self.seconds_window(exchange, asof)
        return start <= day <= end
