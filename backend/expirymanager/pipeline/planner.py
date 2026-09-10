"""The planner: what a download would cost, decided entirely from local state.

One rule holds this module together and everything else follows from it:

    ASKING FOR A PLAN COSTS ZERO FYERS REQUESTS.

Every number in a `PlanPreview` comes from the SQLite registry, the DuckDB catalog and the
coverage ledger. Nothing here opens a socket, and no download ever starts blind. That is what
lets a user price a 62,000 request backfill against a 100,000 request day before committing to
it, and it is why the same function produces both the estimate and the committed task rows: a
separate estimator would drift from the thing that actually runs, and the drift would only ever
be discovered by spending budget.

Three savings do the heavy lifting, in the order they matter:

1. Backward probing. A naive planner emits sixteen 100 day chunks per contract to cover 2022 to
   today. A NIFTY weekly option trades for days, so fifteen of those are guaranteed empty. The
   planner instead emits ONE chunk, the one ending on the expiry date, and leaves the decision
   about an older one to the handler, which will have seen whether the chunk came back full to
   its left edge. For a weekly this turns sixteen requests per contract per resolution into one.

2. Coverage subtraction and sealing. A chunk already recorded in `candle_coverage` is never
   requested again, and a contract whose whole life is covered is sealed and skipped outright.
   An expired contract's history is immutable, so re-fetching it is pure waste, and skipping it
   is what turns the nightly sweep into a forward-moving frontier that converges.

3. Availability floors. Nothing before the exchange's first served date is ever requested, and a
   second resolution is never requested for an expiry outside the last 30 trading days, because
   that data does not exist and asking for it burns budget for nothing.

The interval arithmetic itself is not reimplemented here. `db/queries/coverage.missing_windows`
is a pure function and is already tested exhaustively, and `brokers/fyers/calendar.chunk_range`
already carries the measured 100 calendar day limit. This module composes them.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, text

from expirymanager.api.schemas.downloads import (
    PlanPreview,
    PlanRequest,
    PlanWarning,
)
from expirymanager.brokers.fyers.calendar import (
    MAX_DAYS_PER_REQUEST,
    RESOLUTION_SECONDS,
    SECONDS_RESOLUTIONS,
    SECONDS_WINDOW_TRADING_DAYS,
    TradingCalendar,
    chunk_range,
    exchange_data_floor,
    partial_candle_cutoff,
)
from expirymanager.db.queries.coverage import merge_ranges, missing_windows

if TYPE_CHECKING:  # pragma: no cover - typing only
    from expirymanager.db.reader import DuckReader

__all__ = [
    "BYTES_PER_ROW",
    "DEFAULT_PER_MINUTE",
    "SESSION_SECONDS",
    "PipelineRequestError",
    "PlannerError",
    "UnderlyingSpec",
    "ResolutionSpec",
    "PlannedTask",
    "Plan",
    "Planner",
    "summarise",
]

log = logging.getLogger(__name__)

# Measured against real Parquet output. Used only for the preview, never for a disk guard.
BYTES_PER_ROW = 15.21

# 09:15 to 15:30 IST. The nominal bar count per trading day is this divided by the resolution.
# It is only the fallback: once any chunk has been fetched at a resolution the planner uses the
# observed average instead, which is both more accurate and self correcting.
SESSION_SECONDS = 22_500

# The governor's effective per minute target, not the published 200. ETA has to be honest about
# the rate the pipeline will actually run at.
DEFAULT_PER_MINUTE = 170

# The moment of an expiry day used to read spot for an ATM band. The close, because that is the
# settlement reference a user means by "at the money on expiry day".
_SESSION_CLOSE = time(15, 30)

# MCX is not served by the expired F&O endpoints. Seven underlying forms all returned 422 in the
# same probe run in which BSE:SENSEX-INDEX returned 200, so a plan against an MCX underlying is
# refused here with the real reason rather than being allowed to spend requests discovering it.
_MCX_MESSAGE = (
    "MCX is not served by the Fyers expired F&O endpoints. Every MCX underlying form returned "
    "HTTP 422 when probed, so no amount of budget will produce data for it."
)


class PipelineRequestError(Exception):
    """An expected refusal, carrying the envelope a route will render.

    The planner and the job service do not import the API error helpers on purpose: `pipeline`
    sits below `api` in the module graph, and a scheduler fire has to be able to catch this
    without a FastAPI import. One base class means a route needs exactly one except clause to
    turn any refusal from this layer into the documented error body.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        detail: Any = None,
    ) -> None:
        super().__init__(f"{status_code} {code}")
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


class PlannerError(PipelineRequestError):
    """A plan that cannot be produced from local state."""


@dataclass(frozen=True, slots=True)
class UnderlyingSpec:
    """The registry row the planner reasons about, already typed."""

    underlying_id: int
    fyers_symbol: str
    root: str
    exchange: str
    segment: str
    instrument_kind: str
    display_name: str
    data_from: date
    default_resolutions: tuple[str, ...]
    include_oi: bool
    option_life_days: int
    future_life_days: int
    spot_contract_id: int
    is_active: bool

    def life_days(self, kind: str) -> int:
        return self.future_life_days if kind == "FUT" else self.option_life_days


