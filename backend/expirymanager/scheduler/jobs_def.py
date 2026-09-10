"""What each builtin schedule actually does when it fires.

The rule that shapes every action in this file: **a fire never fetches anything itself.** It
plans, or it enqueues, and then it stops. A scheduled download therefore lands in the same job
list with the same progress, the same retry button and the same task detail as a manual one, and
there is exactly one code path that talks to Fyers, the one the governor gates.

Four internal actions do not create a job at all, because there is nothing to fetch:
`token_health` reads the JWT expiry the broker already decoded, `token_logout` is the 03:00 IST
scheduled logout, `budget_reset` rolls the governor onto the new IST quota date, and `maintenance`
runs the DuckDB housekeeping W09 exposes. Those record the outcome `completed` rather than
`enqueued`, which is the whole reason migration 0005 widened that vocabulary.

Two enqueue paths exist, and the split is forced by what the planner can express rather than by
preference:

- The planner prices anything made of candle chunks: `seconds_capture`, `rolling_backfill`,
  `underlying_history` and `gap_repair`. Those go through `JobService.create`, so the sweep
  reserve, the budget gate and the atomic commit are the same ones the UI gets.
- Discovery and snapshot work has no price to compute and no coverage to subtract: one request
  per underlying or per expiry, known before the fire. `expiry_discovery`,
  `contract_discovery`, `chain_snapshot` and `symbol_master` write their job row and their task
  rows directly, in one transaction, with the same column shape W12 writes. The planner refuses
  a sheet whose expiries have no contracts at all, which is precisely the input contract
  discovery has, so routing it through the planner is not merely unnecessary but impossible.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from expirymanager.api.schemas.downloads import DownloadRequest
from expirymanager.brokers.fyers.calendar import (
    SECONDS_RESOLUTIONS,
    TradingCalendar,
)
from expirymanager.pipeline.handlers.expiry_discovery import last_served_day
from expirymanager.pipeline.planner import PipelineRequestError
from expirymanager.pipeline.queue import DEFAULT_MAX_ATTEMPTS, iso_at, utc_now

__all__ = [
    "OUTCOME_ENQUEUED",
    "OUTCOME_COMPLETED",
    "OUTCOME_SKIPPED_DISABLED",
    "OUTCOME_SKIPPED_HOLIDAY",
    "OUTCOME_SKIPPED_NEEDS_AUTH",
    "OUTCOME_SKIPPED_BLOCKED",
    "OUTCOME_SKIPPED_BUDGET",
    "OUTCOME_ERROR",
    "OUTCOMES",
    "BUILTIN_SCHEDULE_IDS",
    "ScheduleSpec",
    "FireContext",
    "FireResult",
    "ActionMeta",
    "register_action",
    "unregister_action",
    "get_action",
    "action_meta",
    "known_kinds",
    "needs_token",
    "spends_budget",
    "EXPIRY_WINDOW_MAX_DAYS",
]

log = logging.getLogger(__name__)

OUTCOME_ENQUEUED = "enqueued"
OUTCOME_COMPLETED = "completed"
OUTCOME_SKIPPED_DISABLED = "skipped_disabled"
OUTCOME_SKIPPED_HOLIDAY = "skipped_holiday"
OUTCOME_SKIPPED_NEEDS_AUTH = "skipped_needs_auth"
OUTCOME_SKIPPED_BLOCKED = "skipped_blocked"
OUTCOME_SKIPPED_BUDGET = "skipped_budget"
OUTCOME_ERROR = "error"

OUTCOMES: tuple[str, ...] = (
    OUTCOME_ENQUEUED,
    OUTCOME_COMPLETED,
    OUTCOME_SKIPPED_DISABLED,
    OUTCOME_SKIPPED_HOLIDAY,
    OUTCOME_SKIPPED_NEEDS_AUTH,
    OUTCOME_SKIPPED_BLOCKED,
    OUTCOME_SKIPPED_BUDGET,
    OUTCOME_ERROR,
)

# The measured ceiling on the expiry dates window, from API-PROBES.md: 366 days, and the boundary
# is a hard error rather than a truncation. Inclusive days, so one day of margin is kept for the
# same reason calendar.MAX_DAYS_PER_REQUEST keeps one.
EXPIRY_WINDOW_MAX_DAYS = 365

BUILTIN_SCHEDULE_IDS: tuple[str, ...] = (
    "builtin_symbol_master",
    "builtin_seconds_capture",
    "builtin_expiry_discovery",
    "builtin_contract_discovery",
    "builtin_rolling_backfill",
    "builtin_underlying_history",
    "builtin_chain_snapshot",
    "builtin_token_health",
    "builtin_gap_repair",
    "builtin_maintenance",
    "builtin_budget_reset",
    "builtin_token_logout",
)


# ---------------------------------------------------------------------------
# The typed row and the fire contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """One `sqlite.schedule` row, typed. The source of truth for what fires and when."""

    schedule_id: str
    name: str
    kind: str
    cron: str
    timezone: str = "Asia/Kolkata"
    params: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    trading_days_only: bool = True
    misfire_grace_seconds: int = 3600
    max_requests_per_run: int | None = None
    is_builtin: bool = False
    last_fired_at: str | None = None
    next_fire_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ScheduleSpec":
        raw = row.get("params_json") or "{}"
        try:
            params = json.loads(raw)
        except (TypeError, ValueError):
            # A row whose params cannot be parsed still has to fire: the trigger is what the user
            # sees on screen, and refusing to install it would make the schedule disappear.
            log.warning(
                "schedule params are not valid json, firing with an empty parameter set",
                extra={"schedule_id": row.get("schedule_id")},
            )
            params = {}
        if not isinstance(params, dict):
            params = {}
        return cls(
            schedule_id=str(row["schedule_id"]),
            name=str(row["name"]),
            kind=str(row["kind"]),
            cron=str(row["cron"]),
            timezone=str(row.get("timezone") or "Asia/Kolkata"),
            params=params,
            enabled=bool(row.get("enabled", 1)),
            trading_days_only=bool(row.get("trading_days_only", 1)),
            misfire_grace_seconds=int(row.get("misfire_grace_seconds") or 0),
            max_requests_per_run=(
                None
                if row.get("max_requests_per_run") is None
                else int(row["max_requests_per_run"])
            ),
            is_builtin=bool(row.get("is_builtin", 0)),
            last_fired_at=row.get("last_fired_at"),
            next_fire_at=row.get("next_fire_at"),
        )

    def priority(self, fallback: int = 100) -> int:
        try:
            return int(self.params.get("priority", fallback))
        except (TypeError, ValueError):
            return fallback

    def underlying_ids(self) -> tuple[int, ...]:
        """The declared subset, or an empty tuple meaning every active underlying.

        Empty means all on purpose. Writing the four builtin ids into the seed would silently
        ignore a fifth underlying the user adds, which is the kind of bug that is only noticed
        months later when the data is gone.
        """
        raw = self.params.get("underlying_ids") or ()
        out: list[int] = []
        for item in raw if isinstance(raw, (list, tuple)) else ():
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return tuple(out)

    def resolutions(self) -> tuple[str, ...]:
        raw = self.params.get("resolutions") or ()
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(str(item).strip().upper() for item in raw if str(item).strip())

    def int_param(self, key: str, fallback: int) -> int:
        try:
            return int(self.params.get(key, fallback))
        except (TypeError, ValueError):
            return fallback


@dataclass(frozen=True, slots=True)
class FireResult:
    """What one action reports back. The service writes it to `schedule_run`."""

    outcome: str
    job_ids: tuple[str, ...] = ()
    note: str | None = None

    @property
    def job_id(self) -> str | None:
        """The first job, which is what `schedule_run.job_id` and the API response carry."""
        return self.job_ids[0] if self.job_ids else None


@dataclass(slots=True)
class FireContext:
    """Everything an action is allowed to reach. Deliberately small.

    An action gets the services the lifespan built and the moment it fired. It does not get an
    HTTP client, because an action that could fetch would eventually fetch.
    """

    schedule: ScheduleSpec
    services: Any
    engine: Engine
    now: datetime
    job_service: Any = None
    triggered_by: str = "cron"
    id_factory: Callable[[], str] = lambda: str(uuid.uuid4())

    @property
    def reader(self) -> Any:
        return getattr(self.services, "duck_reader", None)

    @property
    def writer(self) -> Any:
        return getattr(self.services, "duck_writer", None)

    @property
    def today(self) -> date:
        return self.now.date()


Action = Callable[[FireContext], Awaitable[FireResult]]


@dataclass(frozen=True, slots=True)
class ActionMeta:
    """What the guards need to know about a kind before they run it."""

    kind: str
    action: Action
    needs_token: bool = True
    spends_budget: bool = True
    description: str = ""


_ACTIONS: dict[str, ActionMeta] = {}


def register_action(
    kind: str,
    action: Action,
    *,
    needs_token: bool = True,
    spends_budget: bool = True,
    description: str = "",
    replace: bool = False,
) -> None:
    """Register the action for one schedule kind.

    Refusing a duplicate is deliberate: two modules registering the same kind is a wiring bug, and
    last-write-wins on a dict would hide it until the wrong one fired at 18:30.
    """
    key = str(kind).strip()
    if not key:
        raise ValueError("a schedule kind cannot be blank")
    if key in _ACTIONS and not replace:
        raise ValueError(f"an action is already registered for schedule kind {key!r}")
    _ACTIONS[key] = ActionMeta(
        kind=key,
        action=action,
        needs_token=needs_token,
        spends_budget=spends_budget,
        description=description,
    )


def unregister_action(kind: str) -> None:
    _ACTIONS.pop(str(kind).strip(), None)


def get_action(kind: str) -> Action | None:
    meta = _ACTIONS.get(str(kind).strip())
    return None if meta is None else meta.action


def action_meta(kind: str) -> ActionMeta | None:
    return _ACTIONS.get(str(kind).strip())


def known_kinds() -> tuple[str, ...]:
    return tuple(sorted(_ACTIONS))


def needs_token(kind: str) -> bool:
    meta = _ACTIONS.get(str(kind).strip())
    return True if meta is None else meta.needs_token


def spends_budget(kind: str) -> bool:
    meta = _ACTIONS.get(str(kind).strip())
    return True if meta is None else meta.spends_budget


# ---------------------------------------------------------------------------
# Registry reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryRow:
    underlying_id: int
    fyers_symbol: str
    exchange: str
    display_name: str
    data_from: date
    default_resolutions: tuple[str, ...]
    include_oi: bool


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def load_registry(engine: Engine, ids: Sequence[int] = ()) -> list[RegistryRow]:
    """Active underlyings, optionally narrowed to a declared subset.

    MCX is filtered out here as well as in the planner. The 2026-09-09 probe measured seven MCX
    forms answering 422 while BSE:SENSEX-INDEX answered 200 in the same run, so a nightly sweep
    that included one would spend a request per contract per day to learn the same thing again.
    """
    sql = (
        "SELECT underlying_id, fyers_symbol, exchange, display_name, data_from,"
        " default_resolutions, include_oi"
        " FROM underlying_registry WHERE is_active = 1 AND exchange <> 'MCX'"
    )
    params: dict[str, Any] = {}
    if ids:
        placeholders = ", ".join(f":u{index}" for index in range(len(ids)))
        sql += f" AND underlying_id IN ({placeholders})"
        params = {f"u{index}": int(value) for index, value in enumerate(ids)}
    sql += " ORDER BY underlying_id"
    with engine.connect() as connection:
        rows = connection.execute(text(sql), params).mappings().all()
    out: list[RegistryRow] = []
    for row in rows:
        try:
            resolutions = tuple(
                str(item).strip().upper()
                for item in json.loads(row["default_resolutions"] or "[]")
            )
        except (TypeError, ValueError):
            resolutions = ()
        out.append(
            RegistryRow(
                underlying_id=int(row["underlying_id"]),
                fyers_symbol=str(row["fyers_symbol"]),
                exchange=str(row["exchange"]).upper(),
                display_name=str(row["display_name"]),
                data_from=_as_date(row["data_from"]),
                default_resolutions=resolutions,
                include_oi=bool(row["include_oi"]),
            )
        )
    return out


def load_calendar(engine: Engine, exchange: str) -> tuple[TradingCalendar, bool]:
    """The holiday calendar for one exchange, and whether any rows were actually loaded.

    The second half of the answer is the point. With no rows the calendar degrades to a weekday
    rule, which makes the 30 trading day seconds window start later than reality and under-reports
    what is still capturable. The seconds capture fire says so in its note rather than quietly
    capturing less than it could have.
    """
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text("SELECT holiday_date FROM market_holiday WHERE exchange = :exchange"),
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


async def _expiry_dates(
    reader: Any,
    *,
    underlying_id: int,
    discovered: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int | None = None,
) -> list[date]:
    """Expiry dates from the catalog, newest first. Reads only, costs no request."""
    if reader is None:
        return []
    where = ["underlying_id = ?"]
    params: list[Any] = [underlying_id]
    if discovered is True:
        where.append("contracts_discovered_at IS NOT NULL")
    elif discovered is False:
        where.append("contracts_discovered_at IS NULL")
    if date_from is not None:
        where.append("expiry_date >= ?")
        params.append(date_from)
    if date_to is not None:
        where.append("expiry_date <= ?")
        params.append(date_to)
    sql = (
        "SELECT expiry_date FROM dim_expiry WHERE "
        + " AND ".join(where)
        + " ORDER BY expiry_date DESC"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    _columns, rows = await reader.fetch_columns(sql, params)
    return [_as_date(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# The two enqueue paths
# ---------------------------------------------------------------------------


async def plan_and_create(
    ctx: FireContext,
    request: DownloadRequest,
    *,
    job_kind: str | None = None,
) -> tuple[str | None, str | None]:
    """Price one sheet and commit it. Returns (job_id, refusal_code).

    A refusal is not an exception here. `nothing_to_download` is the normal answer on a night when
    everything is already held, and turning it into a failed schedule run would put a red row on
    the screen every night the system was up to date.
    """
    service = ctx.job_service
    if service is None:
        return None, "no_job_service"
    try:
        accepted = await service.create(
            request,
            created_by=f"schedule:{ctx.schedule.schedule_id}",
            schedule_id=ctx.schedule.schedule_id,
            sweep=True,
            job_kind=job_kind,
        )
    except PipelineRequestError as exc:
        return None, exc.code
    return accepted.job_id, None


# The columns a directly written task row carries. Kept beside the INSERT so the two cannot drift.
_TASK_COLUMNS = (
    "job_id",
    "seq",
    "kind",
    "state",
    "priority",
    "underlying_id",
    "contract_id",
    "fyers_symbol",
    "expiry_date",
    "resolution",
    "range_from",
    "range_to",
    "include_oi",
    "request_params_json",
    "attempt",
    "max_attempts",
    "not_before",
    "created_at",
)


def enqueue_direct(
    ctx: FireContext,
    *,
    job_kind: str,
    tasks: Sequence[Mapping[str, Any]],
    params: Mapping[str, Any] | None = None,
) -> str | None:
    """Write one job row and its task rows in a single transaction. Returns the job id.

    This is the discovery and snapshot path. It is not a second planner: there is nothing to
    price, no coverage to subtract and no chunk grid, only a known list of one-request tasks. The
    column shape, the `queue.iso_at` timestamps and `DEFAULT_MAX_ATTEMPTS` are deliberately the
    same ones W12 writes, so the lease statement cannot tell the two paths apart.
    """
    if not tasks:
        return None
    job_id = ctx.id_factory()
    now = iso_at(utc_now())
    priority = ctx.schedule.priority()
    job_params = dict(params or {})
    job_params.setdefault("schedule_kind", ctx.schedule.kind)
    rows = []
    for seq, task in enumerate(tasks):
        row = {column: None for column in _TASK_COLUMNS}
        row.update(
            {
                "job_id": job_id,
                "seq": seq,
                "state": "pending",
                "priority": priority,
                "include_oi": 1,
                "attempt": 0,
                "max_attempts": DEFAULT_MAX_ATTEMPTS,
                "not_before": now,
                "created_at": now,
            }
        )
        for key, value in task.items():
            if key not in _TASK_COLUMNS:
                raise ValueError(f"{key!r} is not a task column")
            row[key] = value
        if isinstance(row["request_params_json"], (dict, list)):
            row["request_params_json"] = json.dumps(row["request_params_json"])
        if isinstance(row["expiry_date"], date):
            row["expiry_date"] = row["expiry_date"].isoformat()
        rows.append(row)

    placeholders = ", ".join(f":{column}" for column in _TASK_COLUMNS)
    columns = ", ".join(_TASK_COLUMNS)
    with ctx.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (job_id, kind, status, params_json, schedule_id, priority,"
                " est_requests, total_tasks, created_by, created_at)"
                " VALUES (:job_id, :kind, 'queued', :params_json, :schedule_id, :priority,"
                " :est_requests, :total_tasks, :created_by, :created_at)"
            ),
            {
                "job_id": job_id,
                "kind": job_kind,
                "params_json": json.dumps(job_params, sort_keys=True),
                "schedule_id": ctx.schedule.schedule_id,
                "priority": priority,
                "est_requests": len(rows),
                "total_tasks": len(rows),
                "created_by": f"schedule:{ctx.schedule.schedule_id}",
                "created_at": now,
            },
        )
        connection.execute(
            text(f"INSERT INTO task ({columns}) VALUES ({placeholders})"), rows
        )
    supervisor = getattr(ctx.services, "supervisor", None)
    if supervisor is not None:
        supervisor.notify(job_id)
    return job_id


# ---------------------------------------------------------------------------
# The candle actions, all priced by the planner
# ---------------------------------------------------------------------------


def _download_request(
    *,
    underlying_id: int,
    expiry_dates: Sequence[date],
    resolutions: Sequence[str],
    priority: int,
    include_oi: bool = True,
    include_spot: bool = False,
    range_from: date | None = None,
    range_to: date | None = None,
) -> DownloadRequest:
    return DownloadRequest(
        underlying_id=underlying_id,
        expiry_dates=list(expiry_dates),
        resolutions=list(resolutions),
        instrument_class="BOTH",
        include_oi=include_oi,
        include_spot=include_spot,
        range_from=range_from,
        range_to=range_to,
        priority=priority,
        # No confirm_requests: a schedule fire never saw a preview, so there is no number for the
        # 409 plan_changed gate to compare against and gating it would refuse every sweep.
        confirm_requests=None,
    )


def _summarise(created: Sequence[str], refusals: Mapping[str, int]) -> str:
    parts = [f"{len(created)} jobs created"] if created else ["nothing to enqueue"]
    for code, count in sorted(refusals.items()):
        parts.append(f"{code} x{count}")
    return ", ".join(parts)


def _outcome_for(created: Sequence[str], refusals: Mapping[str, int]) -> str:
    if created:
        return OUTCOME_ENQUEUED
    if "exceeds_budget" in refusals:
        return OUTCOME_SKIPPED_BUDGET
    if "pipeline_stopped" in refusals:
        return OUTCOME_SKIPPED_BLOCKED
    return OUTCOME_COMPLETED


async def run_seconds_capture(ctx: FireContext) -> FireResult:
    """5S for contracts that expired inside the last 30 trading days.

    The highest priority job in the system. This window is the only one in which second
    resolution data exists at all, and once it closes the data is gone permanently, which is why
    a degraded holiday calendar is reported in the note rather than silently narrowing the window.
    """
    schedule = ctx.schedule
    resolutions = schedule.resolutions() or ("5S",)
    created: list[str] = []
    refusals: dict[str, int] = {}
    degraded: list[str] = []
    for row in load_registry(ctx.engine, schedule.underlying_ids()):
        calendar, holidays_loaded = load_calendar(ctx.engine, row.exchange)
        if not holidays_loaded and row.exchange not in degraded:
            degraded.append(row.exchange)
        window_from, window_to = calendar.seconds_window(row.exchange, ctx.today)
        expiries = await _expiry_dates(
            ctx.reader,
            underlying_id=row.underlying_id,
            discovered=True,
            date_from=window_from,
            date_to=min(window_to, ctx.today),
        )
        if not expiries:
            continue
        job_id, refusal = await plan_and_create(
            ctx,
            _download_request(
                underlying_id=row.underlying_id,
                expiry_dates=expiries,
                resolutions=resolutions,
                priority=schedule.priority(5),
                include_oi=row.include_oi,
            ),
        )
        if job_id:
            created.append(job_id)
        elif refusal:
            refusals[refusal] = refusals.get(refusal, 0) + 1
    note = _summarise(created, refusals)
    if degraded:
        note += (
            f"; no holiday rows for {', '.join(degraded)}, so the 30 trading day window was "
            "computed from weekdays alone and starts later than reality"
        )
    return FireResult(_outcome_for(created, refusals), tuple(created), note)


async def run_rolling_backfill(ctx: FireContext) -> FireResult:
    """The budget aware workhorse: the newest unsealed expiries at each underlying's defaults."""
    schedule = ctx.schedule
    max_expiries = schedule.int_param("max_expiries", 40)
    created: list[str] = []
    refusals: dict[str, int] = {}
    for row in load_registry(ctx.engine, schedule.underlying_ids()):
        resolutions = schedule.resolutions() or row.default_resolutions
        # Seconds are excluded here on purpose. They have their own 16:15 schedule against a
        # window this one does not respect, and a 200 day life window at 5S would be seven
        # chunks of guaranteed empty response per contract.
        resolutions = tuple(item for item in resolutions if item not in SECONDS_RESOLUTIONS)
        if not resolutions:
            continue
        expiries = await _expiry_dates(
            ctx.reader,
            underlying_id=row.underlying_id,
            discovered=True,
            date_to=ctx.today,
            limit=max_expiries,
        )
        if not expiries:
            continue
        job_id, refusal = await plan_and_create(
            ctx,
            _download_request(
                underlying_id=row.underlying_id,
                expiry_dates=expiries,
                resolutions=resolutions,
                priority=schedule.priority(40),
                include_oi=row.include_oi,
            ),
            job_kind="candle_backfill",
        )
        if job_id:
            created.append(job_id)
        elif refusal:
            refusals[refusal] = refusals.get(refusal, 0) + 1
    return FireResult(_outcome_for(created, refusals), tuple(created), _summarise(created, refusals))


