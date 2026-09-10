"""The coverage ledger: what is already held, and what the planner still has to fetch.

Every function here reads ``candle_coverage`` and ``contract_bounds`` and nothing else. That is
the entire reason ``candle_coverage`` exists: deriving coverage with ``min(ts), max(ts)`` over
``candles`` would make the heatmap and the planner scale with the fact table, and the fact table
reaches hundreds of millions of rows. A coverage query that touches ``candles`` is a bug.

``missing_windows`` is the planner's core question and is a pure function on purpose. Interval
arithmetic is where an off-by-one silently re-downloads a day or silently skips one, and both
cost governed requests that cannot be recovered. Keeping it out of SQL means it can be tested
exhaustively without a database.

The ledger is keyed on the request, so its ranges are Fyers ranges: inclusive on both ends,
exactly as they were sent. The half-open conversion belongs to the writer and stays there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:
    from expirymanager.db.reader import DuckReader

__all__ = [
    "HeldRange",
    "merge_ranges",
    "missing_windows",
    "held_ranges",
    "missing_for_contract",
    "coverage_grid",
    "coverage_gaps",
    "expiry_chunk_summary",
    "contracts_needing_data",
]

ONE_DAY = timedelta(days=1)

# A chunk recorded as an error is not coverage. It is a failure that must be retried, so it is
# excluded from the held set and the planner will ask for that window again.
HELD_STATUSES = ("ok", "empty")


@dataclass(frozen=True, slots=True)
class HeldRange:
    """One recorded fetch chunk. Both ends inclusive, as Fyers takes them."""

    range_from: date
    range_to: date
    status: str
    row_count: int


def merge_ranges(ranges: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    """Coalesce inclusive ranges, joining ones that touch as well as ones that overlap.

    Two chunks that end and start on consecutive days leave no gap between them, so they must
    merge. Treating them as separate would make the planner re-request the seam forever.
    """
    ordered = sorted(ranges)
    merged: list[tuple[date, date]] = []
    for start, end in ordered:
        if end < start:
            raise ValueError(f"range end {end} is before its start {start}")
        if merged and start <= merged[-1][1] + ONE_DAY:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def missing_windows(
    held: Iterable[tuple[date, date]],
    want_from: date,
    want_to: date,
    *,
    max_days: int | None = None,
) -> list[tuple[date, date]]:
    """The inclusive ranges inside [want_from, want_to] that are not already held.

    ``max_days`` splits each gap into request sized pieces counted in calendar days, which is
    what the Fyers limit counts. The measured boundary is a hard error rather than a truncation,
    so a piece is never allowed to exceed it.
    """
    if want_to < want_from:
        return []
    gaps: list[tuple[date, date]] = []
    cursor = want_from
    for start, end in merge_ranges(held):
        if end < cursor:
            continue
        if start > want_to:
            break
        if start > cursor:
            gaps.append((cursor, min(start - ONE_DAY, want_to)))
        cursor = max(cursor, end + ONE_DAY)
        if cursor > want_to:
            break
    if cursor <= want_to:
        gaps.append((cursor, want_to))

    if max_days is None:
        return gaps
    if max_days < 1:
        raise ValueError("max_days must be at least one day")
    pieces: list[tuple[date, date]] = []
    for start, end in gaps:
        piece_start = start
        while piece_start <= end:
            piece_end = min(piece_start + timedelta(days=max_days - 1), end)
            pieces.append((piece_start, piece_end))
            piece_start = piece_end + ONE_DAY
    return pieces


async def held_ranges(
    reader: DuckReader,
    *,
    contract_id: int,
    res_id: int,
    statuses: Sequence[str] = HELD_STATUSES,
) -> list[HeldRange]:
    """Every chunk already recorded for one contract and resolution."""
    placeholders = ", ".join("?" for _ in statuses)
    rows = await reader.fetch_all(
        "SELECT range_from, range_to, status, row_count FROM candle_coverage "
        f"WHERE contract_id = ? AND res_id = ? AND status IN ({placeholders}) "
        "ORDER BY range_from",
        [contract_id, res_id, *statuses],
    )
    return [
        HeldRange(
            range_from=row[0], range_to=row[1], status=str(row[2]), row_count=int(row[3])
        )
        for row in rows
    ]


async def missing_for_contract(
    reader: DuckReader,
    *,
    contract_id: int,
    res_id: int,
    want_from: date,
    want_to: date,
    max_days: int | None = None,
) -> list[tuple[date, date]]:
    """What the planner still has to request for one contract and resolution."""
    held = await held_ranges(reader, contract_id=contract_id, res_id=res_id)
    return missing_windows(
        [(item.range_from, item.range_to) for item in held],
        want_from,
        want_to,
        max_days=max_days,
    )


async def coverage_grid(
    reader: DuckReader,
    *,
    underlying_id: int,
    res_id: int | None = None,
    expiry_from: date | None = None,
    expiry_to: date | None = None,
) -> list[dict[str, Any]]:
    """The expiry by resolution matrix the heatmap renders.

    ``contracts_total`` comes from ``dim_expiry`` rather than a count over ``dim_contract``, so
    a cell that has no coverage at all still reports how much is missing instead of vanishing.
    """
    # SPOT rows carry coverage too and have no expiry, so they would collapse into a NULL cell
    # that reads as a phantom expiry in the heatmap.
    where = ["c.underlying_id = ?", "c.kind <> 'SPOT'"]
    params: list[Any] = [underlying_id]
    if res_id is not None:
        where.append("cov.res_id = ?")
        params.append(res_id)
    if expiry_from is not None:
        where.append("c.expiry_date >= ?")
        params.append(expiry_from)
    if expiry_to is not None:
        where.append("c.expiry_date <= ?")
        params.append(expiry_to)

    sql = (
        "SELECT c.expiry_date, cov.res_id, "
        "       count(DISTINCT cov.contract_id) AS contracts_covered, "
        "       count(*) FILTER (WHERE cov.status = 'ok')    AS chunks_ok, "
        "       count(*) FILTER (WHERE cov.status = 'empty') AS chunks_empty, "
        "       count(*) FILTER (WHERE cov.status = 'error') AS chunks_error, "
        "       coalesce(sum(cov.row_count), 0) AS rows, "
        "       min(cov.range_from) AS have_from, max(cov.range_to) AS have_to, "
        "       max(e.contract_count) AS contracts_total "
        "  FROM candle_coverage cov "
        "  JOIN dim_contract c USING (contract_id) "
        "  LEFT JOIN dim_expiry e ON e.expiry_id = c.expiry_id "
        " WHERE " + " AND ".join(where) +
        " GROUP BY c.expiry_date, cov.res_id ORDER BY c.expiry_date, cov.res_id"
    )
    columns, rows = await reader.fetch_columns(sql, params)
    return [dict(zip(columns, row)) for row in rows]


async def coverage_gaps(
    reader: DuckReader,
    *,
    underlying_id: int | None = None,
    res_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Holes between two recorded chunks of the same contract, from v_coverage_gaps."""
    where: list[str] = []
    params: list[Any] = []
    if underlying_id is not None:
        where.append(
            "contract_id IN (SELECT contract_id FROM dim_contract WHERE underlying_id = ?)"
        )
        params.append(underlying_id)
    if res_id is not None:
        where.append("res_id = ?")
        params.append(res_id)
    sql = "SELECT * FROM v_coverage_gaps"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY contract_id, gap_after LIMIT ?"
    columns, rows = await reader.fetch_columns(sql, [*params, max(1, int(limit))])
    return [dict(zip(columns, row)) for row in rows]