@dataclass(frozen=True, slots=True)
class ResolutionSpec:
    """One selected resolution, resolved against `ref_resolution`."""

    fyers_code: str
    res_id: int
    seconds: int
    is_seconds: bool
    max_days: int = MAX_DAYS_PER_REQUEST

    @property
    def chunk_days(self) -> int:
        """Calendar days per request for this resolution.

        100 for everything the probe measured, but a second resolution is additionally bounded by
        `ref_resolution.max_days_per_request`, which encodes an availability window rather than a
        request limit. Asking for 100 days of 5S when only 30 exist spends the same governed
        request and returns the same data, so the smaller bound is the honest one.
        """
        if self.is_seconds:
            return max(1, min(MAX_DAYS_PER_REQUEST, self.max_days))
        return MAX_DAYS_PER_REQUEST

    @property
    def bars_per_trading_day(self) -> int:
        if self.seconds >= 86_400:
            return 1
        return max(1, math.ceil(SESSION_SECONDS / self.seconds))


@dataclass(frozen=True, slots=True)
class PlannedTask:
    """One row that will be written to `task`, and therefore one outbound Fyers request.

    Dates are `date` objects here and become ISO strings at the moment they are inserted, so the
    arithmetic above never has to parse anything back.
    """

    kind: str
    seq: int
    priority: int
    underlying_id: int | None = None
    contract_id: int | None = None
    fyers_symbol: str | None = None
    expiry_date: date | None = None
    resolution: str | None = None
    range_from: date | None = None
    range_to: date | None = None
    include_oi: bool = True
    request_params: Mapping[str, Any] | None = None
    # What this one request is expected to yield. Carried on the planned task rather than only in
    # the total, so a partial commit could still be priced.
    rows_estimated: int = 0


@dataclass(frozen=True, slots=True)
class Plan:
    """The complete answer: what it costs, and exactly what would run."""

    request: PlanRequest
    preview: PlanPreview
    tasks: tuple[PlannedTask, ...]
    job_kind: str
    underlying: UnderlyingSpec
    resolutions: tuple[ResolutionSpec, ...]


@dataclass
class _Tally:
    """Mutable running totals, kept out of the frozen preview until the end."""

    tasks: list[PlannedTask] = field(default_factory=list)
    chunks_skipped_covered: int = 0
    contracts_sealed_skipped: int = 0
    contracts_planned: int = 0
    expiries_planned: int = 0
    discovery_tasks: int = 0
    estimated_downstream: int = 0
    spot_tasks: int = 0
    rows_estimated: int = 0
    warnings: list[PlanWarning] = field(default_factory=list)
    seconds_unavailable_expiries: set[date] = field(default_factory=set)

    def warn(self, code: str, message: str, detail: Any = None) -> None:
        for existing in self.warnings:
            if existing.code == code:
                return
        self.warnings.append(PlanWarning(code=code, message=message, detail=detail))