async def run_underlying_history(ctx: FireContext) -> FireResult:
    """Incremental spot bars for every active underlying.

    The resume point is not read from `contract_bounds` here. The planner already subtracts
    recorded coverage from the requested range, so asking for the whole history and letting it
    remove what is held produces the same chunk list with one fewer thing to keep in sync.
    """
    schedule = ctx.schedule
    created: list[str] = []
    refusals: dict[str, int] = {}
    for row in load_registry(ctx.engine, schedule.underlying_ids()):
        resolutions = schedule.resolutions() or row.default_resolutions
        resolutions = tuple(item for item in resolutions if item not in SECONDS_RESOLUTIONS)
        if not resolutions:
            continue
        request = _download_request(
            underlying_id=row.underlying_id,
            # A spot sheet has no expiry, but `PlanRequest` requires at least one date and uses
            # it only to bound the window when no explicit range is given. Both bounds are
            # explicit here, so today is a placeholder and nothing is planned against it.
            expiry_dates=[ctx.today],
            resolutions=resolutions,
            priority=schedule.priority(20),
            include_oi=False,
            include_spot=True,
            range_from=row.data_from,
            range_to=ctx.today,
        )
        job_id, refusal = await plan_and_create(ctx, request, job_kind="underlying_history")
        if job_id:
            created.append(job_id)
        elif refusal:
            refusals[refusal] = refusals.get(refusal, 0) + 1
    return FireResult(_outcome_for(created, refusals), tuple(created), _summarise(created, refusals))


