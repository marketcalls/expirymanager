"""Catalog reads: underlyings, expiries and contracts.

These back API.md sections 3, 4 and 5. Three things are deliberate.

Listings are keyset paged, never OFFSET paged. The contracts table reaches hundreds of thousands
of rows, and OFFSET makes the last page cost as much as the whole scan. The cursor is the sort
key of the last row returned, so the next page is a range predicate.

Sort columns are chosen from a fixed map, never interpolated from a request. A sort parameter
that reached the SQL text would be an injection point, and a whitelist is the only version of
this that stays safe when someone adds a column later.

Bounds leave as UTC seconds. ``GET /contracts/{id}/bounds`` is the lookup that stops every
expired contract rendering as "No bars", and it is called on every symbol change, so the
conversion happens in DuckDB rather than per row in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from expirymanager.db.arrow import IST_OFFSET_SECONDS

if TYPE_CHECKING:
    from expirymanager.db.reader import DuckReader

__all__ = [
    "DEFAULT_PAGE",
    "MAX_PAGE",
    "CONTRACT_SORTS",
    "ContractFilters",
    "Page",
    "list_underlyings",
    "get_underlying",
    "list_expiries",
    "list_expiry_contracts",
    "list_contracts",
    "get_contract",
    "contract_bounds",
    "resolutions",
]

DEFAULT_PAGE = 100
MAX_PAGE = 500

# Whitelist. The value is the SQL expression, the key is what the request may name.
CONTRACT_SORTS: dict[str, str] = {
    "expiry_date": "c.expiry_date",
    "strike": "c.strike",
    "fyers_symbol": "c.fyers_symbol",
    "rows": "coalesce(b.row_count, 0)",
}


@dataclass(frozen=True, slots=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: int | None


@dataclass(frozen=True, slots=True)
class ContractFilters:
    """Every filter GET /api/v1/contracts accepts, in one value."""

    underlying_id: int | None = None
    expiry_from: date | None = None
    expiry_to: date | None = None
    kind: str | None = None
    option_type: str | None = None
    strike_min: float | None = None
    strike_max: float | None = None
    symbol_contains: str | None = None
    has_data: bool | None = None
    sealed: bool | None = None
    res_id: int | None = None


def _rows_to_dicts(columns: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in rows]


async def list_underlyings(reader: DuckReader, *, active_only: bool = False) -> list[dict[str, Any]]:
    """Every registered underlying with its rollups and its spot series bounds.

    The spot join is on ``spot_contract_id``, so the bar count and the last timestamp come from
    ``contract_bounds`` rather than from a count over ``candles``.
    """
    sql = (
        "SELECT u.underlying_id, u.fyers_symbol, u.root, u.exchange, u.segment, "
        "       u.instrument_kind, u.display_name, u.data_from, u.is_active, "
        "       u.first_expiry, u.last_expiry, u.expiry_count, u.contract_count, "
        "       u.spot_contract_id, u.underlying_fytoken, u.synced_at, "
        "       coalesce(sum(b.row_count), 0) AS spot_bars, max(b.last_ts) AS spot_last_ts "
        "  FROM dim_underlying u "
        "  LEFT JOIN contract_bounds b ON b.contract_id = u.spot_contract_id "
        + ("WHERE u.is_active " if active_only else "")
        + " GROUP BY ALL ORDER BY u.underlying_id"
    )
    columns, rows = await reader.fetch_columns(sql)
    return _rows_to_dicts(columns, rows)


async def get_underlying(reader: DuckReader, underlying_id: int) -> dict[str, Any] | None:
    columns, rows = await reader.fetch_columns(
        "SELECT * FROM dim_underlying WHERE underlying_id = ?", [underlying_id]
    )
    return _rows_to_dicts(columns, rows)[0] if rows else None


async def list_expiries(
    reader: DuckReader,
    *,
    underlying_id: int,
    expiry_from: date | None = None,
    expiry_to: date | None = None,
    res_id: int | None = None,
    cursor: str | None = None,
    limit: int = 200,
) -> Page:
    """Expiries with the coverage rollup the expiry table renders.

    The rollup reads ``candle_coverage`` and ``contract_bounds`` only. It must never touch
    ``candles``, because this list is rendered on every visit to the underlying page while
    ``candles`` grows to hundreds of millions of rows.
    """
    bounded = max(1, min(int(limit), MAX_PAGE))
    # Unqualified inside the CTE below, where dim_expiry is selected without an alias.
    where = ["underlying_id = ?"]
    params: list[Any] = [underlying_id]
    if expiry_from is not None:
        where.append("expiry_date >= ?")
        params.append(expiry_from)
    if expiry_to is not None:
        where.append("expiry_date <= ?")
        params.append(expiry_to)
    if cursor:
        where.append("expiry_date > ?")
        params.append(date.fromisoformat(cursor))

    res_filter = "" if res_id is None else " AND cov.res_id = ?"
    cov_params: list[Any] = [] if res_id is None else [res_id]
    bounds_filter = "" if res_id is None else " AND b.res_id = ?"

    sql = (
        "WITH e AS (SELECT * FROM dim_expiry WHERE " + " AND ".join(where) +
        "            ORDER BY expiry_date LIMIT ?), "
        "     cov AS (SELECT c.expiry_id, "
        "                    count(*) FILTER (WHERE cov.status = 'ok')    AS chunks_ok, "
        "                    count(*) FILTER (WHERE cov.status = 'empty') AS chunks_empty, "
        "                    count(*) FILTER (WHERE cov.status = 'error') AS chunks_error, "
        "                    coalesce(sum(cov.row_count), 0)              AS rows "
        "               FROM candle_coverage cov JOIN dim_contract c USING (contract_id) "
        "              WHERE c.expiry_id IN (SELECT expiry_id FROM e)" + res_filter +
        "              GROUP BY c.expiry_id), "
        "     held AS (SELECT c.expiry_id, "
        "                     count(DISTINCT c.contract_id) AS contracts_with_data "
        "                FROM contract_bounds b JOIN dim_contract c USING (contract_id) "
        "               WHERE c.expiry_id IN (SELECT expiry_id FROM e) AND b.row_count > 0"
        + bounds_filter +
        "               GROUP BY c.expiry_id), "
        "     sealed AS (SELECT expiry_id, count(*) AS contracts_sealed "
        "                  FROM dim_contract WHERE sealed_at IS NOT NULL "
        "                   AND expiry_id IN (SELECT expiry_id FROM e) GROUP BY expiry_id) "
        "SELECT e.*, "
        "       coalesce(held.contracts_with_data, 0) AS contracts_with_data, "
        "       coalesce(sealed.contracts_sealed, 0)  AS contracts_sealed, "
        "       coalesce(cov.chunks_ok, 0)            AS chunks_ok, "
        "       coalesce(cov.chunks_empty, 0)         AS chunks_empty, "
        "       coalesce(cov.chunks_error, 0)         AS chunks_error, "
        "       coalesce(cov.rows, 0)                 AS rows "
        "  FROM e LEFT JOIN cov USING (expiry_id) "
        "         LEFT JOIN held USING (expiry_id) "
        "         LEFT JOIN sealed USING (expiry_id) "
        " ORDER BY e.expiry_date"
    )
    columns, rows = await reader.fetch_columns(
        sql, [*params, bounded + 1, *cov_params, *cov_params]
    )
    items = _rows_to_dicts(columns, rows)
    next_cursor = None
    if len(items) > bounded:
        items = items[:bounded]
        next_cursor = items[-1]["expiry_date"].isoformat()
    return Page(items=items, next_cursor=next_cursor)


async def list_expiry_contracts(
    reader: DuckReader,
    *,
    underlying_id: int,
    expiry_date: date,
    kind: str | None = None,
    option_type: str | None = None,
    strike_min: float | None = None,
    strike_max: float | None = None,
    cursor: int | None = None,
    limit: int = DEFAULT_PAGE,
) -> Page:
    """One expiry's contracts joined to their bounds, paged on contract_id.

    Paging on contract_id is free here: ids inside an expiry are contiguous and ascend with
    strike, so the cursor is both the sort key and a range predicate on the leading key.
    """
    filters = ContractFilters(
        underlying_id=underlying_id,
        expiry_from=expiry_date,
        expiry_to=expiry_date,
        kind=kind,
        option_type=option_type,
        strike_min=strike_min,
        strike_max=strike_max,
    )
    return await list_contracts(
        reader, filters=filters, sort="strike", direction="asc", cursor=cursor, limit=limit
    )


def _contract_predicates(filters: ContractFilters) -> tuple[list[str], list[Any]]:
    where: list[str] = ["c.kind <> 'SPOT'"]
    params: list[Any] = []
    if filters.underlying_id is not None:
        where.append("c.underlying_id = ?")
        params.append(filters.underlying_id)
    if filters.expiry_from is not None:
        where.append("c.expiry_date >= ?")
        params.append(filters.expiry_from)
    if filters.expiry_to is not None:
        where.append("c.expiry_date <= ?")
        params.append(filters.expiry_to)
    if filters.kind is not None:
        where.append("c.kind = ?")
        params.append(filters.kind)
    if filters.option_type is not None:
        where.append("c.option_type = ?")
        params.append(filters.option_type)
    if filters.strike_min is not None:
        where.append("c.strike >= CAST(? AS DECIMAL(12,4))")
        params.append(filters.strike_min)
    if filters.strike_max is not None:
        where.append("c.strike <= CAST(? AS DECIMAL(12,4))")
        params.append(filters.strike_max)
    if filters.symbol_contains:
        where.append("contains(upper(c.fyers_symbol), upper(?))")
        params.append(filters.symbol_contains)
    if filters.sealed is True:
        where.append("c.sealed_at IS NOT NULL")
    elif filters.sealed is False:
        where.append("c.sealed_at IS NULL")
    if filters.has_data is True:
        where.append("coalesce(b.row_count, 0) > 0")
    elif filters.has_data is False:
        where.append("coalesce(b.row_count, 0) = 0")
    return (where, params)


async def list_contracts(
    reader: DuckReader,
    *,
    filters: ContractFilters | None = None,
    sort: str = "expiry_date",
    direction: str = "asc",
    cursor: int | None = None,
    limit: int = DEFAULT_PAGE,
) -> Page:
    """The server driven contracts listing.

    ``contract_id`` is always the final sort term and the cursor, so the ordering is total even
    when the named sort column ties, which is what makes keyset paging correct rather than
    merely fast.
    """
    filters = filters or ContractFilters()
    bounded = max(1, min(int(limit), MAX_PAGE))
    if sort not in CONTRACT_SORTS:
        raise ValueError(
            f"unknown sort column {sort!r}. Allowed: " + ", ".join(sorted(CONTRACT_SORTS))
        )
    order = "DESC" if str(direction).lower() == "desc" else "ASC"

    where, params = _contract_predicates(filters)
    bounds_join = "LEFT JOIN contract_bounds b ON b.contract_id = c.contract_id"
    if filters.res_id is not None:
        bounds_join += " AND b.res_id = ?"
        params.insert(0, filters.res_id)
    if cursor is not None:
        where.append("c.contract_id > ?" if order == "ASC" else "c.contract_id < ?")
        params.append(cursor)

    sql = (
        "SELECT c.contract_id, c.fyers_symbol, c.underlying_id, c.kind, c.instrument_class, "
        "       c.expiry_date, CAST(c.strike AS DOUBLE) AS strike, c.strike_raw, c.option_type, "
        "       c.lot_size, CAST(c.tick_size AS DOUBLE) AS tick_size, c.fytoken, "
        "       c.symbol_expiry_encoding, c.expiry_cycle, c.parse_confidence, c.sealed_at, "
        "       coalesce(b.row_count, 0) AS rows, b.res_id, b.first_ts, b.last_ts "
        "  FROM dim_contract c " + bounds_join +
        " WHERE " + " AND ".join(where) +
        f" ORDER BY {CONTRACT_SORTS[sort]} {order}, c.contract_id {order} LIMIT ?"
    )
    columns, rows = await reader.fetch_columns(sql, [*params, bounded + 1])
    items = _rows_to_dicts(columns, rows)
    next_cursor = None
    if len(items) > bounded:
        items = items[:bounded]
        next_cursor = int(items[-1]["contract_id"])
    return Page(items=items, next_cursor=next_cursor)


async def get_contract(reader: DuckReader, contract_id: int) -> dict[str, Any] | None:
    """One contract with its per resolution bounds and its coverage summary."""
    columns, rows = await reader.fetch_columns(
        "SELECT * FROM v_contract_full WHERE contract_id = ?", [contract_id]
    )
    if not rows:
        return None
    contract = _rows_to_dicts(columns, rows)[0]
    contract["resolutions"] = (await contract_bounds(reader, contract_id))["resolutions"]
    cov_columns, cov_rows = await reader.fetch_columns(
        "SELECT res_id, count(*) AS chunks, "
        "       count(*) FILTER (WHERE status = 'ok') AS chunks_ok, "
        "       count(*) FILTER (WHERE status = 'empty') AS chunks_empty, "
        "       count(*) FILTER (WHERE status = 'error') AS chunks_error, "
        "       min(range_from) AS have_from, max(range_to) AS have_to, "
        "       coalesce(sum(row_count), 0) AS rows "
        "  FROM candle_coverage WHERE contract_id = ? GROUP BY res_id ORDER BY res_id",
        [contract_id],
    )
    contract["coverage"] = _rows_to_dicts(cov_columns, cov_rows)
    return contract


async def contract_bounds(reader: DuckReader, contract_id: int) -> dict[str, Any]:
    """First and last timestamp per resolution, in UTC seconds.

    This is the single lookup the chart makes on every symbol change. Without it every expired
    contract renders as "No bars", because the chart's default window is the live present and an
    expired contract has nothing there.
    """
    symbol = await reader.fetch_value(
        "SELECT fyers_symbol FROM dim_contract WHERE contract_id = ?", [contract_id]
    )
    columns, rows = await reader.fetch_columns(
        "SELECT b.res_id, r.fyers_code, r.chart_interval, "
        f"       epoch(b.first_ts) - {IST_OFFSET_SECONDS} AS first_ts, "
        f"       epoch(b.last_ts)  - {IST_OFFSET_SECONDS} AS last_ts, "
        "       b.row_count AS rows "
        "  FROM contract_bounds b LEFT JOIN dim_resolution r USING (res_id) "
        " WHERE b.contract_id = ? ORDER BY r.seconds",
        [contract_id],
    )
    return {
        "contract_id": contract_id,
        "fyers_symbol": symbol,
        "resolutions": _rows_to_dicts(columns, rows),
    }


async def resolutions(reader: DuckReader) -> list[dict[str, Any]]:
    """The resolution reference, which the interval pills and the planner both read."""
    columns, rows = await reader.fetch_columns(
        "SELECT * FROM dim_resolution ORDER BY seconds"
    )
    return _rows_to_dicts(columns, rows)
