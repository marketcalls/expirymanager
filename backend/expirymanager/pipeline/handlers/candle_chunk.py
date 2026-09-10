"""The candle chunk handler, and the plumbing every other handler shares.

This is the hot path: one row of `task`, one governed Fyers request, one DuckDB transaction, one
ack. Everything else in the pipeline exists to feed this function and to record what it did.

Five properties are load bearing and are the reason the code is shaped the way it is.

1. **The governor is reached only through FyersClient.** No handler ever touches httpx and no
   handler takes a second governor slot. `FyersClient._send` holds `governor.slot(endpoint)` for
   the duration of the request, which makes the daily counter equal to the number of requests
   actually sent. A second acquisition would double count a budget whose fourth per minute
   violation costs the rest of the trading day.

2. **Candle fields are addressed by the returned `columns` array.** `open_interest` is the
   seventh element only when `include_oi=1` was asked for, and `include_greeks` will append more
   when it ships, so any fixed index is a future silent corruption. The mapping itself lives in
   `db/arrow.py`, which also does the epoch seconds to naive IST conversion.

3. **The three outcomes are genuinely different things.** `s == "ok"` writes rows. `s ==
   "no_data"` is a success with zero rows: it records a coverage row so the window is never
   requested again, marks the older chunks for that contract skipped and seals the contract. Only
   `s == "error"` is a failure, and what the queue does about it is decided once, by
   `errors.classify`, and never here.

4. **Commit first, ack second.** The DuckDB write is awaited to completion before the outcome is
   returned to the worker, which acks. A crash in between replays exactly one chunk, and the chunk
   write is idempotent by construction (delete then insert over the exact half-open IST window of
   the request that produced the rows), so the replay costs one request and nothing else. The
   reverse order would lose data.

5. **Backward probing happens after the ack decision, not before it.** A NIFTY weekly option
   trades for days, so emitting sixteen 100 day chunks per contract per resolution up front is
   fifteen wasted requests. The planner emits the chunk ending on the expiry date and this handler
   decides, from what came back, whether one more older chunk is worth a request.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import text

from expirymanager.brokers.fyers import calendar as calendar_module
from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers.client import encode_query
from expirymanager.brokers.fyers.errors import Classification, RetryClass
from expirymanager.brokers.fyers.roots import SEGMENT_CODES
from expirymanager.brokers.fyers.symbology import EXCHANGE_CODES
from expirymanager.db import arrow as arrow_module
from expirymanager.db.writer import CallableWrite, CoverageRow
from expirymanager.db.writes import UnderlyingRow, upsert_candle_chunk, upsert_underlying
from expirymanager.pipeline.queue import TaskOutcome, iso_at, utc_now
from expirymanager.pipeline.worker import HandlerContext, HandlerError, register_handler

__all__ = [
    "MCX_REFUSAL",
    "MAX_BACKWARD_PROBE_STEPS",
    "HandlerServices",
    "services_from",
    "fatal",
    "refuse_mcx",
    "symbol_encoding_note",
    "raise_for_response",
    "resolution_id",
    "trading_calendar",
    "ensure_underlying_mirror",
    "clear_caches",
    "ingest_candles",
    "handle_candle_chunk",
    "install",
    "install_all",
]

log = logging.getLogger(__name__)

# Measured on 2026-09-09 against the live API. Seven MCX underlying forms all answered HTTP 422
# while BSE:SENSEX-INDEX answered 200 in the same run, so this is a property of the endpoint and
# not of any one symbol string. Refusing it here costs nothing and saves a whole plan's worth of
# requests spent learning the same thing one 422 at a time.
MCX_REFUSAL = (
    "MCX is not served by the expired F and O endpoints. Every MCX underlying form probed on "
    "2026-09-09 answered HTTP 422 while BSE:SENSEX-INDEX answered 200 in the same run, so this "
    "request would spend budget to receive an error. Register an NSE or a BSE underlying instead."
)

# How far the backward walk may step over already covered ground before giving up and planning
# nothing. Bounded so a contract whose whole history is already held cannot spin here.
MAX_BACKWARD_PROBE_STEPS = 24

# The Unix epoch as a naive datetime, so an epoch second can be turned into an IST calendar day
# without utcfromtimestamp, which is deprecated and returns a naive value anyway.
_EPOCH = datetime(1970, 1, 1)

_RESOLUTION_IDS: dict[str, int] = {}
_CALENDARS: dict[str, calendar_module.TradingCalendar] = {}
_EMPTY_CALENDAR_WARNED: set[str] = set()


# ---------------------------------------------------------------------------
# What a handler is allowed to reach
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HandlerServices:
    """The five things a handler needs, resolved once per task.

    A handler is given `ctx.services`, which in production is the lifespan's AppState. Reading the
    fields through this resolver rather than off AppState directly is what lets a test drive a
    handler with four fakes and no application at all, and it is also the only place that knows
    AppState spells the client `fyers_client` and hangs the writer off `duck`.
    """

    client: Any = None
    writer: Any = None
    reader: Any = None
    engine: Any = None
    settings: Any = None
    paths: Any = None
    token_broker: Any = None
    governor: Any = None

    def require(self, name: str) -> Any:
        value = getattr(self, name)
        if value is None:
            raise HandlerError(
                fatal(f"the {name} service is not available, so this task cannot run")
            )
        return value

    @property
    def token_fingerprint(self) -> str | None:
        """The fingerprint of the token that will authorise this request, never the token."""
        broker = self.token_broker
        if broker is None:
            return None
        record = getattr(broker, "record", None)
        return getattr(record, "fingerprint", None)


def services_from(ctx: HandlerContext) -> HandlerServices:
    """Resolve the services for one task, letting `ctx.extras` override any of them."""
    source = ctx.services
    extras: Mapping[str, Any] = ctx.extras or {}

    def pick(*names: str) -> Any:
        for name in names:
            if name in extras:
                return extras[name]
        for name in names:
            value = getattr(source, name, None)
            if value is not None:
                return value
        return None

    return HandlerServices(
        client=pick("client", "fyers_client"),
        writer=pick("writer", "duck_writer"),
        reader=pick("reader", "duck_reader"),
        engine=pick("engine"),
        settings=pick("settings"),
        paths=pick("paths"),
        token_broker=pick("token_broker"),
        governor=pick("governor"),
    )


# ---------------------------------------------------------------------------
# Failure shapes
# ---------------------------------------------------------------------------


def fatal(message: str, *, code: int | None = None, http_status: int | None = None) -> Classification:
    """A failure that is nobody's fault but the request's. Never retried, never parked."""
    return Classification(
        retry_class=RetryClass.FATAL,
        reason=message,
        code=code,
        http_status=http_status,
    )


def refuse_mcx(symbol: str) -> None:
    """Fail fast on an MCX symbol, naming the measured reason rather than a bare Invalid input."""
    if symbol.strip().upper().startswith("MCX:"):
        raise HandlerError(fatal(MCX_REFUSAL), message=MCX_REFUSAL)


def symbol_encoding_note(symbol: str) -> str:
    """The assertion PIPELINE.md section 4 attaches to a -300 invalid symbol.

    The documented cause of -300 is a special character that was not percent encoded, so before
    the symbol is blamed the encoder is asked what actually went on the wire. `M&M` must leave as
    `M%26M`, and a query string that silently truncates at an ampersand looks exactly like a
    symbol the broker does not know.
    """
    encoded = encode_query({"symbol": symbol})
    on_the_wire = encoded.split("=", 1)[1] if "=" in encoded else encoded
    return f"symbol was sent as {on_the_wire}"


def raise_for_response(response: Any, symbol: str) -> None:
    """Turn a failed Fyers response into the one classification the queue acts on.

    `no_data` deliberately does not raise. `classify` gives it the EMPTY class, which the worker
    would honour by acking an empty outcome, but that path writes no coverage row and a window
    with no coverage row is a window this product will pay to request again. A no_data is a
    success with a durable result, so it goes on down the ordinary write path.
    """
    if getattr(response, "is_no_data", False):
        return
    classification = response.classification()
    if classification is None:
        return
    message = classification.reason
    if classification.message:
        message = f"{message}: {classification.message}"
    if classification.check_symbol_encoding:
        message = f"{message} ({symbol_encoding_note(symbol)})"
    raise HandlerError(
        classification,
        message=message,
        latency_ms=response.latency_ms,
        http_status=response.http_status,
    )


# ---------------------------------------------------------------------------
# Small cached lookups
# ---------------------------------------------------------------------------


async def resolution_id(reader: Any, fyers_code: str) -> int:
    """Map a Fyers resolution code onto `dim_resolution.res_id`.

    Cached for the life of the process because dim_resolution is reference data written by the
    schema itself, and this lookup would otherwise run once per chunk on the hottest path there is.
    """
    code = str(fyers_code).strip().upper()
    cached = _RESOLUTION_IDS.get(code)
    if cached is not None:
        return cached
    row = await reader.fetch_one(
        "SELECT res_id FROM dim_resolution WHERE fyers_code = ?", [code]
    )
    if row is None:
        raise HandlerError(fatal(f"{fyers_code!r} is not a resolution this product knows"))
    _RESOLUTION_IDS[code] = int(row[0])
    return _RESOLUTION_IDS[code]


def trading_calendar(engine: Any, exchange: str) -> calendar_module.TradingCalendar:
    """A calendar for one exchange, loaded from market_holiday.

    The degradation is deliberately made visible. With no holiday rows the calendar falls back to
    weekends only, which counts too few non trading days, and the effect on this module is that a
    chunk looks full to its left edge when it is not, so one extra request gets planned. That is
    the cheap direction, but silence about it is what turns a thin seed table into a permanent
    mystery, so the first time an exchange comes back empty it is logged at warning level.
    """
    key = exchange.strip().upper()
    cached = _CALENDARS.get(key)
    if cached is not None:
        return cached
    days: list[date] = []
    if engine is not None:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT holiday_date FROM market_holiday WHERE exchange = :exchange"),
                {"exchange": key},
            ).fetchall()
        days = [date.fromisoformat(str(row[0])) for row in rows]
    calendar = calendar_module.TradingCalendar()
    if days:
        calendar.add_holidays(key, days)
    elif key not in _EMPTY_CALENDAR_WARNED:
        _EMPTY_CALENDAR_WARNED.add(key)
        log.warning(
            "no market holidays are loaded, the trading day calendar is weekends only",
            extra={"exchange": key},
        )
    _CALENDARS[key] = calendar
    return calendar


async def ensure_underlying_mirror(services: HandlerServices, underlying_id: int) -> bool:
    """Make sure dim_underlying carries a row for this underlying. Idempotent.

    underlying_registry in SQLite is the authoritative record of what the user asked for, and
    dim_underlying is its DuckDB mirror. Nine read sites join that mirror: the spot series, the
    ATM pick, the option chain, the catalog listing, the denormalised export, two maintenance
    assertions, the spot id allocator and the backward walk's own bounds query. Until this call
    existed nothing in the running application ever wrote it, so on a fresh install the four
    builtin underlyings seeded by migration 0004 had a registry row and no mirror row, and every
    one of those joins answered empty.

    Measured live on 2026-09-10 before the fix: a real six contract NIFTY download landed 52,873
    candles correctly, then the backward walk's bounds query found no row, returned None and gave
    up without sealing the contract or planning the next chunk. The seal never appeared and a
    re-plan spent six more requests walking a chunk older than the contract's life.

    Discovery is the right place to do it because it is the first thing that touches an underlying
    and it holds both databases open. Returns whether a row was written.
    """
    engine = services.engine
    writer = services.writer
    reader = services.reader
    if engine is None or writer is None or reader is None:
        return False

    existing = await reader.fetch_one(
        "SELECT underlying_id FROM dim_underlying WHERE underlying_id = ?", [underlying_id]
    )
    if existing is not None:
        return False

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT fyers_symbol, root, exchange, segment, instrument_kind, display_name,"
                " data_from, spot_contract_id, is_active"
                " FROM underlying_registry WHERE underlying_id = :underlying_id"
            ),
            {"underlying_id": underlying_id},
        ).fetchone()
    if row is None:
        raise HandlerError(
            fatal(f"underlying {underlying_id} is not in the registry, so it cannot be mirrored")
        )

    exchange = str(row[2]).upper()
    segment = str(row[3]).upper()
    await upsert_underlying(
        writer,
        UnderlyingRow(
            underlying_id=underlying_id,
            fyers_symbol=str(row[0]),
            root=str(row[1]),
            exchange=exchange,
            exchange_code=EXCHANGE_CODES[exchange],
            segment=segment,
            segment_code=SEGMENT_CODES[segment],
            instrument_kind=str(row[4]),
            display_name=str(row[5]),
            data_from=date.fromisoformat(str(row[6])),
            # The registry already holds a reserved spot id, so this is a mirror and not a fresh
            # registration. Passing it through keeps the two tables agreeing on one number.
            spot_contract_id=int(row[7]),
            is_active=bool(row[8]),
        ),
    )
    log.info(
        "mirrored the underlying registry row into dim_underlying",
        extra={"underlying_id": underlying_id},
    )
    return True


def clear_caches() -> None:
    """Drop the reference data caches. Used by tests, never in production."""
    _RESOLUTION_IDS.clear()
    _CALENDARS.clear()
    _EMPTY_CALENDAR_WARNED.clear()


# ---------------------------------------------------------------------------
# Task field access
# ---------------------------------------------------------------------------


def require_text(value: str | None, field: str) -> str:
    if not value:
        raise HandlerError(fatal(f"the task carries no {field}, so no request can be built"))
    return value


def require_int(value: int | None, field: str) -> int:
    if value is None:
        raise HandlerError(fatal(f"the task carries no {field}, so no request can be built"))
    return int(value)


def require_date(value: str | None, field: str) -> date:
    raw = require_text(value, field)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HandlerError(fatal(f"the task {field} {raw!r} is not a yyyy-mm-dd date")) from exc


def _iso_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat(sep=" ")


# ---------------------------------------------------------------------------
# The shared ingest, used by candle_chunk and by spot_chunk
# ---------------------------------------------------------------------------


async def ingest_candles(
    ctx: HandlerContext,
    services: HandlerServices,
    response: Any,
    *,
    contract_id: int,
    res_id: int,
    range_from: date,
    range_to: date,
    include_oi: bool,
    symbol: str,
    force_refresh: bool = False,
) -> TaskOutcome:
    """Make one candle envelope durable and describe what happened on the task row.

    Shared by the expired contract path and the live spot path because the two differ only in
    which endpoint produced the envelope. The write itself is `db/writer.py`'s CandleChunkWrite,
    enqueued through `db/writes.upsert_candle_chunk`: one WriteOp, one transaction, awaited to
    completion before this returns so the worker's ack can only ever follow the commit.
    """
    raise_for_response(response, symbol)

    columns_json = json.dumps(list(response.columns), separators=(",", ":"))
    fingerprint = services.token_fingerprint
    empty = response.is_no_data

    if empty:
        batch = arrow_module.empty_batch()
    else:
        try:
            batch = arrow_module.candles_to_arrow(
                response.candles, response.columns, contract_id, res_id
            )
        except (arrow_module.CandleSchemaError, arrow_module.PrecisionError) as exc:
            # Not retryable. The same payload would map the same way next time, and spending
            # three more governed requests to relearn that is exactly what the budget cannot
            # afford. The columns array is carried into the message so the mismatch is readable.
            raise HandlerError(
                fatal(f"the response could not be mapped onto the candle schema: {exc}"),
                latency_ms=response.latency_ms,
                http_status=response.http_status,
            ) from exc

    coverage = CoverageRow(
        status="empty" if empty else "ok",
        include_oi=include_oi,
        columns_json=columns_json,
        task_id=ctx.task.task_id,
        schema_version=response.schema_version,
        payload_sha256=response.payload_sha256,
        http_status=response.http_status,
        fyers_code=response.code,
        latency_ms=response.latency_ms,
        response_bytes=response.response_bytes,
        token_fingerprint=fingerprint,
    )

    result = await upsert_candle_chunk(
        services.require("writer"),
        contract_id=contract_id,
        res_id=res_id,
        range_from=range_from,
        range_to=range_to,
        coverage=coverage,
        batch=batch,
        # force_refresh means the user asked for the bytes to be rewritten, so the short circuit
        # that exists to avoid rewriting an unchanged settled expiry is exactly what to turn off.
        honour_payload_hash=not force_refresh,
    )

    return TaskOutcome(
        state="empty" if empty else "done",
        http_status=response.http_status,
        fyers_s=response.status,
        fyers_code=response.code,
        latency_ms=response.latency_ms,
        response_bytes=response.response_bytes,
        row_count=response.row_count,
        first_ts=_iso_or_none(result.first_ts),
        last_ts=_iso_or_none(result.last_ts),
        columns_json=columns_json,
        schema_version=response.schema_version,
        payload_sha256=response.payload_sha256,
        token_fingerprint=fingerprint,
        rows_written=result.rows_written,
        bytes_downloaded=response.response_bytes,
        requests_used=1,
    )


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


async def handle_candle_chunk(ctx: HandlerContext) -> TaskOutcome:
    """One expired contract, one resolution, one at most 100 calendar day window."""
    task = ctx.task
    services = services_from(ctx)

    symbol = require_text(task.fyers_symbol, "fyers_symbol")
    refuse_mcx(symbol)
    contract_id = require_int(task.contract_id, "contract_id")
    resolution = require_text(task.resolution, "resolution")
    range_from = require_date(task.range_from, "range_from")
    range_to = require_date(task.range_to, "range_to")
    include_oi = bool(task.include_oi)

    reader = services.require("reader")
    res_id = await resolution_id(reader, resolution)
    force_refresh = _force_refresh(task.request_params_json)

    response = await ep.expired_historical_data(
        services.require("client"),
        symbol=symbol,
        resolution=resolution,
        range_from=range_from,
        range_to=range_to,
        include_oi=include_oi,
    )

    outcome = await ingest_candles(
        ctx,
        services,
        response,
        contract_id=contract_id,
        res_id=res_id,
        range_from=range_from,
        range_to=range_to,
        include_oi=include_oi,
        symbol=symbol,
        force_refresh=force_refresh,
    )

    # Everything past this point is planning, not fetching. It runs after the commit and before
    # the ack, and a failure in it must not turn a chunk that is already durable into a task the
    # queue will replay, so it is deliberately swallowed and logged.
    try:
        await _decide_backward(ctx, services, response, res_id=res_id, contract_id=contract_id)
    except Exception:  # noqa: BLE001 - the data is already committed, this is only planning
        log.exception(
            "backward probing failed after a successful chunk",
            extra={"task_id": task.task_id, "contract_id": contract_id},
        )
    return outcome


def _force_refresh(request_params_json: str | None) -> bool:
    if not request_params_json:
        return False
    try:
        params = json.loads(request_params_json)
    except ValueError:
        return False
    return bool(isinstance(params, Mapping) and params.get("force_refresh"))


# ---------------------------------------------------------------------------
# Backward probing, PIPELINE.md section 1.3
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalkBounds:
    """How far back this contract may ever be requested, and on which calendar."""

    exchange: str
    floor: date
    expiry_date: date | None
    kind: str


async def _decide_backward(
    ctx: HandlerContext,
    services: HandlerServices,
    response: Any,
    *,
    res_id: int,
    contract_id: int,
) -> None:
    task = ctx.task
    range_from = require_date(task.range_from, "range_from")
    range_to = require_date(task.range_to, "range_to")
    reader = services.reader
    engine = services.engine
    writer = services.writer
    if reader is None or engine is None or writer is None:
        return

    if response.is_no_data:
        # A contract's life is contiguous, so a window with no trades means everything older is
        # dead too. Marking those rows skipped in one statement is where most of the request
        # saving comes from on a plan that was not built by backward probing.
        skipped = _skip_older_chunks(engine, task, range_from)
        if skipped:
            log.info(
                "no data ended the backward walk",
                extra={"contract_id": contract_id, "skipped_tasks": skipped},
            )
        await _seal_if_idle(
            engine, writer, contract_id, reason="no_data", exclude_task_id=task.task_id
        )
        return

    bounds = await _walk_bounds(reader, engine, contract_id, task)
    if bounds is None:
        return

    calendar = trading_calendar(engine, bounds.exchange)
    if _life_started_inside(response, calendar, bounds.exchange, range_from):
        await _seal_if_idle(
            engine, writer, contract_id, reason="life_inside_chunk", exclude_task_id=task.task_id
        )
        return

    span_days = (range_to - range_from).days + 1
    planned = await _plan_older_chunk(
        engine, reader, task, bounds=bounds, span_days=span_days, res_id=res_id
    )
    if planned is None:
        await _seal_if_idle(
            engine, writer, contract_id, reason="floor_reached", exclude_task_id=task.task_id
        )


def _life_started_inside(
    response: Any,
    calendar: calendar_module.TradingCalendar,
    exchange: str,
    left_edge: date,
) -> bool:
    """Whether the first candle sits more than one trading day after the chunk's left edge.

    If it does, the contract started trading inside this chunk and there is nothing older to
    fetch. The comparison is in trading days rather than calendar days because a chunk whose left
    edge lands on a Saturday would otherwise always look like it had a gap.
    """
    if not response.candles:
        return True
    positions = arrow_module.map_columns(response.columns)
    first_epoch = int(response.candles[0][positions["ts"]])
    first_day = (
        _EPOCH + timedelta(seconds=first_epoch + arrow_module.IST_OFFSET_SECONDS)
    ).date()
    first_expected = calendar.next_trading_day(exchange, left_edge, inclusive=True)
    return first_day > first_expected


async def _walk_bounds(
    reader: Any, engine: Any, contract_id: int, task: Any
) -> WalkBounds | None:
    """The oldest date this contract may ever be requested from.

    Three clamps, all of which the planner also applies: the exchange availability floor, the
    underlying's own data_from, and the configured life window measured back from the expiry date.
    Recomputing them here rather than carrying them on the task row keeps request_params_json
    exactly what went on the wire, which is what makes it usable as provenance.
    """
    row = await reader.fetch_one(
        "SELECT c.kind, u.exchange, u.data_from, c.expiry_date, c.underlying_id"
        "  FROM dim_contract c"
        "  JOIN dim_underlying u USING (underlying_id)"
        " WHERE c.contract_id = ?",
        [contract_id],
    )
    if row is None:
        return None
    kind = str(row[0])
    exchange = str(row[1])
    data_from = row[2]
    expiry_date = row[3]
    underlying_id = int(row[4])

    if isinstance(data_from, datetime):
        data_from = data_from.date()
    if isinstance(expiry_date, datetime):
        expiry_date = expiry_date.date()
    if expiry_date is None and task.expiry_date:
        expiry_date = date.fromisoformat(task.expiry_date)

    floor = calendar_module.exchange_data_floor(exchange)
    if isinstance(data_from, date):
        floor = max(floor, data_from)

    life_days = _life_days(engine, underlying_id, kind)
    if expiry_date is not None and life_days:
        floor = max(floor, expiry_date - timedelta(days=life_days))

    return WalkBounds(exchange=exchange, floor=floor, expiry_date=expiry_date, kind=kind)


def _life_days(engine: Any, underlying_id: int, kind: str) -> int | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT option_life_days, future_life_days FROM underlying_registry"
                " WHERE underlying_id = :underlying_id"
            ),
            {"underlying_id": underlying_id},
        ).fetchone()
    if row is None:
        return None
    return int(row[1] if str(kind).upper() == "FUT" else row[0])


async def _plan_older_chunk(
    engine: Any,
    reader: Any,
    task: Any,
    *,
    bounds: WalkBounds,
    span_days: int,
    res_id: int,
) -> tuple[date, date] | None:
    """Insert one more chunk further back, skipping over ground already covered.

    Returns the window it planned, or None when there is nothing older left to ask for. The insert
    is conditional on no identical row existing for the job, so a chunk replayed after a crash
    between the commit and the ack cannot plan its successor twice.
    """
    span = max(1, min(span_days, calendar_module.MAX_DAYS_PER_REQUEST))
    range_from = require_date(task.range_from, "range_from")
    contract_id = require_int(task.contract_id, "contract_id")

    end = range_from - timedelta(days=1)
    for _ in range(MAX_BACKWARD_PROBE_STEPS):
        if end < bounds.floor:
            return None
        start = max(bounds.floor, end - timedelta(days=span - 1))
        if not await _already_covered(reader, contract_id, res_id, start, end):
            _insert_followup(engine, task, start, end)
            return (start, end)
        end = start - timedelta(days=1)
    return None


async def _already_covered(
    reader: Any, contract_id: int, res_id: int, start: date, end: date
) -> bool:
    """Whether candle_coverage already holds this whole window with an ok or empty status."""
    row = await reader.fetch_one(
        "SELECT count(*) FROM candle_coverage"
        " WHERE contract_id = ? AND res_id = ? AND status IN ('ok', 'empty')"
        "   AND range_from <= ? AND range_to >= ?",
        [contract_id, res_id, start, end],
    )
    return bool(row and int(row[0]) > 0)


_INSERT_FOLLOWUP = """
INSERT INTO task (job_id, seq, kind, state, priority, underlying_id, contract_id, fyers_symbol,
                  expiry_date, resolution, range_from, range_to, include_oi,
                  request_params_json, parent_task_id, attempt, max_attempts, not_before,
                  created_at)