async def _expiries_with_holes(reader: Any, underlying_id: int, limit: int) -> list[date]:
    """Expiries carrying a recorded error chunk or a hole between two recorded chunks.

    A FATAL task is deliberately not represented here: those rows are not coverage errors, they
    are requests a human has to read the code on, and re-requesting one every Sunday would spend
    budget on the same refusal every week.
    """
    if reader is None:
        return []
    _columns, rows = await reader.fetch_columns(
        "SELECT DISTINCT c.expiry_date FROM candle_coverage cov"
        "  JOIN dim_contract c USING (contract_id)"
        " WHERE c.underlying_id = ? AND c.expiry_date IS NOT NULL AND cov.status = 'error'"
        " UNION"
        " SELECT DISTINCT c.expiry_date FROM v_coverage_gaps g"
        "  JOIN dim_contract c ON c.contract_id = g.contract_id"
        " WHERE c.underlying_id = ? AND c.expiry_date IS NOT NULL"
        " ORDER BY 1 DESC LIMIT ?",
        [underlying_id, underlying_id, int(limit)],
    )
    return [_as_date(row[0]) for row in rows]


async def run_gap_repair(ctx: FireContext) -> FireResult:
    """Re-request coverage rows recorded as errors, and the holes between recorded chunks."""
    schedule = ctx.schedule
    limit = schedule.int_param("max_expiries", 40)
    created: list[str] = []
    refusals: dict[str, int] = {}
    for row in load_registry(ctx.engine, schedule.underlying_ids()):
        resolutions = schedule.resolutions() or row.default_resolutions
        resolutions = tuple(item for item in resolutions if item not in SECONDS_RESOLUTIONS)
        if not resolutions:
            continue
        expiries = await _expiries_with_holes(ctx.reader, row.underlying_id, limit)
        if not expiries:
            continue
        job_id, refusal = await plan_and_create(
            ctx,
            _download_request(
                underlying_id=row.underlying_id,
                expiry_dates=expiries,
                resolutions=resolutions,
                priority=schedule.priority(60),
                include_oi=row.include_oi,
            ),
            job_kind="gap_repair",
        )
        if job_id:
            created.append(job_id)
        elif refusal:
            refusals[refusal] = refusals.get(refusal, 0) + 1
    return FireResult(_outcome_for(created, refusals), tuple(created), _summarise(created, refusals))