async def expiry_chunk_summary(
    reader: DuckReader,
    *,
    underlying_id: int,
    expiry_date: date,
    res_id: int,
) -> list[dict[str, Any]]:
    """Per contract coverage for one expiry, the query DATA-MODEL section 3.7 specifies."""
    columns, rows = await reader.fetch_columns(
        "SELECT c.contract_id, c.fyers_symbol, CAST(c.strike AS DOUBLE) AS strike, "
        "       c.option_type, "
        "       min(cov.range_from) AS have_from, max(cov.range_to) AS have_to, "
        "       coalesce(sum(cov.row_count), 0) AS rows_held, "
        "       count(*) FILTER (WHERE cov.status = 'empty') AS empty_chunks "
        "  FROM candle_coverage cov JOIN dim_contract c USING (contract_id) "
        " WHERE c.underlying_id = ? AND c.expiry_date = ? AND cov.res_id = ? "
        " GROUP BY 1, 2, 3, 4 ORDER BY 3, 4",
        [underlying_id, expiry_date, res_id],
    )
    return [dict(zip(columns, row)) for row in rows]


async def contracts_needing_data(
    reader: DuckReader,
    *,
    underlying_id: int,
    expiry_date: date,
    res_id: int,
) -> list[dict[str, Any]]:
    """The contracts of one expiry that hold no chunk yet for a resolution.

    The planner asks this before it asks anything else, because a contract with no coverage row
    at all needs its whole life requested and there is no interval arithmetic to do.
    """
    columns, rows = await reader.fetch_columns(
        "SELECT c.contract_id, c.fyers_symbol, c.kind, CAST(c.strike AS DOUBLE) AS strike, "
        "       c.option_type, c.expiry_date, c.sealed_at "
        "  FROM dim_contract c "
        " WHERE c.underlying_id = ? AND c.expiry_date = ? AND c.kind <> 'SPOT' "
        "   AND NOT EXISTS (SELECT 1 FROM candle_coverage cov "
        "                    WHERE cov.contract_id = c.contract_id AND cov.res_id = ?) "
        " ORDER BY c.contract_id",
        [underlying_id, expiry_date, res_id],
    )
    return [dict(zip(columns, row)) for row in rows]
