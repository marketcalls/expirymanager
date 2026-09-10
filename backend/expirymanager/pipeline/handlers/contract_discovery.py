"""Task kind `underlying_symbols`: which contracts existed for one expiry.

Level 2 of the decomposition, and the point at which the id keyspace is allocated. One request
returns the whole chain for one expiry, futures and options in two arrays, and the measured shape
is one NIFTY weekly answering 482 options and 0 futures with no cap observed on the array.

Three things happen here and the order matters.

The vendor's symbol strings are parsed rather than trusted, with the underlying root and the
requested expiry supplied as hints. The download pipeline always knows both, which collapses every
weekly split ambiguity to a single answer, and parsing then verifies the vendor's string instead
of guessing at facts the caller already holds. `expiry_date` is always the value that was passed
to the request, never one reconstructed from the symbol, because a monthly coded symbol carries no
day at all.

The rows go in through `db/writes.upsert_contracts`, which allocates the contiguous 1024 aligned
id block. Nothing here touches ids.

Then the level below is enqueued. The planner priced this expiry from its siblings' contract
counts because there were no contracts to count yet; now there are, so the same planner is asked
again for exactly this expiry and its candle chunks are appended to the same job. Without that
step a plan containing an undiscovered expiry would discover it and then stop, which is the one
failure mode where the preview the user approved and the work the job does part company.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import text

from expirymanager.api.schemas.downloads import PlanRequest
from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers import roots as roots_module
from expirymanager.brokers.fyers import symbology
from expirymanager.db.writes import ContractRow, upsert_contracts
from expirymanager.pipeline.handlers.candle_chunk import (
    ensure_underlying_mirror,
    raise_for_response,
    refuse_mcx,
    require_date,
    require_int,
    require_text,
    services_from,
)
from expirymanager.pipeline.planner import Planner
from expirymanager.pipeline.queue import TaskOutcome, iso_at, utc_now
from expirymanager.pipeline.worker import HandlerContext, register_handler

__all__ = [
    "SOURCE_ENDPOINT",
    "contract_symbols_from",
    "build_contract_rows",
    "handle_underlying_symbols",
    "install",
]

log = logging.getLogger(__name__)

SOURCE_ENDPOINT = "underlying-symbols"

# Which array a symbol arrived in. Recorded on the row because the vendor's own classification is
# evidence, and a symbol that parses as an option but arrived under futures is a data quality
# alarm rather than something to paper over.
_ARRAYS = ("futures", "options")


def contract_symbols_from(payload: Any) -> dict[str, list[str]]:
    """Pull `data.contracts.futures` and `data.contracts.options` out of the envelope."""
    out: dict[str, list[str]] = {name: [] for name in _ARRAYS}
    if not isinstance(payload, Mapping):
        return out
    block = payload.get("contracts")
    if not isinstance(block, Mapping):
        return out
    for name in _ARRAYS:
        values = block.get(name)
        if isinstance(values, Iterable) and not isinstance(values, (str, bytes, Mapping)):
            out[name] = [str(value).strip() for value in values if str(value).strip()]
    return out


def build_contract_rows(
    arrays: Mapping[str, Sequence[str]],
    *,
    expiry_date: date,
    root: str | None,
    task_id: int,
    registry: Any = None,
) -> tuple[list[ContractRow], list[str]]:
    """Turn the two symbol arrays into dim_contract rows.

    Returns the rows and the symbols that could not be parsed. An unparseable symbol is reported
    rather than fatal: dropping one strike from a 482 contract chain is a gap in one contract,
    while refusing the whole response would lose the other 481 and the id block with them.
    """
    registry = registry or roots_module.default_registry()
    rows: list[ContractRow] = []
    unparseable: list[str] = []
    for array in _ARRAYS:
        for index, symbol in enumerate(arrays.get(array, ())):
            try:
                parsed = symbology.parse_symbol(
                    symbol,
                    registry=registry,
                    expected_root=root,
                    expected_expiry=expiry_date,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not lose the chain
                unparseable.append(symbol)
                log.warning(
                    "a contract symbol could not be parsed",
                    extra={"fyers_symbol": symbol, "reason": type(exc).__name__},
                )
                continue
            rows.append(
                _row(parsed, symbol, array=array, index=index, expiry_date=expiry_date, task_id=task_id)
            )
    return rows, unparseable


def _row(
    parsed: Any,
    symbol: str,
    *,
    array: str,
    index: int,
    expiry_date: date,
    task_id: int,
) -> ContractRow:
    return ContractRow(
        fyers_symbol=symbol,
        kind=parsed.kind,
        instrument_class=parsed.instrument_class or parsed.kind,
        exchange=parsed.exchange,
        exchange_code=parsed.exchange_code,
        segment=parsed.segment,
        segment_code=parsed.segment_code,
        root=parsed.root,
        source_endpoint=SOURCE_ENDPOINT,
        parse_method=parsed.parse_method,
        parse_confidence=parsed.parse_confidence,
        strike=parsed.strike,
        strike_raw=parsed.strike_raw,
        option_type=parsed.option_type,
        # The requested expiry is authoritative. parsed_expiry_date holds whatever the symbol
        # string said, and a disagreement between the two is a fact worth keeping, not one to
        # resolve by preferring the prettier answer.
        expiry_date=expiry_date,
        expiry_year=expiry_date.year,
        expiry_month=expiry_date.month,
        expiry_day=expiry_date.day,
        expiry_dow=expiry_date.isoweekday(),
        parsed_expiry_date=parsed.expiry,
        symbol_expiry_encoding=parsed.symbol_expiry_encoding,
        source_expiry_date_requested=expiry_date,
        source_array=array,
        source_array_index=index,
        discovered_task_id=task_id,
        parse_warnings=json.dumps(list(parsed.parse_warnings)) if parsed.parse_warnings else None,
    )


async def handle_underlying_symbols(ctx: HandlerContext) -> TaskOutcome:
    task = ctx.task
    services = services_from(ctx)

    symbol = require_text(task.fyers_symbol, "fyers_symbol")
    refuse_mcx(symbol)
    underlying_id = require_int(task.underlying_id, "underlying_id")
    expiry_date = require_date(task.expiry_date, "expiry_date")

    # The contract rows written below carry this underlying_id, and the backward walk, the spot
    # series, the chain and the export all join dim_underlying on it. A discovery task may be the
    # first thing that ever touches this underlying, so the mirror is made sure of here too rather
    # than assumed to have been made by the expiry discovery that usually precedes it.
    await ensure_underlying_mirror(services, underlying_id)

    response = await ep.underlying_symbols(
        services.require("client"), symbol=symbol, expiry_date=expiry_date
    )
    raise_for_response(response, symbol)

    payload = response.data
    arrays = contract_symbols_from(payload)
    root = _root_for(services.engine, underlying_id)
    rows, unparseable = build_contract_rows(
        arrays, expiry_date=expiry_date, root=root, task_id=task.task_id
    )

    written = 0
    if rows:
        result = await upsert_contracts(
            services.require("writer"),
            underlying_id=underlying_id,
            expiry_date=expiry_date,
            rows=rows,
            discovered_at=datetime.now(),
        )
        written = result.rows_written
        try:
            planned = await _enqueue_candle_chunks(ctx, services, expiry_date=expiry_date)
        except Exception:  # noqa: BLE001 - the contracts are durable, this is only planning
            log.exception(
                "enqueueing the candle chunks behind a discovery failed",
                extra={"task_id": task.task_id, "expiry_date": expiry_date.isoformat()},
            )
            planned = 0
        log.info(
            "expiry discovered",
            extra={
                "expiry_date": expiry_date.isoformat(),
                "contracts": written,
                "chunks_planned": planned,
            },
        )

    error_text = None
    if unparseable:
        error_text = f"{len(unparseable)} symbols could not be parsed: " + ", ".join(
            unparseable[:5]
        )

    return TaskOutcome(
        state="done" if rows else "empty",
        http_status=response.http_status,
        fyers_s=response.status,
        fyers_code=response.code,
        last_error_text=error_text,
        latency_ms=response.latency_ms,
        response_bytes=response.response_bytes,
        row_count=len(rows),
        payload_sha256=response.payload_sha256,
        token_fingerprint=services.token_fingerprint,
        rows_written=written,
        bytes_downloaded=response.response_bytes,
        requests_used=1,
    )


def _root_for(engine: Any, underlying_id: int) -> str | None:
    if engine is None:
        return None
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT root FROM underlying_registry WHERE underlying_id = :underlying_id"),
            {"underlying_id": underlying_id},
        ).fetchone()
    return None if row is None else str(row[0])


# ---------------------------------------------------------------------------
# Enqueueing the level below
# ---------------------------------------------------------------------------

_INSERT_TASK = """
INSERT INTO task (job_id, seq, kind, state, priority, underlying_id, contract_id, fyers_symbol,
                  expiry_date, resolution, range_from, range_to, include_oi,
                  request_params_json, parent_task_id, attempt, max_attempts, not_before,
                  created_at)