# ---------------------------------------------------------------------------
# The discovery and snapshot actions, enqueued directly
# ---------------------------------------------------------------------------


async def run_expiry_discovery(ctx: FireContext) -> FireResult:
    """One expiry-dates request per active underlying over a trailing plus forward window.

    The window is clamped to the measured 366 day ceiling, which the probe found to be a hard
    error rather than a truncation. Asking for 426 days would answer 422 and cost a request per
    underlying per night to learn nothing.
    """
    schedule = ctx.schedule
    forward_days = max(0, schedule.int_param("forward_days", 60))
    lookback_days = max(1, schedule.int_param("lookback_days", 366))
    # Measured against the live endpoint on 2026-09-10: a range_to of today, and any range_to in
    # the future, answers HTTP 422 code -50, while the same window ending yesterday answers 200.
    # The endpoint serves expired contracts only, so a forward margin was never going to return
    # anything; it was going to cost one refused request per underlying per night. forward_days is
    # kept so an existing schedule row still loads, but it cannot reach past the last served day.
    to_date = min(ctx.today + timedelta(days=forward_days), last_served_day(ctx.today))
    from_date = max(
        ctx.today - timedelta(days=lookback_days),
        to_date - timedelta(days=EXPIRY_WINDOW_MAX_DAYS),
    )
    tasks = []
    for row in load_registry(ctx.engine, schedule.underlying_ids()):
        tasks.append(
            {
                "kind": "expiry_dates",
                "underlying_id": row.underlying_id,
                "fyers_symbol": row.fyers_symbol,
                "range_from": from_date.isoformat(),
                "range_to": to_date.isoformat(),
                "include_oi": 0,
                "request_params_json": {
                    "symbol": row.fyers_symbol,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                },
            }
        )
    job_id = enqueue_direct(
        ctx,
        job_kind="expiry_discovery",
        tasks=tasks,
        params={"from_date": from_date.isoformat(), "to_date": to_date.isoformat()},
    )
    if job_id is None:
        return FireResult(OUTCOME_COMPLETED, (), "no active underlyings to discover")
    return FireResult(OUTCOME_ENQUEUED, (job_id,), f"{len(tasks)} underlyings")