class Planner:
    """Turns a download sheet into a priced task list without touching the network."""

    def __init__(
        self,
        *,
        reader: "DuckReader",
        engine: Engine,
        settings: Any = None,
        governor: Any = None,
        clock: Any = None,
    ) -> None:
        self._reader = reader
        self._engine = engine
        self._settings = settings
        self._governor = governor
        self._clock = clock or (lambda: datetime.now())

    # -- public surface -----------------------------------------------------

    async def plan(
        self,
        request: PlanRequest,
        *,
        sweep: bool = False,
        probe_backward: bool = True,
        priority: int = 100,
        now: datetime | None = None,
    ) -> Plan:
        """Price a download sheet. Reads local state only, spends nothing.

        `sweep` applies the reserve fraction, so a scheduled backfill can never consume the share
        of the day that is held back for downloads the user starts by hand. `probe_backward` is
        the section 1.3 rule and is only turned off by a repair job that already knows the shape
        of the hole it is filling.
        """
        moment = now or self._now()
        today = moment.date()

        underlying = self._load_underlying(request.underlying_id)
        if underlying.exchange.upper() == "MCX":
            raise PlannerError("mcx_not_served", _MCX_MESSAGE, status_code=400)

        resolutions = self._load_resolutions(request.resolutions)
        calendar, holidays_loaded = self._load_calendar(underlying.exchange)

        tally = _Tally()
        if not holidays_loaded:
            # The safe direction (a contract is treated as outside the seconds window rather than
            # inside it) but lossy for 5S capture, which is the one irreplaceable job. Making the
            # degradation visible is the whole point of this warning.
            tally.warn(
                "holiday_calendar_empty",
                f"No market holidays are loaded for {underlying.exchange}, so trading days fall "
                "back to a weekday rule. The 30 trading day window for second resolutions will "
                "start later than reality and some capturable data may not be offered.",
                {"exchange": underlying.exchange},
            )
        if not underlying.is_active:
            tally.warn(
                "underlying_inactive",
                f"{underlying.display_name} is marked inactive in the registry.",
                {"underlying_id": underlying.underlying_id},
            )

        expiries = await self._load_expiries(underlying.underlying_id, request.expiry_dates)
        missing = [d for d in request.expiry_dates if d not in expiries]
        if missing:
            tally.warn(
                "expiry_not_discovered",
                f"{len(missing)} of the selected expiry dates are not in the catalog yet. Run "
                "expiry discovery for this underlying first.",
                {"expiry_dates": [d.isoformat() for d in missing]},
            )

        known = [d for d in request.expiry_dates if d in expiries]
        undiscovered = [d for d in known if expiries[d].get("contracts_discovered_at") is None]
        discovered = [d for d in known if expiries[d].get("contracts_discovered_at") is not None]

        if known and not discovered:
            # Nothing local to price. Returning a preview built entirely on a guess would be a
            # number the user could not act on, so the sheet is refused with the remedy attached.
            raise PlannerError(
                "no_contracts_discovered",
                "No contracts have been discovered for the selected expiries yet. Discovery has "
                "to run before a download can be priced.",
                status_code=400,
                detail={
                    "discovery_tasks": len(undiscovered),
                    "expiry_dates": [d.isoformat() for d in undiscovered],
                },
            )

        seq = 0
        average_contracts = self._average_contract_count(expiries, discovered)

        # Discovery first, so that within one job the chain arrives before anything tries to
        # download it. Ordering is (priority, job_id, seq) and these share a priority, so seq
        # alone settles it.
        for expiry_date in undiscovered:
            tally.tasks.append(
                PlannedTask(
                    kind="underlying_symbols",
                    seq=seq,
                    priority=priority,
                    underlying_id=underlying.underlying_id,
                    fyers_symbol=underlying.fyers_symbol,
                    expiry_date=expiry_date,
                    include_oi=request.include_oi,
                    request_params={
                        "symbol": underlying.fyers_symbol,
                        "expiry_date": expiry_date.isoformat(),
                    },
                )
            )
            seq += 1
            tally.discovery_tasks += 1
            tally.estimated_downstream += average_contracts * len(resolutions)
        if undiscovered:
            tally.warn(
                "contracts_not_discovered",
                f"{len(undiscovered)} of the selected expiries have no contracts yet. One "
                "discovery request is planned for each, and the candle requests behind them are "
                "estimated from the sibling expiries rather than counted.",
                {
                    "expiry_dates": [d.isoformat() for d in undiscovered],
                    "contracts_per_expiry_assumed": average_contracts,
                },
            )

        observed_rows = await self._observed_rows_per_chunk(
            underlying.underlying_id, [item.res_id for item in resolutions]
        )

        for expiry_date in discovered:
            contracts = await self._load_contracts(
                underlying.underlying_id, expiry_date, request
            )
            if not contracts:
                tally.warn(
                    "no_contracts_in_scope",
                    "Some selected expiries have no contracts matching the instrument class, "
                    "option type and strike scope on the sheet.",
                    {"expiry_date": expiry_date.isoformat()},
                )
                continue
            tally.expiries_planned += 1
            seq = await self._plan_expiry(
                tally=tally,
                seq=seq,
                priority=priority,
                underlying=underlying,
                resolutions=resolutions,
                calendar=calendar,
                contracts=contracts,
                expiry_date=expiry_date,
                request=request,
                observed_rows=observed_rows,
                today=today,
                moment=moment,
                probe_backward=probe_backward,
            )

        if request.include_spot:
            seq = await self._plan_spot(
                tally=tally,
                seq=seq,
                priority=priority,
                underlying=underlying,
                resolutions=resolutions,
                calendar=calendar,
                request=request,
                expiry_dates=known or list(request.expiry_dates),
                observed_rows=observed_rows,
                today=today,
                moment=moment,
            )

        if tally.seconds_unavailable_expiries:
            selected_seconds = [item.fyers_code for item in resolutions if item.is_seconds]
            tally.warn(
                "seconds_window_closed",
                f"{', '.join(selected_seconds)} not available for "
                f"{len(tally.seconds_unavailable_expiries)} of {len(known)} selected expiries. "
                f"Second resolutions exist only inside the last {SECONDS_WINDOW_TRADING_DAYS} "
                "trading days and that data is gone once the window closes.",
                {
                    "expiry_dates": sorted(
                        d.isoformat() for d in tally.seconds_unavailable_expiries
                    )
                },
            )

        preview = self._price(tally, sweep=sweep)
        job_kind = self._job_kind(tally, request)
        return Plan(
            request=request,
            preview=preview,
            tasks=tuple(tally.tasks),
            job_kind=job_kind,
            underlying=underlying,
            resolutions=tuple(resolutions),
        )

    # -- expiry level -------------------------------------------------------

    async def _plan_expiry(
        self,
        *,
        tally: _Tally,
        seq: int,
        priority: int,
        underlying: UnderlyingSpec,
        resolutions: Sequence[ResolutionSpec],
        calendar: TradingCalendar,
        contracts: Sequence[Mapping[str, Any]],
        expiry_date: date,
        request: PlanRequest,
        observed_rows: Mapping[int, int],
        today: date,
        moment: datetime,
        probe_backward: bool,
    ) -> int:
        live_contracts: list[Mapping[str, Any]] = []
        for contract in contracts:
            if contract.get("sealed_at") is not None and not request.force_refresh:
                tally.contracts_sealed_skipped += 1
                continue
            live_contracts.append(contract)
        if not live_contracts:
            return seq
        tally.contracts_planned += len(live_contracts)

        for resolution in resolutions:
            if resolution.is_seconds and not calendar.is_within_seconds_window(
                underlying.exchange, expiry_date, today
            ):
                tally.seconds_unavailable_expiries.add(expiry_date)
                continue

            held = (
                {}
                if request.force_refresh
                else await self._held_ranges(
                    [int(item["contract_id"]) for item in live_contracts], resolution.res_id
                )
            )
            for contract in live_contracts:
                contract_id = int(contract["contract_id"])
                window = self._contract_window(
                    underlying=underlying,
                    contract=contract,
                    expiry_date=expiry_date,
                    resolution=resolution,
                    request=request,
                    moment=moment,
                    calendar=calendar,
                    today=today,
                )
                if window is None:
                    continue
                range_from, range_to = window
                chunks = chunk_range(
                    range_from,
                    range_to,
                    max_days=resolution.chunk_days,
                    newest_first=True,
                )
                emitted = self._emit_chunks(
                    tally=tally,
                    seq=seq,
                    priority=priority,
                    underlying=underlying,
                    contract=contract,
                    expiry_date=expiry_date,
                    resolution=resolution,
                    request=request,
                    chunks=chunks,
                    held=held.get(contract_id, ()),
                    calendar=calendar,
                    observed_rows=observed_rows,
                    probe_backward=probe_backward,
                )
                seq += emitted
        return seq

    def _emit_chunks(
        self,
        *,
        tally: _Tally,
        seq: int,
        priority: int,
        underlying: UnderlyingSpec,
        contract: Mapping[str, Any],
        expiry_date: date,
        resolution: ResolutionSpec,
        request: PlanRequest,
        chunks: Sequence[tuple[date, date]],
        held: Sequence[tuple[date, date, str, int]],
        calendar: TradingCalendar,
        observed_rows: Mapping[int, int],
        probe_backward: bool,
    ) -> int:
        """Walk one contract's chunks newest first and emit what is actually missing.

        The walk stops at the first chunk it emits when `probe_backward` is on. That is section
        1.3: the handler will see whether the chunk came back full to its left edge and only then
        does an older chunk get planned. Emitting all sixteen up front is the difference between
        a feasible backfill and an infeasible one.

        It also stops at a chunk that is already recorded as `empty` with no rows. An expired
        contract's life is contiguous, so an empty newer chunk means everything older is empty
        too, and the remaining chunks are not counted as skipped coverage because they were never
        held in the first place.
        """
        held_ranges = [(item[0], item[1]) for item in held]
        empty_ranges = merge_ranges(
            [(item[0], item[1]) for item in held if item[2] == "empty" and item[3] == 0]
        )
        emitted = 0
        for chunk_from, chunk_to in chunks:
            if self._fully_covered(empty_ranges, chunk_from, chunk_to):
                # This chunk is a recorded hole in the data, not a hole in our coverage.
                tally.chunks_skipped_covered += 1
                break
            if not missing_windows(held_ranges, chunk_from, chunk_to):
                tally.chunks_skipped_covered += 1
                continue
            rows = self._rows_for_chunk(
                exchange=underlying.exchange,
                calendar=calendar,
                chunk_from=chunk_from,
                chunk_to=chunk_to,
                resolution=resolution,
                observed_rows=observed_rows,
                tally=tally,
            )
            tally.tasks.append(
                PlannedTask(
                    kind="candle_chunk",
                    seq=seq + emitted,
                    priority=priority,
                    underlying_id=underlying.underlying_id,
                    contract_id=int(contract["contract_id"]),
                    fyers_symbol=str(contract["fyers_symbol"]),
                    expiry_date=expiry_date,
                    resolution=resolution.fyers_code,
                    range_from=chunk_from,
                    range_to=chunk_to,
                    include_oi=request.include_oi,
                    request_params={
                        "symbol": str(contract["fyers_symbol"]),
                        "resolution": resolution.fyers_code,
                        "date_format": 1,
                        "range_from": chunk_from.isoformat(),
                        "range_to": chunk_to.isoformat(),
                        "cont_flag": 0,
                        "oi_flag": 1 if request.include_oi else 0,
                    },
                    rows_estimated=rows,
                )
            )
            tally.rows_estimated += rows
            emitted += 1
            if probe_backward:
                break
        return emitted

    # -- spot ---------------------------------------------------------------

    async def _plan_spot(
        self,
        *,
        tally: _Tally,
        seq: int,
        priority: int,
        underlying: UnderlyingSpec,
        resolutions: Sequence[ResolutionSpec],
        calendar: TradingCalendar,
        request: PlanRequest,
        expiry_dates: Sequence[date],
        observed_rows: Mapping[int, int],
        today: date,
        moment: datetime,
    ) -> int:
        """Spot bars for the underlying itself, over the union of the selected expiries.

        Backward probing does not apply here. An index spot series is continuous from the
        exchange floor to today, so there is nothing to probe: every missing chunk is real work
        and emitting all of them is what makes an incremental spot top up a single job.
        """
        if not expiry_dates:
            return seq
        want_from = request.range_from or min(expiry_dates)
        want_from = max(want_from, underlying.data_from, exchange_data_floor(underlying.exchange))
        for resolution in resolutions:
            if resolution.is_seconds:
                start, end = calendar.seconds_window(underlying.exchange, today)
                spot_from = max(want_from, start)
                spot_to = min(request.range_to or max(expiry_dates), end)
            else:
                spot_from = want_from
                spot_to = request.range_to or max(expiry_dates)
            spot_to = self._clamp_partial(spot_to, resolution, moment)
            if spot_to < spot_from:
                continue
            held = (
                ()
                if request.force_refresh
                else (
                    await self._held_ranges([underlying.spot_contract_id], resolution.res_id)
                ).get(underlying.spot_contract_id, ())
            )
            held_ranges = [(item[0], item[1]) for item in held]
            # The same chunk grid the contract path uses, so a skipped chunk here means exactly
            # what it means there: this window is already recorded and will not be requested.
            wanted: list[tuple[date, date]] = []
            for chunk in chunk_range(spot_from, spot_to, max_days=resolution.chunk_days):
                if missing_windows(held_ranges, chunk[0], chunk[1]):
                    wanted.append(chunk)
                else:
                    tally.chunks_skipped_covered += 1
            for chunk_from, chunk_to in reversed(wanted):
                rows = self._rows_for_chunk(
                    exchange=underlying.exchange,
                    calendar=calendar,
                    chunk_from=chunk_from,
                    chunk_to=chunk_to,
                    resolution=resolution,
                    observed_rows=observed_rows,
                    tally=tally,
                )
                tally.tasks.append(
                    PlannedTask(
                        kind="spot_chunk",
                        seq=seq,
                        priority=priority,
                        underlying_id=underlying.underlying_id,
                        contract_id=underlying.spot_contract_id,
                        fyers_symbol=underlying.fyers_symbol,
                        resolution=resolution.fyers_code,
                        range_from=chunk_from,
                        range_to=chunk_to,
                        include_oi=False,
                        request_params={
                            "symbol": underlying.fyers_symbol,
                            "resolution": resolution.fyers_code,
                            "date_format": 1,
                            "range_from": chunk_from.isoformat(),
                            "range_to": chunk_to.isoformat(),
                            "cont_flag": 1,
                            "oi_flag": 0,
                        },
                        rows_estimated=rows,
                    )
                )
                tally.rows_estimated += rows
                tally.spot_tasks += 1
                seq += 1
        return seq

    # -- windows and arithmetic ---------------------------------------------

    def _contract_window(
        self,
        *,
        underlying: UnderlyingSpec,
        contract: Mapping[str, Any],
        expiry_date: date,
        resolution: ResolutionSpec,
        request: PlanRequest,
        moment: datetime,
        calendar: TradingCalendar,
        today: date,
    ) -> tuple[date, date] | None:
        """The inclusive fetch window for one contract at one resolution, fully clamped."""
        kind = str(contract.get("kind") or "OPT")
        life = underlying.life_days(kind)
        range_to = expiry_date
        if request.range_to is not None:
            range_to = min(range_to, request.range_to)
        range_from = expiry_date - timedelta(days=life)
        range_from = max(range_from, underlying.data_from)
        # The exchange floor is the hard one: requesting before it is a guaranteed empty response
        # that still costs a governed request.
        range_from = max(range_from, exchange_data_floor(underlying.exchange))
        if request.range_from is not None:
            range_from = max(range_from, request.range_from)
        if resolution.is_seconds:
            # Second resolutions exist only inside the last 30 trading days. The expiry itself is
            # already checked against that window; this stops the 200 day life window reaching
            # back past its left edge, where every request would be a governed call for data that
            # was never retained.
            window_start, _ = calendar.seconds_window(underlying.exchange, today)
            range_from = max(range_from, window_start)
        range_to = self._clamp_partial(range_to, resolution, moment)
        if range_to < range_from:
            return None
        return range_from, range_to

    def _clamp_partial(
        self, range_to: date, resolution: ResolutionSpec, moment: datetime
    ) -> date:
        """Never let a window reach a bar that is still forming.

        Fyers documents that `range_to` must sit at least one resolution period before now for
        the response to contain only completed candles. An expired contract never reaches the
        present, so this is a no-op for the common case and matters only for spot top ups.
        """
        if range_to < moment.date():
            return range_to
        cutoff = partial_candle_cutoff(resolution.fyers_code, moment).date()
        return min(range_to, cutoff)

    def _rows_for_chunk(
        self,
        *,
        exchange: str,
        calendar: TradingCalendar,
        chunk_from: date,
        chunk_to: date,
        resolution: ResolutionSpec,
        observed_rows: Mapping[int, int],
        tally: _Tally,
    ) -> int:
        """Expected rows from one request.

        The observed average of chunks already fetched at this resolution beats any formula,
        because it already carries the truth that a weekly option only trades for a few days of
        a 100 day window. The nominal calculation is the fallback for a first ever download, and
        it says so in a warning rather than pretending to a precision it does not have.
        """
        observed = observed_rows.get(resolution.res_id)
        if observed:
            return int(observed)
        tally.warn(
            "rows_estimated_is_nominal",
            "No chunk has been fetched at one or more of the selected resolutions yet, so the "
            "row and byte estimates assume a full trading session for every day of the window. "
            "The real figures will be lower for short lived contracts.",
            {"resolution": resolution.fyers_code},
        )
        days = calendar.count_trading_days(exchange, chunk_from, chunk_to)
        return days * resolution.bars_per_trading_day

    def _price(self, tally: _Tally, *, sweep: bool) -> PlanPreview:
        """Turn the tally into the preview, including the budget arithmetic."""
        tasks_total = len(tally.tasks)
        requests_estimated = tasks_total + tally.estimated_downstream
        rows_estimated = tally.rows_estimated
        bytes_estimated = int(round(rows_estimated * BYTES_PER_ROW))

        per_minute = self._per_minute()
        eta_seconds = int(math.ceil(requests_estimated * 60 / per_minute)) if per_minute else 0

        used, daily_budget = self._budget()
        remaining = max(0, daily_budget - used)
        if sweep:
            fraction = self._reserve_fraction()
            allowance = max(0, int(daily_budget * fraction) - used)
            reserve_applied = True
        else:
            allowance = remaining
            reserve_applied = False

        exceeds = requests_estimated > allowance
        if exceeds:
            tally.warn(
                "exceeds_budget",
                f"This plan needs {requests_estimated} requests and only {allowance} remain "
                + (
                    "inside the sweep reserve today."
                    if reserve_applied
                    else "in today's budget."
                ),
                {"requests_estimated": requests_estimated, "budget_allowance": allowance},
            )

        return PlanPreview(
            tasks_total=tasks_total,
            requests_estimated=requests_estimated,
            chunks_skipped_covered=tally.chunks_skipped_covered,
            contracts_sealed_skipped=tally.contracts_sealed_skipped,
            rows_estimated=rows_estimated,
            bytes_estimated=bytes_estimated,
            eta_seconds=eta_seconds,
            budget_used_today=used,
            budget_remaining_today=remaining,
            budget_after=remaining - requests_estimated,
            warnings=[item.message for item in tally.warnings],
            expiries_planned=tally.expiries_planned,
            contracts_planned=tally.contracts_planned,
            discovery_tasks=tally.discovery_tasks,
            estimated_downstream_requests=tally.estimated_downstream,
            spot_tasks=tally.spot_tasks,
            budget_allowance=allowance,
            reserve_applied=reserve_applied,
            exceeds_budget=exceeds,
            warning_details=list(tally.warnings),
        )

    @staticmethod
    def _job_kind(tally: _Tally, request: PlanRequest) -> str:
        """The `job.kind` a committed plan takes, from what it actually contains."""
        kinds = {task.kind for task in tally.tasks}
        if kinds == {"underlying_symbols"}:
            return "contract_discovery"
        if kinds == {"spot_chunk"}:
            return "underlying_history"
        if any(item.strip().upper() in SECONDS_RESOLUTIONS for item in request.resolutions):
            return "seconds_capture"
        return "candle_backfill"

    @staticmethod
    def _fully_covered(
        merged: Sequence[tuple[date, date]], want_from: date, want_to: date
    ) -> bool:
        return any(start <= want_from and end >= want_to for start, end in merged)

    # -- local state --------------------------------------------------------

    def _now(self) -> datetime:
        moment = self._clock()
        return moment if isinstance(moment, datetime) else datetime.now()

    def _per_minute(self) -> int:
        governor = self._governor
        if governor is not None:
            try:
                return int(governor.snapshot().per_minute)
            except Exception:  # pragma: no cover - a governor without a snapshot
                pass
        if self._settings is not None:
            try:
                return int(self._settings.get_int("throttle_per_minute"))
            except Exception:  # pragma: no cover - key absent in a bare settings store
                pass
        return DEFAULT_PER_MINUTE

    def _budget(self) -> tuple[int, int]:
        governor = self._governor
        if governor is not None:
            try:
                return int(governor.requests_used), int(governor.daily_budget)
            except Exception:  # pragma: no cover - a stub without the properties
                pass
        daily = 100_000
        if self._settings is not None:
            try:
                daily = int(self._settings.get_int("daily_budget"))
            except Exception:  # pragma: no cover - key absent
                pass
        return 0, daily

    def _reserve_fraction(self) -> float:
        if self._settings is not None:
            try:
                return float(self._settings.get_float("budget_reserve_fraction"))
            except Exception:  # pragma: no cover - key absent
                pass
        return 0.70

    def _load_underlying(self, underlying_id: int) -> UnderlyingSpec:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT underlying_id, fyers_symbol, root, exchange, segment,"
                        " instrument_kind, display_name, data_from, default_resolutions,"
                        " include_oi, option_life_days, future_life_days, spot_contract_id,"
                        " is_active FROM underlying_registry WHERE underlying_id = :id"
                    ),
                    {"id": underlying_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            raise PlannerError(
                "underlying_not_found",
                f"No underlying with id {underlying_id} is registered.",
                status_code=404,
            )
        return UnderlyingSpec(
            underlying_id=int(row["underlying_id"]),
            fyers_symbol=str(row["fyers_symbol"]),
            root=str(row["root"]),
            exchange=str(row["exchange"]),
            segment=str(row["segment"]),
            instrument_kind=str(row["instrument_kind"]),
            display_name=str(row["display_name"]),
            data_from=_as_date(row["data_from"]),
            default_resolutions=_as_codes(row["default_resolutions"]),
            include_oi=bool(row["include_oi"]),
            option_life_days=int(row["option_life_days"]),
            future_life_days=int(row["future_life_days"]),
            spot_contract_id=int(row["spot_contract_id"]),
            is_active=bool(row["is_active"]),
        )

    def _load_resolutions(self, codes: Sequence[str]) -> list[ResolutionSpec]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT fyers_code, res_id, seconds, max_days_per_request"
                        "  FROM ref_resolution"
                    )
                )
                .mappings()
                .all()
            )
        known = {str(row["fyers_code"]): row for row in rows}
        out: list[ResolutionSpec] = []
        for code in codes:
            wanted = code.strip().upper()
            row = known.get(wanted)
            if row is None:
                raise PlannerError(
                    "unknown_resolution",
                    f"{wanted} is not a resolution this build serves.",
                    status_code=400,
                    detail={"resolution": wanted, "known": sorted(known)},
                )
            out.append(
                ResolutionSpec(
                    fyers_code=wanted,
                    res_id=int(row["res_id"]),
                    seconds=int(row["seconds"]),
                    is_seconds=wanted in SECONDS_RESOLUTIONS
                    or RESOLUTION_SECONDS.get(wanted, 60) < 60,
                    max_days=int(row["max_days_per_request"]),
                )
            )
        return out

    def _load_calendar(self, exchange: str) -> tuple[TradingCalendar, bool]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT holiday_date FROM market_holiday WHERE exchange = :exchange"
                    ),
                    {"exchange": exchange},
                )
                .scalars()
                .all()
            )
        days = [_as_date(value) for value in rows]
        calendar = TradingCalendar()
        if days:
            calendar.add_holidays(exchange, days)
        return calendar, bool(days)

    async def _load_expiries(
        self, underlying_id: int, expiry_dates: Sequence[date]
    ) -> dict[date, dict[str, Any]]:
        if not expiry_dates:
            return {}
        placeholders = ", ".join("?" for _ in expiry_dates)
        columns, rows = await self._reader.fetch_columns(
            "SELECT expiry_id, expiry_date, contracts_discovered_at, contract_count,"
            " options_count, futures_count"
            "  FROM dim_expiry WHERE underlying_id = ?"
            f"   AND expiry_date IN ({placeholders})",
            [underlying_id, *expiry_dates],
        )
        out: dict[date, dict[str, Any]] = {}
        for row in rows:
            record = dict(zip(columns, row))
            out[_as_date(record["expiry_date"])] = record
        return out

    @staticmethod
    def _average_contract_count(
        expiries: Mapping[date, Mapping[str, Any]], discovered: Sequence[date]
    ) -> int:
        """How many contracts an undiscovered expiry is assumed to carry.

        The median of the sibling expiries, not the mean: one monthly expiry with three times the
        strikes of the weeklies around it should not drag the estimate for every weekly with it.
        """
        counts = sorted(
            int(expiries[day]["contract_count"] or 0)
            for day in discovered
            if expiries[day].get("contract_count")
        )
        if not counts:
            return 0
        return counts[len(counts) // 2]

    async def _load_contracts(
        self, underlying_id: int, expiry_date: date, request: PlanRequest
    ) -> list[dict[str, Any]]:
        where = ["underlying_id = ?", "expiry_date = ?", "kind <> 'SPOT'"]
        params: list[Any] = [underlying_id, expiry_date]
        if request.instrument_class != "BOTH":
            where.append("kind = ?")
            params.append(request.instrument_class)
        if request.instrument_class in ("OPT", "BOTH") and request.option_types:
            rights = ", ".join("?" for _ in request.option_types)
            # A future has a NULL option_type and must survive the right filter in BOTH mode.
            where.append(f"(option_type IS NULL OR option_type IN ({rights}))")
            params.extend(request.option_types)
        columns, rows = await self._reader.fetch_columns(
            "SELECT contract_id, fyers_symbol, kind, CAST(strike AS DOUBLE) AS strike,"
            " option_type, expiry_date, sealed_at, lot_size"
            "  FROM dim_contract WHERE " + " AND ".join(where) + " ORDER BY contract_id",
            params,
        )
        contracts = [dict(zip(columns, row)) for row in rows]
        return await self._apply_strike_scope(
            underlying_id, expiry_date, contracts, request
        )

    async def _apply_strike_scope(
        self,
        underlying_id: int,
        expiry_date: date,
        contracts: list[dict[str, Any]],
        request: PlanRequest,
    ) -> list[dict[str, Any]]:
        scope = request.strike_scope
        if scope.mode == "all":
            return contracts
        if scope.mode == "explicit":
            wanted = {round(float(value), 4) for value in (scope.strikes or ())}
            return [
                item
                for item in contracts
                if item.get("strike") is None or round(float(item["strike"]), 4) in wanted
            ]

        spot = await self._spot_on(underlying_id, expiry_date)
        if spot is None:
            raise PlannerError(
                "strike_scope_needs_spot",
                "An at the money band needs the underlying's own history for the expiry day, "
                "and none is stored yet. Download the underlying history for this date first, "
                "or switch the strike scope to all.",
                status_code=400,
                detail={
                    "underlying_id": underlying_id,
                    "expiry_date": expiry_date.isoformat(),
                    "remedy": "underlying_history",
                },
            )
        strikes = sorted(
            {
                round(float(item["strike"]), 4)
                for item in contracts
                if item.get("strike") is not None
            }
        )
        if not strikes:
            return contracts
        # Nearest to spot, the strike value itself breaking a tie, so a spot exactly between two
        # strikes always picks the same one.
        atm_index = min(
            range(len(strikes)), key=lambda i: (abs(strikes[i] - spot), strikes[i])
        )
        steps = int(scope.steps or 0)
        lo = max(0, atm_index - steps)
        hi = min(len(strikes) - 1, atm_index + steps)
        band = set(strikes[lo : hi + 1])
        return [
            item
            for item in contracts
            if item.get("strike") is None or round(float(item["strike"]), 4) in band
        ]

    async def _spot_on(self, underlying_id: int, expiry_date: date) -> float | None:
        """Last spot close at or before the expiry day's close, at any stored resolution.

        Any resolution, because the band only needs a price good to within a strike step, and
        insisting on one interval would refuse a sheet whose underlying was captured at another.
        """
        moment = datetime.combine(expiry_date, _SESSION_CLOSE)
        row = await self._reader.fetch_one(
            "SELECT c.close FROM candles c"
            "  JOIN dim_underlying u ON u.spot_contract_id = c.contract_id"
            " WHERE u.underlying_id = ? AND c.ts <= ?"
            " ORDER BY c.ts DESC LIMIT 1",
            [underlying_id, moment],
        )
        return None if row is None or row[0] is None else float(row[0])

    async def _held_ranges(
        self, contract_ids: Sequence[int], res_id: int
    ) -> dict[int, list[tuple[date, date, str, int]]]:
        """Coverage for a whole expiry in one read.

        Per contract this would be 482 round trips for one NIFTY weekly, and a plan is supposed
        to answer while the user is looking at it.
        """
        if not contract_ids:
            return {}
        placeholders = ", ".join("?" for _ in contract_ids)
        rows = await self._reader.fetch_all(
            "SELECT contract_id, range_from, range_to, status, row_count"
            "  FROM candle_coverage"
            f" WHERE res_id = ? AND contract_id IN ({placeholders})"
            "   AND status IN ('ok', 'empty') ORDER BY contract_id, range_from",
            [res_id, *contract_ids],
        )
        out: dict[int, list[tuple[date, date, str, int]]] = {}
        for contract_id, range_from, range_to, status, row_count in rows:
            out.setdefault(int(contract_id), []).append(
                (_as_date(range_from), _as_date(range_to), str(status), int(row_count))
            )
        return out

    async def _observed_rows_per_chunk(
        self, underlying_id: int, res_ids: Sequence[int]
    ) -> dict[int, int]:
        """Median rows per successful chunk, per resolution, for this underlying."""
        if not res_ids:
            return {}
        placeholders = ", ".join("?" for _ in res_ids)
        rows = await self._reader.fetch_all(
            "SELECT cov.res_id,"
            "       CAST(median(cov.row_count) AS BIGINT) AS typical"
            "  FROM candle_coverage cov JOIN dim_contract c USING (contract_id)"
            " WHERE c.underlying_id = ? AND cov.status = 'ok' AND cov.row_count > 0"
            f"   AND cov.res_id IN ({placeholders})"
            " GROUP BY cov.res_id",
            [underlying_id, *res_ids],
        )
        return {int(res_id): int(typical or 0) for res_id, typical in rows}


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_codes(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip().upper() for item in value)
    text_value = str(value or "").strip()
    if not text_value:
        return ()
    if text_value.startswith("["):
        import json

        try:
            return tuple(str(item).strip().upper() for item in json.loads(text_value))
        except ValueError:
            return ()
    return tuple(part.strip().upper() for part in text_value.split(",") if part.strip())


def summarise(tasks: Iterable[PlannedTask]) -> dict[str, int]:
    """Task counts by kind. Used by the job service's log line and by the tests."""
    out: dict[str, int] = {}
    for task in tasks:
        out[task.kind] = out.get(task.kind, 0) + 1
    return out