SELECT :job_id, :seq, :kind, 'pending', :priority, :underlying_id, :contract_id, :fyers_symbol,
       :expiry_date, :resolution, :range_from, :range_to, :include_oi,
       :request_params_json, :parent_task_id, 0, :max_attempts, :now, :now
 WHERE NOT EXISTS (
     SELECT 1 FROM task
      WHERE job_id = :job_id AND kind = :kind AND contract_id = :contract_id
        AND resolution = :resolution AND range_from = :range_from AND range_to = :range_to)
"""


def _insert_followup(engine: Any, task: Any, start: date, end: date) -> bool:
    """Write one follow-up task row and keep the job's own totals honest.

    total_tasks and est_requests are bumped in the same transaction because the progress bar reads
    the fresh GROUP BY for its numerator and the job row for its denominator, and a denominator
    that lags makes a growing backfill look like it is going backwards.
    """
    now = iso_at(utc_now())
    params = dict(_request_params(task, start, end))
    row = {
        "job_id": task.job_id,
        "seq": 0,
        "kind": task.kind,
        "priority": task.priority,
        "underlying_id": task.underlying_id,
        "contract_id": task.contract_id,
        "fyers_symbol": task.fyers_symbol,
        "expiry_date": task.expiry_date,
        "resolution": task.resolution,
        "range_from": start.isoformat(),
        "range_to": end.isoformat(),
        "include_oi": task.include_oi,
        "request_params_json": json.dumps(params, separators=(",", ":"), sort_keys=True),
        "parent_task_id": task.task_id,
        "max_attempts": task.max_attempts,
        "now": now,
    }
    with engine.begin() as connection:
        row["seq"] = int(
            connection.execute(
                text("SELECT coalesce(max(seq), -1) + 1 FROM task WHERE job_id = :job_id"),
                {"job_id": task.job_id},
            ).scalar_one()
        )
        inserted = connection.execute(text(_INSERT_FOLLOWUP), row).rowcount == 1
        if inserted:
            connection.execute(
                text(
                    "UPDATE job SET total_tasks = total_tasks + 1,"
                    " est_requests = est_requests + 1 WHERE job_id = :job_id"
                ),
                {"job_id": task.job_id},
            )
    return inserted


def _request_params(task: Any, start: date, end: date) -> Mapping[str, Any]:
    """The verbatim query parameters for the follow-up chunk. Never a token."""
    base: dict[str, Any] = {}
    if task.request_params_json:
        try:
            loaded = json.loads(task.request_params_json)
            if isinstance(loaded, Mapping):
                base.update(loaded)
        except ValueError:
            pass
    base.update(
        {
            "symbol": task.fyers_symbol,
            "resolution": task.resolution,
            "date_format": 1,
            "range_from": start.isoformat(),
            "range_to": end.isoformat(),
        }
    )
    return base


_SKIP_OLDER = """
UPDATE task
   SET state = 'skipped', finished_at = :now,
       last_error_text = 'older than a no_data boundary for this contract'
 WHERE job_id = :job_id
   AND kind = 'candle_chunk'
   AND state = 'pending'
   AND contract_id = :contract_id
   AND resolution = :resolution
   AND range_to < :range_from