async def run_contract_discovery(ctx: FireContext) -> FireResult:
    """One underlying-symbols request per undiscovered expiry whose date has passed.

    This path does not go through the planner, and it cannot: the planner refuses a sheet on
    which no selected expiry has contracts, which is exactly and only what this fire selects.
    """
    schedule = ctx.schedule
    limit = schedule.int_param("max_expiries", 60)
    tasks = []
    for row in load_registry(ctx.engine, schedule.underlying_ids()):
        expiries = await _expiry_dates(
            ctx.reader,
            underlying_id=row.underlying_id,
            discovered=False,
            date_to=ctx.today,
            limit=limit,
        )
        for expiry_date in expiries:
            tasks.append(
                {
                    "kind": "underlying_symbols",
                    "underlying_id": row.underlying_id,
                    "fyers_symbol": row.fyers_symbol,
                    "expiry_date": expiry_date,
                    "include_oi": 1 if row.include_oi else 0,
                    "request_params_json": {
                        "symbol": row.fyers_symbol,
                        "expiry_date": expiry_date.isoformat(),
                    },
                }
            )
    job_id = enqueue_direct(ctx, job_kind="contract_discovery", tasks=tasks)
    if job_id is None:
        return FireResult(OUTCOME_COMPLETED, (), "every known expiry already has its contracts")
    return FireResult(OUTCOME_ENQUEUED, (job_id,), f"{len(tasks)} expiries")


