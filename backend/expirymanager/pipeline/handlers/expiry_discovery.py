"""Task kind `expiry_dates`: which expiries exist for one underlying.

Level 1 of the four level decomposition. One request answers up to a 366 day window, and the
window is a hard error rather than a truncation, so the caller has to get it right rather than
discover the boundary at the cost of a request.

Two facts this handler is careful about.

The expiry date it records is the one the broker returned, never one inferred from a last Thursday
or last Tuesday rule. NSE and BSE expiry weekdays have changed several times since 2022 and any
hardcoded rule silently corrupts historical rows, which is the sort of corruption nobody notices
until a chain query for an old expiry comes back empty.

`data.symbol` is the authoritative root echo. The registry stores what the user typed; this is
what the broker actually resolved it to, and it is the only thing that can confirm that
NSE:NIFTY50-INDEX is the underlying the F and O contracts hang off.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

from sqlalchemy import text

from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.db.writer import CallableWrite
from expirymanager.db.writes import ExpiryRow, upsert_expiries
from expirymanager.pipeline.handlers.candle_chunk import (
    ensure_underlying_mirror,
    fatal,
    raise_for_response,
    refuse_mcx,
    require_date,
    require_int,
    require_text,
    services_from,
)
from expirymanager.pipeline.queue import TaskOutcome
from expirymanager.pipeline.worker import HandlerContext, HandlerError, register_handler

__all__ = [
    "MAX_WINDOW_DAYS",
    "DEFAULT_TRAILING_DAYS",
    "LAST_SERVED_DAY_OFFSET",
    "last_served_day",
    "expiry_dates_from",
    "handle_expiry_dates",
    "install",
]

log = logging.getLogger(__name__)

# Documented and measured: the endpoint errors rather than truncating past this, and it echoes
# from_date and to_date back unchanged, so there is no silent clamp to lean on.
MAX_WINDOW_DAYS = 366

# What a task that carries no explicit range gets. A trailing year is the window the nightly
# expiry_discovery schedule wants, and one call covers it.
DEFAULT_TRAILING_DAYS = 365

# How far back the newest day the endpoint will serve sits from today. Measured against the live
# service on 2026-09-10: a window ending on today answered HTTP 422 code -50, the same window
# ending yesterday answered HTTP 200 with 26 option expiries, and a window reaching 60 days into
# the future answered 422 as well. The endpoint serves expired contracts, so it refuses any range
# that reaches the present rather than trimming it. Without this clamp the nightly 18:00 schedule,
# which builds a window ending today plus a forward margin, would spend one request per underlying
# every night to be told Invalid input.
LAST_SERVED_DAY_OFFSET = 1


def last_served_day(today: date) -> date:
    """The newest range_to the expiry-dates endpoint will accept."""
    return today - timedelta(days=LAST_SERVED_DAY_OFFSET)


def expiry_dates_from(payload: Any) -> tuple[list[date], list[date]]:
    """Pull the two arrays out of `data.expiry_dates`, tolerating either being absent."""
    if not isinstance(payload, Mapping):
        return ([], [])
    block = payload.get("expiry_dates")
    if not isinstance(block, Mapping):
        return ([], [])
    return (_dates(block.get("futures")), _dates(block.get("options")))


def _dates(values: Any) -> list[date]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        return []
    out: list[date] = []
    for value in values:
        try:
            out.append(date.fromisoformat(str(value).strip()))
        except ValueError:
            log.warning("expiry-dates returned a value that is not a date", extra={"value": str(value)[:32]})
    return sorted(set(out))


def _last_of_month(days: Iterable[date]) -> set[date]:
    """Which of the returned expiries is the last one in its own calendar month.

    Derived from the response set rather than from a rule, and only within the window that was
    asked for, which is why the value is written as `expiry_cycle_source = 'derived'`. The
    authoritative W or M flag comes from the live option chain, in chain_snapshot.
    """
    latest: dict[tuple[int, int], date] = {}
    for day in days:
        key = (day.year, day.month)
        if key not in latest or day > latest[key]:
            latest[key] = day
    return set(latest.values())


async def handle_expiry_dates(ctx: HandlerContext) -> TaskOutcome:
    task = ctx.task
    services = services_from(ctx)

    symbol = require_text(task.fyers_symbol, "fyers_symbol")
    refuse_mcx(symbol)
    underlying_id = require_int(task.underlying_id, "underlying_id")

    # Before the request, so a registry row that cannot be mirrored costs nothing to discover.
    # dim_expiry rows written below are meaningless without the parent row every reader joins to.
    await ensure_underlying_mirror(services, underlying_id)

    if task.range_from and task.range_to:
        range_from = require_date(task.range_from, "range_from")
        range_to = require_date(task.range_to, "range_to")
    else:
        range_to = datetime.now().date()
        range_from = range_to - timedelta(days=DEFAULT_TRAILING_DAYS)

    # The clamp lives here rather than only in the callers because this is the line that spends
    # the request. A window ending today or later is refused outright by the endpoint, so trimming
    # it back to the last served day turns a guaranteed 422 into the answer the caller wanted.
    latest = last_served_day(datetime.now().date())
    if range_to > latest:
        range_to = latest
    if range_from > range_to:
        raise HandlerError(
            fatal(
                f"an expiry-dates window from {range_from} cannot end on or before {range_to}, "
                "the newest day the endpoint serves"
            )
        )
    span = (range_to - range_from).days + 1
    if span > MAX_WINDOW_DAYS:
        raise HandlerError(
            fatal(
                f"an expiry-dates window of {span} days exceeds the {MAX_WINDOW_DAYS} day limit, "
                "which the endpoint answers with an error rather than a truncation"
            )
        )

    response = await ep.expiry_dates(
        services.require("client"),
        symbol=symbol,
        range_from=range_from,
        range_to=range_to,
    )
    raise_for_response(response, symbol)

    payload = response.data
    futures, options = expiry_dates_from(payload)
    every = sorted(set(futures) | set(options))
    monthlies = _last_of_month(every)

    discovered_at = datetime.now()
    rows = [
        ExpiryRow(
            underlying_id=underlying_id,
            expiry_date=day,
            expiry_cycle_derived="M" if day in monthlies else "W",
            expiry_cycle_source="derived",
            is_last_of_month=day in monthlies,
            source_range_from=range_from,
            source_range_to=range_to,
            discovered_at=discovered_at,
            discovered_task_id=task.task_id,
        )
        for day in every
    ]

    writer = services.require("writer")
    if rows:
        await upsert_expiries(writer, rows)
        await _mark_instrument_availability(
            writer, underlying_id, futures=set(futures), options=set(options)
        )
    _record_root_echo(services.engine, underlying_id, payload)

    return TaskOutcome(
        state="done" if rows else "empty",
        http_status=response.http_status,
        fyers_s=response.status,
        fyers_code=response.code,
        latency_ms=response.latency_ms,
        response_bytes=response.response_bytes,
        row_count=len(rows),
        first_ts=every[0].isoformat() if every else None,
        last_ts=every[-1].isoformat() if every else None,
        payload_sha256=response.payload_sha256,
        token_fingerprint=services.token_fingerprint,
        rows_written=len(rows),
        bytes_downloaded=response.response_bytes,
        requests_used=1,
    )


async def _mark_instrument_availability(
    writer: Any, underlying_id: int, *, futures: set[date], options: set[date]
) -> None:
    """Record which expiries carry futures and which carry options.

    ORed rather than assigned. The contract batch sets the same two flags from the contracts it
    actually saw, and a discovery call limited to a narrow window must never unset a flag that a
    contract discovery has already proved true.
    """
    if not futures and not options:
        return

    def apply(cur: Any) -> int:
        touched = 0
        for column, days in (("has_futures", futures), ("has_options", options)):
            if not days:
                continue
            placeholders = ", ".join("?" for _ in days)
            cur.execute(
                f"UPDATE dim_expiry SET {column} = TRUE"
                f" WHERE underlying_id = ? AND expiry_date IN ({placeholders})",
                [underlying_id, *sorted(days)],
            )
            touched += len(days)
        return touched

    await writer.submit(CallableWrite(label="expiry_instrument_flags", fn=apply))


def _record_root_echo(engine: Any, underlying_id: int, payload: Any) -> None:
    """Store the exact `data.symbol` the broker answered with.

    This is the only confirmation that the registered underlying resolves to the root the F and O
    contracts are actually filed under, and it costs nothing because the call was made anyway.
    """
    if engine is None or not isinstance(payload, Mapping):
        return
    echo = payload.get("symbol")
    if not isinstance(echo, str) or not echo.strip():
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE underlying_registry SET resolved_root_echo = :echo, resolved_at = :now"
                " WHERE underlying_id = :underlying_id"
            ),
            {
                "echo": echo.strip(),
                "now": datetime.now().isoformat(timespec="seconds"),
                "underlying_id": underlying_id,
            },
        )


def install() -> None:
    register_handler("expiry_dates", handle_expiry_dates, replace=True)