SELECT :job_id, :seq, :kind, 'pending', :priority, :underlying_id, :contract_id, :fyers_symbol,
       :expiry_date, :resolution, :range_from, :range_to, :include_oi,
       :request_params_json, :parent_task_id, 0, :max_attempts, :now, :now
 WHERE NOT EXISTS (
     SELECT 1 FROM task
      WHERE job_id = :job_id AND kind = :kind
        AND coalesce(contract_id, -1) = coalesce(:contract_id, -1)
        AND coalesce(resolution, '') = coalesce(:resolution, '')
        AND coalesce(range_from, '') = coalesce(:range_from, '')
        AND coalesce(range_to, '') = coalesce(:range_to, ''))
"""


async def _enqueue_candle_chunks(
    ctx: HandlerContext, services: Any, *, expiry_date: date
) -> int:
    """Ask the planner for this expiry's chunks and append them to the same job.

    The same planner the user saw the preview from, scoped to one expiry, so the tasks that get
    written are exactly the ones the estimate stood for. Planning costs zero requests, which is
    what makes it safe to run inside a handler that is already holding a lease.
    """
    task = ctx.task
    engine = services.engine
    reader = services.reader
    if engine is None or reader is None:
        return 0

    request = _plan_request_for(engine, task.job_id, expiry_date)
    if request is None:
        return 0

    planner = Planner(
        reader=reader,
        engine=engine,
        settings=services.settings,
        governor=services.governor,
    )
    plan = await planner.plan(request, priority=task.priority)
    chunks = [item for item in plan.tasks if item.kind in ("candle_chunk", "spot_chunk")]
    if not chunks:
        return 0
    return _insert_tasks(engine, task, chunks)


def _plan_request_for(engine: Any, job_id: str, expiry_date: date) -> PlanRequest | None:
    """Rebuild the download sheet from the job's params, narrowed to one expiry.

    Returns None when the job was not created from a sheet, which is the case for a schedule fire
    that only wanted discovery. A job with no sheet has nothing to say about which resolutions or
    which strikes the user wanted, and guessing would spend budget on an answer nobody asked for.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT params_json FROM job WHERE job_id = :job_id"), {"job_id": job_id}
        ).fetchone()
    if row is None or not row[0]:
        return None
    try:
        params = json.loads(row[0])
    except ValueError:
        return None
    if not isinstance(params, Mapping) or "resolutions" not in params:
        return None
    fields = {name: params[name] for name in PlanRequest.model_fields if name in params}
    fields["expiry_dates"] = [expiry_date.isoformat()]
    try:
        return PlanRequest(**fields)
    except Exception:  # noqa: BLE001 - a sheet we cannot rebuild is not one to guess at
        log.warning("a job's params could not be read back as a plan request", extra={"job_id": job_id})
        return None


