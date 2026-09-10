"""Bars, chains and ATM: the read side every chart and every backtest sits on.

Three conventions hold across this module and none of them are negotiable.

Timestamps leave in UTC seconds as ``epoch(ts) - 19800``. The store keeps naive IST wall clock,
the chart adapter wants epoch UTC, and doing the arithmetic in DuckDB rather than in Python
means it happens once per query instead of once per row.

Prices leave as DOUBLE. They are stored DECIMAL(11,4) so that they are exact on the way in and
in every aggregate, but a JSON response has no decimal type, so the cast is explicit and happens
at the very last moment.

Predicates lead with ``contract_id``, which is the leading key of the physical sort order. The
chain queries turn a whole expiry into ``contract_id BETWEEN lo AND hi`` from ``dim_expiry``,
which is the entire reason contract ids are allocated in contiguous padded blocks.

``spot_at`` and ``atm_strike`` are called as the shipped DuckDB macros rather than reimplemented,
so the API and a future backtester cannot end up with two definitions of ATM. The chain grid is
inline SQL because the API response needs ``contract_id`` per leg and the ``chain_at`` macro,
which lives in a file this work item does not own, does not return it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from expirymanager.db.arrow import IST_OFFSET_SECONDS

if TYPE_CHECKING:
    from expirymanager.db.reader import DuckReader

__all__ = [
    "FYERS_COLUMNS",
    "FYERS_COLUMNS_NO_OI",
    "MAX_CANDLES_PER_RESPONSE",
    "DEFAULT_BEFORE_COUNT",
    "MAX_BEFORE_COUNT",
    "BarsPage",
    "ChainLeg",
    "ChainRow",
    "ChainSlice",
    "AtmPick",
    "RangeTooLarge",
    "to_ist",
    "to_utc_seconds",
    "bars",
    "bars_before",
    "oi_series",
    "spot_bars",
    "spot_at",
    "atm",
    "chain",
    "chain_window",
    "daily_bars",
    "window_from_epoch",
]

# Exactly the array Fyers returns with include_oi=1, so the frontend maps by name on both the
# ingest side and the read side and adding a column later is a non event.
FYERS_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "open_interest")
FYERS_COLUMNS_NO_OI = FYERS_COLUMNS[:-1]

# Above this a chart cannot render the result and the JSON encode alone costs more than the
# query. The error names the maximum so the caller can narrow the window.
MAX_CANDLES_PER_RESPONSE = 500_000

DEFAULT_BEFORE_COUNT = 500
MAX_BEFORE_COUNT = 5_000

_BAR_PROJECTION = (
    "epoch(ts) - {offset} AS timestamp, "
    "CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high, "
    "CAST(low AS DOUBLE) AS low, CAST(close AS DOUBLE) AS close, "
    "volume, oi"
).format(offset=IST_OFFSET_SECONDS)


class RangeTooLarge(ValueError):
    """The requested window holds more candles than a single response may carry."""

    def __init__(self, available: int) -> None:
        super().__init__(
            f"the requested range holds {available} candles and the maximum for one response "
            f"is {MAX_CANDLES_PER_RESPONSE}. Narrow the range or use a coarser resolution."
        )
        self.available = available
        self.maximum = MAX_CANDLES_PER_RESPONSE


_EPOCH = datetime(1970, 1, 1)


def to_ist(utc_seconds: int | float) -> datetime:
    """A UTC epoch second to the naive IST wall clock the store is keyed on.

    Plain arithmetic on a fixed offset rather than a timezone lookup: India has been UTC+05:30
    with no daylight saving since 1945, so there is nothing for a timezone database to add.
    """
    return _EPOCH + timedelta(seconds=int(utc_seconds) + IST_OFFSET_SECONDS)


def to_utc_seconds(moment: datetime | None) -> int | None:
    """The inverse, for a bound read back out of the store."""
    if moment is None:
        return None
    return int((moment - _EPOCH).total_seconds()) - IST_OFFSET_SECONDS


@dataclass(frozen=True, slots=True)
class BarsPage:
    """The columnar bars response, which is also the smallest shape on the wire."""

    contract_id: int
    symbol: str | None
    resolution: str | None
    columns: tuple[str, ...]
    candles: list[list[Any]]

    @property
    def row_count(self) -> int:
        return len(self.candles)


@dataclass(frozen=True, slots=True)
class ChainLeg:
    contract_id: int
    fyers_symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int | None


@dataclass(frozen=True, slots=True)
class ChainRow:
    strike: float
    lot_size: int | None
    ce: ChainLeg | None
    pe: ChainLeg | None


@dataclass(frozen=True, slots=True)
class ChainSlice:
    underlying_id: int
    expiry_date: date
    ts: int | None
    spot: float | None
    atm_strike: float | None
    rows: list[ChainRow]


@dataclass(frozen=True, slots=True)
class AtmPick:
    spot: float | None
    atm_strike: float | None
    ce_contract_id: int | None
    pe_contract_id: int | None


def _project(include_oi: bool) -> tuple[str, tuple[str, ...]]:
    if include_oi:
        return (_BAR_PROJECTION, FYERS_COLUMNS)
    # The oi column is dropped from the projection rather than nulled, so a caller that did not
    # ask for open interest does not pay to serialise a column of nulls.
    return (_BAR_PROJECTION.rsplit(", oi", 1)[0], FYERS_COLUMNS_NO_OI)


async def _count_bars(
    reader: DuckReader, contract_id: int, res_id: int, start: datetime, end: datetime
) -> int:
    value = await reader.fetch_value(
        "SELECT count(*) FROM candles "
        "WHERE contract_id = ? AND res_id = ? AND ts >= ? AND ts < ?",
        [contract_id, res_id, start, end],
    )
    return int(value or 0)


async def bars(
    reader: DuckReader,
    *,
    contract_id: int,
    res_id: int,
    start: datetime,
    end: datetime,
    include_oi: bool = True,
    symbol: str | None = None,
    resolution: str | None = None,
    enforce_limit: bool = True,
) -> BarsPage:
    """Bars for one contract over a half-open window.

    The window is half-open on the right for the same reason the writer's delete predicate is:
    two adjacent requests must not both claim the boundary bar.
    """
    if enforce_limit:
        available = await _count_bars(reader, contract_id, res_id, start, end)
        if available > MAX_CANDLES_PER_RESPONSE:
            raise RangeTooLarge(available)
    projection, columns = _project(include_oi)
    rows = await reader.fetch_all(
        f"SELECT {projection} FROM candles "
        "WHERE contract_id = ? AND res_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
        [contract_id, res_id, start, end],
    )
    return BarsPage(
        contract_id=contract_id,
        symbol=symbol,
        resolution=resolution,
        columns=columns,
        candles=[list(row) for row in rows],
    )


async def bars_before(
    reader: DuckReader,
    *,
    contract_id: int,
    res_id: int,
    before: datetime,
    count: int = DEFAULT_BEFORE_COUNT,
    include_oi: bool = True,
    symbol: str | None = None,
    resolution: str | None = None,
) -> BarsPage:
    """The ``count`` bars immediately before a timestamp, oldest first.

    Backs the chart's history loader. The inner query walks backwards so the LIMIT prunes, and
    the outer one restores ascending order because that is what the adapter prepends.
    """
    bounded = max(1, min(int(count), MAX_BEFORE_COUNT))
    projection, columns = _project(include_oi)
    rows = await reader.fetch_all(
        f"SELECT * FROM (SELECT {projection} FROM candles "
        "WHERE contract_id = ? AND res_id = ? AND ts < ? "
        "ORDER BY ts DESC LIMIT ?) ORDER BY 1",
        [contract_id, res_id, before, bounded],
    )
    return BarsPage(
        contract_id=contract_id,
        symbol=symbol,
        resolution=resolution,
        columns=columns,
        candles=[list(row) for row in rows],
    )


async def oi_series(
    reader: DuckReader,
    *,
    contract_id: int,
    res_id: int,
    start: datetime,
    end: datetime,
) -> list[list[Any]]:
    """Open interest points for the Tier 2 indicator, which the Bar shape has no field for."""
    rows = await reader.fetch_all(
        f"SELECT epoch(ts) - {IST_OFFSET_SECONDS} AS timestamp, oi FROM candles "
        "WHERE contract_id = ? AND res_id = ? AND ts >= ? AND ts < ? AND oi IS NOT NULL "
        "ORDER BY ts",
        [contract_id, res_id, start, end],
    )
    return [[row[0], row[1]] for row in rows]


async def spot_contract_id(reader: DuckReader, underlying_id: int) -> int | None:
    value = await reader.fetch_value(
        "SELECT spot_contract_id FROM dim_underlying WHERE underlying_id = ?", [underlying_id]
    )
    return None if value is None else int(value)


async def spot_bars(
    reader: DuckReader,
    *,
    underlying_id: int,
    res_id: int,
    start: datetime,
    end: datetime,
    include_oi: bool = False,
) -> BarsPage:
    """The underlying's own series, served by the identical bars path.

    Spot lives in ``candles`` as a contract of kind SPOT, so this is one extra catalog lookup and
    then the same query, rather than a second store and a second chart adapter.
    """
    contract_id = await spot_contract_id(reader, underlying_id)
    if contract_id is None:
        return BarsPage(
            contract_id=0,
            symbol=None,
            resolution=None,
            columns=FYERS_COLUMNS if include_oi else FYERS_COLUMNS_NO_OI,
            candles=[],
        )
    symbol = await reader.fetch_value(
        "SELECT fyers_symbol FROM dim_underlying WHERE underlying_id = ?", [underlying_id]
    )
    return await bars(
        reader,
        contract_id=contract_id,
        res_id=res_id,
        start=start,
        end=end,
        include_oi=include_oi,
        symbol=symbol,
    )


async def spot_at(
    reader: DuckReader, *, underlying_id: int, res_id: int, moment: datetime
) -> float | None:
    """The last spot close at or before a moment, through the shipped macro."""
    row = await reader.fetch_one(
        "SELECT close FROM spot_at(?, ?, ?)", [underlying_id, res_id, moment]
    )
    return None if row is None or row[0] is None else float(row[0])


async def atm(
    reader: DuckReader,
    *,
    underlying_id: int,
    expiry_date: date,
    res_id: int,
    moment: datetime,
) -> AtmPick:
    """The ATM strike at a moment, plus the call and the put that sit on it.

    Strike selection is nearest to spot with the strike value itself breaking a tie, so a spot
    exactly between two strikes always picks the same one rather than whichever the plan
    happened to emit first.
    """
    spot = await spot_at(reader, underlying_id=underlying_id, res_id=res_id, moment=moment)
    if spot is None:
        return AtmPick(spot=None, atm_strike=None, ce_contract_id=None, pe_contract_id=None)
    rows = await reader.fetch_all(
        "SELECT strike, option_type, contract_id FROM dim_contract "
        " WHERE underlying_id = ? AND expiry_date = ? AND kind = 'OPT' AND strike IS NOT NULL "
        " ORDER BY abs(strike - CAST(? AS DECIMAL(12,4))), strike, option_type "
        " LIMIT 2",
        [underlying_id, expiry_date, spot],
    )
    if not rows:
        return AtmPick(spot=spot, atm_strike=None, ce_contract_id=None, pe_contract_id=None)
    strike = float(rows[0][0])
    legs = {str(row[1]): int(row[2]) for row in rows if float(row[0]) == strike}
    return AtmPick(
        spot=spot,
        atm_strike=strike,
        ce_contract_id=legs.get("CE"),
        pe_contract_id=legs.get("PE"),
    )


_CHAIN_SQL = """
WITH ids AS (
    SELECT contract_id_lo AS lo, contract_id_hi AS hi
      FROM dim_expiry
     WHERE underlying_id = ? AND expiry_date = ?
)
SELECT c.contract_id, c.strike, c.option_type, c.fyers_symbol, c.lot_size,
       CAST(k.open AS DOUBLE), CAST(k.high AS DOUBLE), CAST(k.low AS DOUBLE),
       CAST(k.close AS DOUBLE), k.volume, k.oi
  FROM candles k
  JOIN ids ON k.contract_id BETWEEN ids.lo AND ids.hi
  JOIN dim_contract c USING (contract_id)
 WHERE k.res_id = ? AND k.ts = ? AND c.kind = 'OPT'
 ORDER BY c.strike, c.option_type