"""


def _skip_older_chunks(engine: Any, task: Any, range_from: date) -> int:
    """Mark every remaining older chunk for this contract skipped, in one statement.

    Scoped to the same job. An older chunk that belongs to somebody else's job is equally dead,
    but silently cancelling another job's work would make a job ledger that nobody can explain,
    and the planner will skip it anyway once the coverage row lands.
    """
    with engine.begin() as connection:
        return connection.execute(
            text(_SKIP_OLDER),
            {
                "now": iso_at(utc_now()),
                "job_id": task.job_id,
                "contract_id": task.contract_id,
                "resolution": task.resolution,
                "range_from": range_from.isoformat(),
            },
        ).rowcount


_OPEN_TASKS_FOR_CONTRACT = """
SELECT count(*) FROM task
 WHERE contract_id = :contract_id
   AND kind = 'candle_chunk'
   AND state IN ('pending', 'leased')
   AND task_id <> :task_id
"""


async def _seal_if_idle(
    engine: Any, writer: Any, contract_id: int, *, reason: str, exclude_task_id: int = -1
) -> bool:
    """Set dim_contract.sealed_at once nothing else is queued for this contract.

    The seal is what turns the nightly sweep into a forward moving frontier, and an expired
    contract's history is immutable so re-fetching it is pure waste. It is deliberately withheld
    while another resolution still has open chunks for the same contract: the seal is per
    contract, not per resolution, and sealing on the first resolution to finish would cut off a
    second resolution that is still mid walk.
    """
    with engine.connect() as connection:
        open_tasks = int(
            connection.execute(
                text(_OPEN_TASKS_FOR_CONTRACT),
                {"contract_id": contract_id, "task_id": exclude_task_id},
            ).scalar_one()
        )
    if open_tasks:
        return False
    sealed_at = datetime.now()

    def seal(cur: Any) -> int:
        row = cur.execute(
            "UPDATE dim_contract SET sealed_at = ? WHERE contract_id = ? AND sealed_at IS NULL",
            [sealed_at, contract_id],
        ).fetchone()
        return int(row[0]) if row else 0

    await writer.submit(CallableWrite(label="seal_contract", fn=seal))
    log.info("contract sealed", extra={"contract_id": contract_id, "reason": reason})
    return True


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def install() -> None:
    """Bind this handler to its task kind."""
    register_handler("candle_chunk", handle_candle_chunk, replace=True)


def install_all() -> None:
    """Register every W13 handler.

    This aggregator lives here rather than in `handlers/__init__.py` because that file belongs to
    the scaffold work item and is not ours to write. Registration is explicit rather than an
    import side effect for the same reason the supervisor's is: a module that registers itself
    merely by being imported changes the behaviour of any test that imports it.
    """
    from expirymanager.pipeline.handlers import chain_snapshot as chain_snapshot_module
    from expirymanager.pipeline.handlers import contract_discovery as contract_discovery_module
    from expirymanager.pipeline.handlers import expiry_discovery as expiry_discovery_module
    from expirymanager.pipeline.handlers import spot_chunk as spot_chunk_module
    from expirymanager.pipeline.handlers import symbol_master as symbol_master_module

    install()
    expiry_discovery_module.install()
    contract_discovery_module.install()
    spot_chunk_module.install()
    symbol_master_module.install()
    chain_snapshot_module.install()
