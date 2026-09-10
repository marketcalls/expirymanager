"""Task kind `chain_snapshot`: one live option chain per underlying per fire.

About four requests a day, and they buy two things nothing else can supply.

`expiryData` carries the broker's own `expiry_flag`, W or M. Everything else in this system derives
the weekly or monthly cycle from the set of expiries it happens to have seen inside one 366 day
discovery window, which is a guess that goes wrong at the edges of the window and whenever an
exchange changes its expiry weekday. The flag is authoritative, so a snapshot upgrades
`dim_expiry.expiry_cycle_source` from `derived` to `api` for every expiry it names.

`optionsChain` carries live greeks and open interest. Those are written to `chain_snapshot` as a
point in time observation. They are deliberately not written to `candle_greeks`, which is keyed by
the contract id of an expired contract: a live chain row is a quote, not a settled candle, and
filing it as one would put unsettled numbers in the table exports read from.

The chain also answers the underlying's own row, with `strike_price` of -1 and an empty
`option_type`, and a separate India VIX quote. Both are folded onto every row of the snapshot so
the observation is self contained.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.db.writer import CallableWrite
from expirymanager.pipeline.handlers.candle_chunk import (
    raise_for_response,
    refuse_mcx,
    require_int,
    require_text,
    services_from,
)
from expirymanager.pipeline.queue import TaskOutcome
from expirymanager.pipeline.worker import HandlerContext, register_handler

__all__ = [
    "DEFAULT_STRIKE_COUNT",
    "SNAPSHOT_COLUMNS",
    "parse_expiry_data",
    "parse_chain_rows",
    "handle_chain_snapshot",
    "install",
]

log = logging.getLogger(__name__)

# Enough strikes each side of the money to describe the smile without turning one snapshot into a
# thousand rows a day. Overridable per task through request_params.
DEFAULT_STRIKE_COUNT = 20

SNAPSHOT_COLUMNS = (
    "snapshot_ts",
    "underlying_id",
    "expiry_date",
    "expiry_flag",
    "strike",
    "option_type",
    "fyers_symbol",
    "ltp",
    "bid",
    "ask",
    "volume",
    "oi",
    "prev_oi",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "fp",
    "india_vix",
    "task_id",
)

_INSERT = (
    "INSERT INTO chain_snapshot (" + ", ".join(SNAPSHOT_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in SNAPSHOT_COLUMNS) + ")"
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        candidate = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return candidate if candidate.is_finite() else None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_expiry_data(payload: Any) -> list[tuple[date, str]]:
    """Read `expiryData` into (expiry_date, flag) pairs.

    The `date` field is dd-mm-yyyy, which is the one place in this API that is not yyyy-mm-dd, so
    it is parsed explicitly rather than handed to date.fromisoformat. The `expiry` field is an
    epoch and is deliberately not used: an epoch boundary interpreted in the wrong timezone is the
    classic off by one day bug, and the printed date has no such ambiguity.
    """
    out: list[tuple[date, str]] = []
    if not isinstance(payload, Mapping):
        return out
    entries = payload.get("expiryData")
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes, Mapping)):
        return out
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw = str(entry.get("date", "")).strip()
        flag = str(entry.get("expiry_flag", "")).strip().upper() or None
        try:
            day = datetime.strptime(raw, "%d-%m-%Y").date()
        except ValueError:
            log.warning("options-chain returned an expiry date it could not parse", extra={"value": raw[:16]})
            continue
        if flag in ("W", "M"):
            out.append((day, flag))
    return out


def parse_chain_rows(payload: Any) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    """Split `optionsChain` into the option legs and the underlying's own row.

    The underlying row is identified by `strike_price` of -1 with a blank option_type, exactly as
    the documented sample shows it, and never by position in the array.
    """
    legs: list[Mapping[str, Any]] = []
    underlying: dict[str, Any] = {}
    if not isinstance(payload, Mapping):
        return (legs, underlying)
    entries = payload.get("optionsChain")
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes, Mapping)):
        return (legs, underlying)
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        option_type = str(entry.get("option_type", "")).strip().upper()
        if option_type in ("CE", "PE"):
            legs.append(entry)
        else:
            underlying = dict(entry)
    return (legs, underlying)


async def handle_chain_snapshot(ctx: HandlerContext) -> TaskOutcome:
    task = ctx.task
    services = services_from(ctx)

    symbol = require_text(task.fyers_symbol, "fyers_symbol")
    refuse_mcx(symbol)
    underlying_id = require_int(task.underlying_id, "underlying_id")
    strike_count = _strike_count(task.request_params_json)

    response = await ep.options_chain(
        services.require("client"), symbol=symbol, strike_count=strike_count, greeks=True
    )
    raise_for_response(response, symbol)

    payload = response.data
    expiries = parse_expiry_data(payload)
    legs, underlying_row = parse_chain_rows(payload)
    snapshot_ts = datetime.now()

    # The chain answers one expiry at a time, the nearest one, and expiryData names it first.
    front = expiries[0] if expiries else (None, None)
    rows = _snapshot_rows(
        legs,
        underlying_id=underlying_id,
        expiry_date=front[0],
        expiry_flag=front[1],
        snapshot_ts=snapshot_ts,
        task_id=task.task_id,
        underlying_row=underlying_row,
        india_vix=_india_vix(payload),
    )

    writer = services.require("writer")
    written = 0
    if rows or expiries:
        written = await writer.submit(
            CallableWrite(
                label="chain_snapshot",
                fn=_apply(rows, expiries, underlying_id),
            )
        )

    return TaskOutcome(
        state="done" if (rows or expiries) else "empty",
        http_status=response.http_status,
        fyers_s=response.status,
        fyers_code=response.code,
        latency_ms=response.latency_ms,
        response_bytes=response.response_bytes,
        row_count=len(rows),
        columns_json=json.dumps(list(SNAPSHOT_COLUMNS), separators=(",", ":")),
        payload_sha256=response.payload_sha256,
        token_fingerprint=services.token_fingerprint,
        rows_written=written,
        bytes_downloaded=response.response_bytes,
        requests_used=1,
    )


def _strike_count(request_params_json: str | None) -> int:
    if not request_params_json:
        return DEFAULT_STRIKE_COUNT
    try:
        params = json.loads(request_params_json)
    except ValueError:
        return DEFAULT_STRIKE_COUNT
    if not isinstance(params, Mapping):
        return DEFAULT_STRIKE_COUNT
    value = _int(params.get("strikecount") or params.get("strike_count"))
    return value if value and value > 0 else DEFAULT_STRIKE_COUNT


def _india_vix(payload: Any) -> Decimal | None:
    """The separate VIX quote the chain carries alongside the underlying."""
    if not isinstance(payload, Mapping):
        return None
    for key in ("indiavixData", "indiaVixData", "indiavix"):
        block = payload.get(key)
        if isinstance(block, Mapping):
            return _decimal(block.get("ltp"))
        value = _decimal(block)
        if value is not None:
            return value
    return None


def _snapshot_rows(
    legs: Sequence[Mapping[str, Any]],
    *,
    underlying_id: int,
    expiry_date: date | None,
    expiry_flag: str | None,
    snapshot_ts: datetime,
    task_id: int,
    underlying_row: Mapping[str, Any],
    india_vix: Decimal | None,
) -> list[list[Any]]:
    if expiry_date is None:
        # Without an expiry the row has no primary meaning, and chain_snapshot.expiry_date is NOT
        # NULL. Better to record nothing than to invent a date for a live quote.
        return []
    forward = _decimal(underlying_row.get("fp"))
    rows: list[list[Any]] = []
    for leg in legs:
        greeks = leg.get("greeks") if isinstance(leg.get("greeks"), Mapping) else {}
        rows.append(
            [
                snapshot_ts,
                underlying_id,
                expiry_date,
                expiry_flag,
                _decimal(leg.get("strike_price")),
                str(leg.get("option_type", "")).strip().upper() or None,
                str(leg.get("symbol", "")).strip() or None,
                _decimal(leg.get("ltp")),
                _decimal(leg.get("bid")),
                _decimal(leg.get("ask")),
                _int(leg.get("volume")),
                _int(leg.get("oi")),
                _int(leg.get("prev_oi")),
                _decimal(greeks.get("iv")),
                _decimal(greeks.get("delta")),
                _decimal(greeks.get("gamma")),
                _decimal(greeks.get("theta")),
                _decimal(greeks.get("vega")),
                forward,
                india_vix,
                task_id,
            ]
        )
    return rows


def _apply(
    rows: Sequence[Sequence[Any]],
    expiries: Sequence[tuple[date, str]],
    underlying_id: int,
):
    """One transaction: the snapshot rows, then the authoritative expiry cycle flags."""

    def run(cur: Any) -> int:
        cur.execute("BEGIN TRANSACTION")
        try:
            for row in rows:
                cur.execute(_INSERT, list(row))
            for day, flag in expiries:
                # Only an expiry this product already knows is updated. The chain lists forward
                # expiries that have not expired yet and therefore have no dim_expiry row, and
                # inventing one here would put an undiscovered expiry in the catalog.
                cur.execute(
                    "UPDATE dim_expiry SET expiry_cycle_derived = ?, expiry_cycle_source = 'api',"
                    " is_last_of_month = ?"
                    " WHERE underlying_id = ? AND expiry_date = ?",
                    [flag, flag == "M", underlying_id, day],
                )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return len(rows)

    return run


def install() -> None:
    register_handler("chain_snapshot", handle_chain_snapshot, replace=True)