async def run_chain_snapshot(ctx: FireContext) -> FireResult:
    """One options-chain call per active underlying, for the authoritative expiry flag."""
    schedule = ctx.schedule
    tasks = [
        {
            "kind": "chain_snapshot",
            "underlying_id": row.underlying_id,
            "fyers_symbol": row.fyers_symbol,
            "include_oi": 1,
            "request_params_json": {"symbol": row.fyers_symbol, "strikecount": 20},
        }
        for row in load_registry(ctx.engine, schedule.underlying_ids())
    ]
    job_id = enqueue_direct(ctx, job_kind="chain_snapshot", tasks=tasks)
    if job_id is None:
        return FireResult(OUTCOME_COMPLETED, (), "no active underlyings")
    return FireResult(OUTCOME_ENQUEUED, (job_id,), f"{len(tasks)} underlyings")


async def run_symbol_master(ctx: FireContext) -> FireResult:
    """Enqueue the seven public JSON masters as one task.

    It is enqueued rather than run inline for one reason worth stating: the download is minutes of
    streaming and megabytes of diffing, and doing it inside the scheduler's coroutine would block
    the trigger loop. It needs no token and no budget, which is why its guards are both off, and
    that is what keeps the one unrecoverable job running while the token is dead.
    """
    job_id = enqueue_direct(
        ctx,
        job_kind="symbol_master",
        tasks=[
            {
                "kind": "symbol_master",
                "include_oi": 0,
                "request_params_json": {"as_of": ctx.today.isoformat()},
            }
        ],
        params={"as_of": ctx.today.isoformat()},
    )
    if job_id is None:  # pragma: no cover - the task list is a literal
        return FireResult(OUTCOME_ERROR, (), "the symbol master task could not be enqueued")
    return FireResult(OUTCOME_ENQUEUED, (job_id,), "seven public masters")