def _insert_tasks(engine: Any, parent: Any, planned: Sequence[Any]) -> int:
    """Append planned tasks to the parent's job in one transaction.

    Each insert is conditional on no identical row existing for the job, so a discovery replayed
    after a crash between the DuckDB commit and the ack appends nothing the second time.
    """
    now = iso_at(utc_now())
    written = 0
    with engine.begin() as connection:
        seq = int(
            connection.execute(
                text("SELECT coalesce(max(seq), -1) + 1 FROM task WHERE job_id = :job_id"),
                {"job_id": parent.job_id},
            ).scalar_one()
        )
        for item in planned:
            params = dict(item.request_params or {})
            row = {
                "job_id": parent.job_id,
                "seq": seq,
                "kind": item.kind,
                "priority": item.priority,
                "underlying_id": item.underlying_id,
                "contract_id": item.contract_id,
                "fyers_symbol": item.fyers_symbol,
                "expiry_date": item.expiry_date.isoformat() if item.expiry_date else None,
                "resolution": item.resolution,
                "range_from": item.range_from.isoformat() if item.range_from else None,
                "range_to": item.range_to.isoformat() if item.range_to else None,
                "include_oi": 1 if item.include_oi else 0,
                "request_params_json": json.dumps(params, separators=(",", ":"), sort_keys=True),
                "parent_task_id": parent.task_id,
                "max_attempts": parent.max_attempts,
                "now": now,
            }
            if connection.execute(text(_INSERT_TASK), row).rowcount == 1:
                written += 1
                seq += 1
        if written:
            connection.execute(
                text(
                    "UPDATE job SET total_tasks = total_tasks + :n,"
                    " est_requests = est_requests + :n WHERE job_id = :job_id"
                ),
                {"n": written, "job_id": parent.job_id},
            )
    return written


def install() -> None:
    register_handler("underlying_symbols", handle_underlying_symbols, replace=True)
