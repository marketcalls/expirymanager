"""Task kind `spot_chunk`: the underlying's own price series.

The same ingest as an expired contract chunk, against a different endpoint, and the differences
are the whole reason this is a separate handler rather than a flag.

`/data/history` is the live endpoint. It answers with the candle envelope but does **not**
document a `columns` array, so `FyersClient` falls back to the documented order and records on the
response that it did. That is why `columns_from_response` is carried into the coverage row's
columns array unchanged: a reader six months from now must be able to tell a mapping the broker
supplied from one this product assumed.

It also spells the open interest flag `oi_flag` rather than `include_oi`, and it takes `cont_flag`
for a continuous series. `endpoints.py` owns both inconsistencies.

There is no backward probing here. An index spot series is continuous from the exchange floor to
today, so every missing chunk is real work and there is nothing to probe for. There is no sealing
either, for the same reason: a live underlying's history is never finished.
"""

from __future__ import annotations

import logging

from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.pipeline.handlers.candle_chunk import (
    ingest_candles,
    refuse_mcx,
    require_date,
    require_int,
    require_text,
    resolution_id,
    services_from,
)
from expirymanager.pipeline.queue import TaskOutcome
from expirymanager.pipeline.worker import HandlerContext, register_handler

__all__ = ["handle_spot_chunk", "install"]

log = logging.getLogger(__name__)


async def handle_spot_chunk(ctx: HandlerContext) -> TaskOutcome:
    """One underlying, one resolution, one at most 100 calendar day window."""
    task = ctx.task
    services = services_from(ctx)

    symbol = require_text(task.fyers_symbol, "fyers_symbol")
    refuse_mcx(symbol)
    # The rows land under the underlying's reserved spot id in 1..999, which is what every spot
    # bars query joins dim_underlying to.
    contract_id = require_int(task.contract_id, "contract_id")
    resolution = require_text(task.resolution, "resolution")
    range_from = require_date(task.range_from, "range_from")
    range_to = require_date(task.range_to, "range_to")
    include_oi = bool(task.include_oi)

    res_id = await resolution_id(services.require("reader"), resolution)

    response = await ep.history(
        services.require("client"),
        symbol=symbol,
        resolution=resolution,
        range_from=range_from,
        range_to=range_to,
        include_oi=include_oi,
        cont_flag=True,
    )

    if response.ok and not response.columns_from_response:
        log.debug(
            "history returned no columns array, the documented order was assumed",
            extra={"fyers_symbol": symbol, "resolution": resolution},
        )

    return await ingest_candles(
        ctx,
        services,
        response,
        contract_id=contract_id,
        res_id=res_id,
        range_from=range_from,
        range_to=range_to,
        include_oi=include_oi,
        symbol=symbol,
    )


def install() -> None:
    register_handler("spot_chunk", handle_spot_chunk, replace=True)