# ---------------------------------------------------------------------------
# The internal actions, which create no job
# ---------------------------------------------------------------------------


async def run_token_health(ctx: FireContext) -> FireResult:
    """Warn at 24 hours, park at 120 seconds, and re-emit the banner frame.

    Everything here is local: the JWT `exp` claim was decoded at store time. Asking the broker
    would spend a request to learn something the token already says.
    """
    schedule = ctx.schedule
    warn_seconds = schedule.int_param("warn_seconds", 86400)
    park_seconds = schedule.int_param("park_seconds", 120)
    broker = getattr(ctx.services, "token_broker", None)
    if broker is None:
        return FireResult(OUTCOME_COMPLETED, (), "no token broker")
    if broker.record() is None:
        return FireResult(OUTCOME_COMPLETED, (), "no token stored")
    remaining = broker.seconds_to_expiry()
    if remaining is None:
        return FireResult(OUTCOME_COMPLETED, (), "the token carries no expiry claim")
    if remaining <= park_seconds:
        supervisor = getattr(ctx.services, "supervisor", None)
        reason = (
            "the access token expires in under "
            f"{park_seconds} seconds, parking before a request is wasted"
        )
        if supervisor is not None:
            await supervisor.on_auth_failure(broker.generation, reason=reason)
        else:
            await broker.clear(reason=reason)
        _notify(ctx, "warning", "needs_reauth", "Fyers login required", reason)
        return FireResult(OUTCOME_COMPLETED, (), reason)
    if remaining <= warn_seconds:
        hours = int(remaining // 3600)
        body = f"The Fyers access token expires in about {hours} hours. Log in again to avoid a pause."
        _notify(ctx, "warning", "token_expiring", "Fyers token expiring", body)
        return FireResult(OUTCOME_COMPLETED, (), body)
    return FireResult(OUTCOME_COMPLETED, (), f"{int(remaining)} seconds remaining")


async def run_token_logout(ctx: FireContext) -> FireResult:
    """The 03:00 IST scheduled logout.

    Order matters and it is the whole design. The jobs are parked FIRST, through the same
    `on_auth_failure` path a rejected request would take, and only then is the token destroyed.
    Doing it the other way round would mean `TokenBroker.clear` had already recorded a park on the
    current generation, so the broker's generation guard would answer False and the supervisor
    would return without moving a single job. Running jobs would then sit as `running` with a dead
    token instead of as `blocked_auth`, and nothing would un-park them at the next login.

    Nothing is failed. Tasks stay `pending` with their leases released, so the backfill that was
    interrupted at 03:00 resumes at the exact task the moment the user logs in.
    """
    broker = getattr(ctx.services, "token_broker", None)
    if broker is None:
        return FireResult(OUTCOME_COMPLETED, (), "no token broker")
    if broker.record() is None:
        return FireResult(OUTCOME_COMPLETED, (), "no token to clear")
    reason = "scheduled daily logout at 03:00 ist"
    supervisor = getattr(ctx.services, "supervisor", None)
    parked = False
    if supervisor is not None:
        parked = bool(await supervisor.on_auth_failure(broker.generation, reason=reason))
    await broker.scheduled_logout()
    _notify(
        ctx,
        "info",
        "needs_reauth",
        "Log in to Fyers again",
        "The daily 03:00 logout cleared the access token. Any running download is parked and "
        "resumes at the exact task after you log in.",
    )
    note = "token cleared" + (", running jobs parked" if parked else "")
    return FireResult(OUTCOME_COMPLETED, (), note)


async def run_budget_reset(ctx: FireContext) -> FireResult:
    """Roll the governor onto the new IST quota date and lift a budget stop.

    It has to be called from outside because a pipeline stopped on budget has no caller left
    inside `acquire` to notice that the date rolled.
    """
    governor = getattr(ctx.services, "governor", None)
    if governor is None:
        return FireResult(OUTCOME_COMPLETED, (), "no governor")
    mode = await governor.refresh_day()
    released = 0
    try:
        from expirymanager.pipeline.jobs import roll_deferred_budget_jobs

        released = roll_deferred_budget_jobs(ctx.engine, ist_today=ctx.today)
    except Exception:  # noqa: BLE001 - the day must still roll if the release query fails
        log.exception("the deferred budget roll failed during the budget reset")
    supervisor = getattr(ctx.services, "supervisor", None)
    if released and supervisor is not None:
        supervisor.notify()
    return FireResult(
        OUTCOME_COMPLETED, (), f"mode {mode}, {released} deferred jobs released"
    )


async def run_maintenance(ctx: FireContext) -> FireResult:
    """CHECKPOINT, the health assertions, coverage reconciliation and the retention prunes.

    Compaction is deliberately absent. It closes and swaps the live file, so it is a manual
    Optimise action in Settings guarded by a free disk check, never something a timer does at 02:00
    while a backfill holds the writer.
    """
    schedule = ctx.schedule
    notes: list[str] = []
    duck = getattr(ctx.services, "duck", None)
    reader = ctx.reader
    if duck is not None:
        from expirymanager.db import maintenance as maintenance_module

        result = await maintenance_module.checkpoint(duck)
        notes.append(f"wal {result.wal_bytes_before} to {result.wal_bytes_after} bytes")
        if reader is not None:
            failures = await maintenance_module.health_checks(reader)
            offending = [item for item in failures if item.get("offending")]
            notes.append(f"{len(offending)} health checks with rows")
            duplicates = await maintenance_module.duplicate_rows(reader)
            notes.append(f"{len(duplicates)} duplicate groups")
            mismatched = await maintenance_module.reconcile_coverage(reader)
            notes.append(f"{len(mismatched)} coverage mismatches")
            if offending or duplicates or mismatched:
                _notify(
                    ctx,
                    "warning",
                    "maintenance_findings",
                    "Maintenance found something to look at",
                    "; ".join(notes),
                )
    pruned = _prune_rate_events(
        ctx.engine, ctx.today, schedule.int_param("rate_event_retention_days", 30)
    )
    notes.append(f"{pruned} rate events pruned")
    return FireResult(OUTCOME_COMPLETED, (), "; ".join(notes) or "nothing to do")


def _prune_rate_events(engine: Engine, today: date, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = (today - timedelta(days=retention_days)).isoformat()
    with engine.begin() as connection:
        return int(
            connection.execute(
                text("DELETE FROM rate_event WHERE substr(at, 1, 10) < :cutoff"),
                {"cutoff": cutoff},
            ).rowcount
        )


def _notify(ctx: FireContext, level: str, code: str, title: str, body: str) -> None:
    """Write one notification row and publish the matching frame.

    Best effort on purpose: a schedule that did its work must not be recorded as failed because
    the banner could not be written.
    """
    try:
        with ctx.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO notification (notification_id, level, code, title, body,"
                    " created_at) VALUES (:id, :level, :code, :title, :body, :created_at)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "level": level,
                    "code": code,
                    "title": title,
                    "body": body,
                    "created_at": iso_at(utc_now()),
                },
            )
    except Exception:  # noqa: BLE001
        log.warning("could not write the notification row", extra={"notification_code": code})
    supervisor = getattr(ctx.services, "supervisor", None)
    bus = getattr(supervisor, "bus", None)
    if bus is None:
        return
    from expirymanager.pipeline.events import EVENT_NOTIFICATION

    try:
        bus.publish(EVENT_NOTIFICATION, {"level": level, "code": code, "title": title, "body": body})
    except Exception:  # noqa: BLE001
        log.warning("could not publish the notification frame")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_builtin_actions(replace: bool = True) -> None:
    """Register every builtin action. Called at import, and idempotent.

    Unlike the pipeline's `install()`, registering here is safe as an import side effect: this
    dictionary is read only when a schedule fires, so filling it changes nothing about what an
    application built by a test starts.
    """
    register_action(
        "symbol_master",
        run_symbol_master,
        needs_token=False,
        spends_budget=False,
        description="Pull the seven public JSON masters and SCD-2 diff them.",
        replace=replace,
    )
    register_action(
        "seconds_capture",
        run_seconds_capture,
        description="5S for contracts that expired inside the last 30 trading days.",
        replace=replace,
    )
    register_action(
        "expiry_discovery",
        run_expiry_discovery,
        description="Refresh expiry dates over a trailing 366 day window.",
        replace=replace,
    )
    register_action(
        "contract_discovery",
        run_contract_discovery,
        description="Discover contracts for expiries whose date has passed.",
        replace=replace,
    )
    register_action(
        "rolling_backfill",
        run_rolling_backfill,
        description="The budget aware candle workhorse.",
        replace=replace,
    )
    register_action(
        "underlying_history",
        run_underlying_history,
        description="Incremental spot bars for every active underlying.",
        replace=replace,
    )
    register_action(
        "chain_snapshot",
        run_chain_snapshot,
        description="One options chain call per active underlying.",
        replace=replace,
    )
    register_action(
        "token_health",
        run_token_health,
        needs_token=False,
        spends_budget=False,
        description="Warn at 24 hours, park at 120 seconds.",
        replace=replace,
    )
    register_action(
        "gap_repair",
        run_gap_repair,
        description="Re-request error chunks and coverage holes.",
        replace=replace,
    )
    register_action(
        "maintenance",
        run_maintenance,
        needs_token=False,
        spends_budget=False,
        description="Checkpoint, health assertions, reconciliation and retention.",
        replace=replace,
    )
    register_action(
        "budget_reset",
        run_budget_reset,
        needs_token=False,
        spends_budget=False,
        description="Roll onto the new IST quota date.",
        replace=replace,
    )
    register_action(
        "token_logout",
        run_token_logout,
        needs_token=False,
        spends_budget=False,
        description="The 03:00 IST scheduled logout. Parks running jobs, never fails them.",
        replace=replace,
    )


register_builtin_actions()