"""


async def chain(
    reader: DuckReader,
    *,
    underlying_id: int,
    expiry_date: date,
    res_id: int,
    moment: datetime,
) -> ChainSlice:
    """The whole option chain at one timestamp, one row per strike carrying both rights.

    The BETWEEN on the id block is what makes this a bounded scan of the front of the expiry's
    rows instead of several hundred scattered lookups, and it is the query the backtester runs
    once per bar per expiry.
    """
    rows = await reader.fetch_all(
        _CHAIN_SQL, [underlying_id, expiry_date, res_id, moment]
    )
    pick = await atm(
        reader,
        underlying_id=underlying_id,
        expiry_date=expiry_date,
        res_id=res_id,
        moment=moment,
    )
    grid: dict[float, dict[str, Any]] = {}
    order: list[float] = []
    for row in rows:
        strike = float(row[1])
        if strike not in grid:
            grid[strike] = {"lot_size": row[4], "CE": None, "PE": None}
            order.append(strike)
        leg = ChainLeg(
            contract_id=int(row[0]),
            fyers_symbol=str(row[3]),
            open=row[5],
            high=row[6],
            low=row[7],
            close=row[8],
            volume=int(row[9]) if row[9] is not None else 0,
            oi=None if row[10] is None else int(row[10]),
        )
        grid[strike][str(row[2])] = leg
        if grid[strike]["lot_size"] is None:
            grid[strike]["lot_size"] = row[4]
    return ChainSlice(
        underlying_id=underlying_id,
        expiry_date=expiry_date,
        ts=to_utc_seconds(moment),
        spot=pick.spot,
        atm_strike=pick.atm_strike,
        rows=[
            ChainRow(
                strike=strike,
                lot_size=grid[strike]["lot_size"],
                ce=grid[strike]["CE"],
                pe=grid[strike]["PE"],
            )
            for strike in order
        ],
    )


async def chain_window(
    reader: DuckReader,
    *,
    underlying_id: int,
    expiry_date: date,
    res_id: int,
    start: datetime,
    end: datetime,
    strikes_each_side: int,
) -> list[tuple[Any, ...]]:
    """A strike band around ATM over a session, through the shipped macro.

    Because ids inside a block ascend with strike, the band is itself a narrow contiguous id
    range and reads a fraction of the chain.
    """
    return await reader.fetch_all(
        "SELECT epoch(ts) - ? AS timestamp, CAST(strike AS DOUBLE), option_type, "
        "       CAST(close AS DOUBLE), volume, oi "
        "  FROM chain_window(?, ?, ?, ?, ?, ?)",
        [
            IST_OFFSET_SECONDS,
            underlying_id,
            expiry_date,
            res_id,
            start,
            end,
            strikes_each_side,
        ],
    )


async def daily_bars(reader: DuckReader, *, contract_id: int) -> list[tuple[Any, ...]]:
    """The daily series Fyers does not serve for expired contracts, aggregated from one minute."""
    return await reader.fetch_all(
        "SELECT trade_date, CAST(open AS DOUBLE), CAST(high AS DOUBLE), CAST(low AS DOUBLE), "
        "       CAST(close AS DOUBLE), volume, oi, bar_count "
        "  FROM v_candle_daily WHERE contract_id = ? ORDER BY trade_date",
        [contract_id],
    )


def window_from_epoch(
    start_seconds: int | float | None, end_seconds: int | float | None
) -> tuple[datetime, datetime]:
    """Translate a caller supplied UTC epoch window into the store's naive IST window."""
    if start_seconds is None or end_seconds is None:
        raise ValueError("both ends of the window are required")
    start = to_ist(start_seconds)
    end = to_ist(end_seconds)
    if end < start:
        raise ValueError("the end of the window is before its start")
    return (start, end)
