"""Task kind `symbol_master`: the seven public JSON masters, SCD-2 diffed.

The only task kind that consumes no part of the daily budget and needs no token. The files are
unauthenticated public objects on a CDN, measured on 2026-09-10 to answer HTTP 200 with no
Authorization header, no cookie and no token anywhere in the path, so the fetch deliberately does
not go through `FyersClient` or the governor.

That is not an optimisation, it is the point. This is the one job that must keep running while the
token is dead, because a symbol master file is the only chance to record lot size and tick size
before a contract expires and vanishes from it, and a day that is missed is missed permanently.

The work itself is `brokers/fyers/symbol_master.run_symbol_master_snapshot`, which owns the
streaming fetch, the member-at-a-time parse, the type 2 diff and the truncation guard. This module
is only the seam that lets the scheduler reach it through the ordinary task ledger, so a master
pull appears in the same job list with the same progress as everything else.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Sequence

from expirymanager.brokers.fyers import symbol_master as master
from expirymanager.pipeline.handlers.candle_chunk import fatal, services_from
from expirymanager.pipeline.queue import TaskOutcome
from expirymanager.pipeline.worker import HandlerContext, HandlerError, register_handler

__all__ = ["files_from_params", "handle_symbol_master", "install"]

log = logging.getLogger(__name__)


def files_from_params(request_params_json: str | None) -> tuple[str, ...]:
    """Which master files this task wants, defaulting to the documented run order.

    Cash before derivatives per exchange, because the root rebuild joins the derivative rows'
    under_fytoken to fresh cash rows and that join is the only thing that knows BANKNIFTY is
    quoted as NSE:NIFTYBANK-INDEX.
    """
    if not request_params_json:
        return master.DEFAULT_RUN_ORDER
    try:
        params = json.loads(request_params_json)
    except ValueError:
        return master.DEFAULT_RUN_ORDER
    if not isinstance(params, Mapping):
        return master.DEFAULT_RUN_ORDER
    requested = params.get("files")
    if not isinstance(requested, Sequence) or isinstance(requested, (str, bytes)):
        return master.DEFAULT_RUN_ORDER
    names = tuple(str(name).strip() for name in requested if str(name).strip())
    unknown = [name for name in names if name not in master.MASTER_FILES]
    if unknown:
        raise HandlerError(fatal("unknown symbol master files: " + ", ".join(unknown)))
    return names or master.DEFAULT_RUN_ORDER


async def handle_symbol_master(ctx: HandlerContext) -> TaskOutcome:
    services = services_from(ctx)
    files = files_from_params(ctx.task.request_params_json)
    work_dir = _work_dir(services)

    result = await master.run_symbol_master_snapshot(
        writer=services.require("writer"),
        reader=services.require("reader"),
        work_dir=work_dir,
        files=files,
    )

    # One failing file does not fail the task. A day that is missed is missed permanently, so six
    # files are worth strictly more than none, and the failures are recorded on the row where an
    # operator can see which exchange went quiet.
    error_text = None
    if result.failures:
        error_text = "; ".join(f"{name}: {reason}" for name, reason in result.failures)[:2000]
        log.warning("some symbol master files failed", extra={"failures": len(result.failures)})

    return TaskOutcome(
        state="done",
        fyers_s="ok" if result.ok else "error",
        last_error_text=error_text,
        row_count=result.rows_staged,
        columns_json=json.dumps([name for name in files], separators=(",", ":")),
        rows_written=result.new_rows,
        # The masters bypass the governor entirely, so charging the job one request would make the
        # budget ledger disagree with what the governor actually counted.
        requests_used=0,
        bytes_downloaded=sum(item.byte_size for item in result.files),
    )


def _work_dir(services: Any) -> Any:
    paths = services.paths
    if paths is None:
        raise HandlerError(fatal("no paths object is available, so the masters cannot be staged"))
    return paths.tmp_dir


def install() -> None:
    register_handler("symbol_master", handle_symbol_master, replace=True)
