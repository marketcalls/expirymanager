"""The process wide outbound limiter.

There is exactly one `FyersGovernor` in the process, constructed in the lifespan and injected
everywhere. Any code path that constructs a second one, or that calls httpx without going through
it, is a day losing bug rather than a performance bug, because the documented penalty for
exceeding the per minute limit more than three times in one day is that the account is blocked for
the rest of that day.

Published Standard limits are 10 per second, 200 per minute and 100,000 per day. The targets here
are 8 and 170. The 15 percent margin is not timidity: it buys cover for clock skew, in flight
retries and coarse timer resolution, and it costs a few percent of throughput against the loss of
a whole trading day of quota.

Why a sliding window and not a classic token bucket. A bucket of capacity 8 refilling at 8 per
second permits 16 grants inside one second at the refill boundary, which is precisely the
overshoot the three strikes rule punishes. A sliding window enforces the guarantee the limit is
actually written as: never more than N grants in any window of that length. The bucket attribute
names are kept, because that is how PIPELINE.md section 2.2 names them.

The daily counter and the violation counter live in SQLite, keyed by the IST date. A restart, a
crash loop or a laptop lid cannot reset either one, which is the whole point of persisting them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

__all__ = [
    "IST",
    "GovernorMode",
    "MAX_MINUTE_VIOLATIONS",
    "BUDGET_FLUSH_INTERVAL",
    "PLAN_LIMITS",
    "BudgetRow",
    "BudgetStore",
    "InMemoryBudgetStore",
    "SqliteBudgetStore",
    "SlidingWindowLimiter",
    "GovernorSnapshot",
    "GovernorError",
    "AccountBlocked",
    "FyersGovernor",
]

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# The documented rule: blocked for the rest of the day after the per minute limit is exceeded more
# than three times. Three strikes are survivable; the fourth is not.
MAX_MINUTE_VIOLATIONS = 3

# The daily counter is flushed every N grants and on every state transition. N is a trade off
# between write amplification and how many requests a hard kill can lose from the count. Twenty
# five is under a second of traffic at full rate.
BUDGET_FLUSH_INTERVAL = 25

# Published ceilings, not targets. Stored on the budget row so a historical row still says what
# the plan allowed on that day.
PLAN_LIMITS: dict[str, tuple[int, int]] = {
    # plan: (per day, per minute)
    "standard": (100_000, 200),
    "prime": (200_000, 600),
}


class GovernorMode(StrEnum):
    """`pipeline_state.mode`. The governor owns this value at runtime."""

    RUNNING = "running"
    PAUSED_AUTH = "paused_auth"
    PAUSED_RATE = "paused_rate"
    PAUSED_USER = "paused_user"
    STOPPED_BUDGET = "stopped_budget"
    STOPPED_FATAL = "stopped_fatal"


class GovernorError(Exception):
    """Base class for governor refusals."""


class AccountBlocked(GovernorError):
    """The three strikes ceiling was passed. Nothing goes out until the IST date rolls."""


@dataclass(frozen=True, slots=True)
class BudgetRow:
    """One `api_budget` row: the durable half of the counters."""

    ist_date: str
    plan: str
    plan_limit_day: int
    plan_limit_minute: int
    requests_used: int = 0
    minute_violations: int = 0
    last_429_at: str | None = None
    blocked_until: str | None = None


class BudgetStore(Protocol):
    """Durable storage for the daily counters. Synchronous, because SQLite is."""

    def load(self, ist_date: str) -> BudgetRow | None: ...

    def save(self, row: BudgetRow) -> None: ...


class InMemoryBudgetStore:
    """A store that forgets on restart. Only for tests and for the dry run planner."""

    def __init__(self) -> None:
        self.rows: dict[str, BudgetRow] = {}

    def load(self, ist_date: str) -> BudgetRow | None:
        return self.rows.get(ist_date)

    def save(self, row: BudgetRow) -> None:
        self.rows[row.ist_date] = row


_UPSERT_BUDGET = """
INSERT INTO api_budget (ist_date, plan, plan_limit_day, plan_limit_minute, requests_used,
                        minute_violations, last_429_at, blocked_until, updated_at)
VALUES (:ist_date, :plan, :plan_limit_day, :plan_limit_minute, :requests_used,
        :minute_violations, :last_429_at, :blocked_until, :updated_at)
ON CONFLICT(ist_date) DO UPDATE SET
    plan = excluded.plan,
    plan_limit_day = excluded.plan_limit_day,
    plan_limit_minute = excluded.plan_limit_minute,
    requests_used = excluded.requests_used,
    minute_violations = excluded.minute_violations,
    last_429_at = excluded.last_429_at,
    blocked_until = excluded.blocked_until,
    updated_at = excluded.updated_at
"""

_SELECT_BUDGET = """
SELECT ist_date, plan, plan_limit_day, plan_limit_minute, requests_used, minute_violations,
       last_429_at, blocked_until
  FROM api_budget
 WHERE ist_date = :ist_date
"""

_INSERT_RATE_EVENT = """
INSERT INTO rate_event (at, kind, endpoint, detail) VALUES (:at, :kind, :endpoint, :detail)
"""


class SqliteBudgetStore:
    """The real store, straight SQL against `api_budget` through a SQLAlchemy engine.

    Plain SQL rather than the ORM, so the governor can run during bootstrap before the declarative
    layer is imported, and so one upsert is one statement.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def load(self, ist_date: str) -> BudgetRow | None:
        from sqlalchemy import text as sa_text

        with self._engine.connect() as connection:
            row = connection.execute(sa_text(_SELECT_BUDGET), {"ist_date": ist_date}).fetchone()
        if row is None:
            return None
        return BudgetRow(
            ist_date=row[0],
            plan=row[1],
            plan_limit_day=int(row[2]),
            plan_limit_minute=int(row[3]),
            requests_used=int(row[4]),
            minute_violations=int(row[5]),
            last_429_at=row[6],
            blocked_until=row[7],
        )

    def save(self, row: BudgetRow) -> None:
        from sqlalchemy import text as sa_text

        params = {
            "ist_date": row.ist_date,
            "plan": row.plan,
            "plan_limit_day": row.plan_limit_day,
            "plan_limit_minute": row.plan_limit_minute,
            "requests_used": row.requests_used,
            "minute_violations": row.minute_violations,
            "last_429_at": row.last_429_at,
            "blocked_until": row.blocked_until,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        with self._engine.begin() as connection:
            connection.execute(sa_text(_UPSERT_BUDGET), params)

    def record_event(self, *, kind: str, endpoint: str | None, detail: str | None) -> None:
        """Append one `rate_event` row. Best effort provenance, never load bearing."""
        from sqlalchemy import text as sa_text

        params = {
            "at": datetime.now(UTC).isoformat(),
            "kind": kind,
            "endpoint": endpoint,
            "detail": detail,
        }
        with self._engine.begin() as connection:
            connection.execute(sa_text(_INSERT_RATE_EVENT), params)


class SlidingWindowLimiter:
    """At most `limit` grants in any `window` seconds.

    Holds one monotonic timestamp per grant inside the window, so memory is bounded by the limit
    itself: 170 floats for the minute window. Waiters are served in arrival order, because an
    unfair limiter starves whichever worker is unlucky, and a starved worker still holds a lease.
    """

    def __init__(self, limit: int, window: float, *, clock: Any = time.monotonic) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._window = window
        self._clock = clock
        self._grants: deque[float] = deque()
        self._lock = asyncio.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._grants and self._grants[0] <= cutoff:
            self._grants.popleft()

    def _wait_for(self, now: float) -> float:
        """Seconds until a slot frees, or zero when one is free right now."""
        self._prune(now)
        if len(self._grants) < self._limit:
            return 0.0
        return self._grants[0] + self._window - now

    async def take(self) -> float:
        """Block until a slot is free, then consume it. Returns the seconds spent waiting."""
        waited = 0.0
        # The lock is what makes the ordering fair and the check-then-append atomic. It is held
        # across the sleep on purpose: releasing it would let a later caller take the slot this
        # one is waiting for, which is how a limiter starves its oldest waiter.
        async with self._lock:
            while True:
                now = self._clock()
                delay = self._wait_for(now)
                if delay <= 0:
                    self._grants.append(now)
                    return waited
                waited += delay
                await asyncio.sleep(delay)


@dataclass(frozen=True, slots=True)
class GovernorSnapshot:
    """What the budget screen and the SSE frame report."""

    mode: GovernorMode
    reason: str | None
    ist_date: str
    plan: str
    requests_used: int
    daily_budget: int
    plan_limit_day: int
    plan_limit_minute: int
    requests_remaining: int
    minute_violations: int
    strikes_remaining: int
    in_flight: int
    blocked_until: str | None
    per_second: int
    per_minute: int


def _ist_now() -> datetime:
    return datetime.now(IST)


def _next_ist_midnight(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=IST)


class FyersGovernor:
    """The one outbound gate: rate windows, in flight semaphore, durable budget, mode machine."""

    def __init__(
        self,
        *,
        settings: Any = None,
        budget_store: BudgetStore | None = None,
        per_second: int | None = None,
        per_minute: int | None = None,
        in_flight: int | None = None,
        daily_budget: int | None = None,
        plan: str | None = None,
        clock: Any = time.monotonic,
        now_ist: Any = _ist_now,
        second_window: float = 1.0,
        minute_window: float = 60.0,
    ) -> None:
        self._settings = settings
        self._store = budget_store or InMemoryBudgetStore()
        self._clock = clock
        self._now_ist = now_ist

        self._per_second = self._setting(per_second, "throttle_per_second", 8)
        self._per_minute = self._setting(per_minute, "throttle_per_minute", 170)
        self._in_flight_limit = self._setting(in_flight, "throttle_in_flight", 6)
        self._daily_budget = self._setting(daily_budget, "daily_budget", 100_000)
        self._plan = plan or self._str_setting("plan_tier", "standard")

        # The window lengths are parameters only so a test can exercise the guarantee
        # deterministically in milliseconds instead of a minute of wall clock. Production always
        # uses one second and one minute, which are the defaults.
        self.second_bucket = SlidingWindowLimiter(self._per_second, second_window, clock=clock)
        self.minute_bucket = SlidingWindowLimiter(self._per_minute, minute_window, clock=clock)
        self._in_flight = asyncio.Semaphore(self._in_flight_limit)
        self._in_flight_count = 0

        self._mode = GovernorMode.RUNNING
        self._reason: str | None = None
        self._running = asyncio.Event()
        self._running.set()
        self._state_lock = asyncio.Lock()

        self._row = self._load_row()
        self._unflushed = 0

    # Construction helpers

    def _setting(self, override: int | None, key: str, fallback: int) -> int:
        if override is not None:
            return int(override)
        if self._settings is None:
            return fallback
        return int(self._settings.get_int(key))

    def _str_setting(self, key: str, fallback: str) -> str:
        if self._settings is None:
            return fallback
        return str(self._settings.get_str(key))

    def _plan_limits(self) -> tuple[int, int]:
        return PLAN_LIMITS.get(self._plan, PLAN_LIMITS["standard"])

    def _today(self) -> str:
        return self._now_ist().date().isoformat()

    def _load_row(self) -> BudgetRow:
        today = self._today()
        existing = self._store.load(today)
        if existing is not None:
            return existing
        limit_day, limit_minute = self._plan_limits()
        row = BudgetRow(
            ist_date=today,
            plan=self._plan,
            plan_limit_day=limit_day,
            plan_limit_minute=limit_minute,
        )
        self._store.save(row)
        return row

    # State

    @property
    def mode(self) -> GovernorMode:
        return self._mode

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def is_running(self) -> bool:
        return self._mode is GovernorMode.RUNNING

    @property
    def requests_used(self) -> int:
        return self._row.requests_used

    @property
    def minute_violations(self) -> int:
        return self._row.minute_violations

    @property
    def strikes_remaining(self) -> int:
        """How many more per minute violations the day can absorb before the block lands."""
        return max(0, MAX_MINUTE_VIOLATIONS - self._row.minute_violations)

    @property
    def daily_budget(self) -> int:
        return self._daily_budget

    @property
    def requests_remaining(self) -> int:
        return max(0, self._daily_budget - self._row.requests_used)

    def snapshot(self) -> GovernorSnapshot:
        return GovernorSnapshot(
            mode=self._mode,
            reason=self._reason,
            ist_date=self._row.ist_date,
            plan=self._row.plan,
            requests_used=self._row.requests_used,
            daily_budget=self._daily_budget,
            plan_limit_day=self._row.plan_limit_day,
            plan_limit_minute=self._row.plan_limit_minute,
            requests_remaining=self.requests_remaining,
            minute_violations=self._row.minute_violations,
            strikes_remaining=self.strikes_remaining,
            in_flight=self._in_flight_count,
            blocked_until=self._row.blocked_until,
            per_second=self._per_second,
            per_minute=self._per_minute,
        )

    # Persistence

    def flush(self) -> None:
        """Persist the daily counter. Called every 25 grants and on every state transition."""
        if self._unflushed == 0:
            return
        self._store.save(self._row)
        self._unflushed = 0

    def _persist_now(self) -> None:
        self._store.save(self._row)
        self._unflushed = 0

    def _roll_day_if_needed(self) -> None:
        """A new IST date is a new quota. It is not a reason to forgive a violation block."""
        today = self._today()
        if today == self._row.ist_date:
            return
        self.flush()
        self._row = self._load_row()
        self._unflushed = 0
        if self._mode is GovernorMode.STOPPED_BUDGET:
            # A new quota day is not a fault, so this one lifts by itself. A rate violation block
            # does not: three strikes is a per day rule and the fourth deserves a human.
            self._set_mode_sync(GovernorMode.RUNNING, reason=None)

    # Mode machine

    def _set_mode_sync(self, mode: GovernorMode, *, reason: str | None) -> None:
        self._mode = mode
        self._reason = reason
        if mode is GovernorMode.RUNNING:
            self._running.set()
        else:
            self._running.clear()
        self._persist_now()
        log.info(
            "pipeline mode changed",
            extra={"mode": str(mode), "reason": reason or ""},
        )

    async def set_mode(self, mode: GovernorMode, *, reason: str | None = None) -> None:
        async with self._state_lock:
            self._set_mode_sync(mode, reason=reason)

    async def pause_user(self, *, reason: str = "paused by the user") -> None:
        await self.set_mode(GovernorMode.PAUSED_USER, reason=reason)

    async def pause_auth(self, *, reason: str = "the access token was rejected") -> None:
        """Called by the token broker the instant an auth error is seen."""
        await self.set_mode(GovernorMode.PAUSED_AUTH, reason=reason)

    async def stop_fatal(self, *, reason: str) -> None:
        await self.set_mode(GovernorMode.STOPPED_FATAL, reason=reason)

    async def resume(self, *, by: str | None = None) -> GovernorMode:
        """Explicit user resume. Refuses while the broker block is still in force."""
        async with self._state_lock:
            self._roll_day_if_needed()
            blocked_until = self._row.blocked_until
            if blocked_until is not None:
                until = datetime.fromisoformat(blocked_until)
                if self._now_ist() < until:
                    raise AccountBlocked(
                        "the account is blocked by the broker until the ist date rolls"
                    )
                self._row = replace(self._row, blocked_until=None)
                self._persist_now()
            self._set_mode_sync(GovernorMode.RUNNING, reason=None)
            log.info("pipeline resumed", extra={"changed_by": by or "user"})
            return self._mode

    async def refresh_day(self) -> GovernorMode:
        """Roll onto the current IST date and lift a budget stop.

        The extension point for the 00:01 IST budget reset schedule. It has to be called from
        outside, because a pipeline stopped on budget has no caller inside `acquire` left awake to
        notice that the date rolled.
        """
        async with self._state_lock:
            self._roll_day_if_needed()
            return self._mode

    # The gate

    async def acquire(self, endpoint: str | None = None) -> None:
        """Block until one outbound request may be sent, then count it.

        Order matters. The mode gate comes first so a paused pipeline consumes no window slots,
        then the two rate windows, then the in flight semaphore, which is released by `release`.
        """
        while True:
            await self._running.wait()
            async with self._state_lock:
                self._roll_day_if_needed()
                if self._row.blocked_until is not None:
                    until = datetime.fromisoformat(self._row.blocked_until)
                    if self._now_ist() < until:
                        if self._mode is not GovernorMode.STOPPED_FATAL:
                            self._set_mode_sync(
                                GovernorMode.STOPPED_FATAL,
                                reason="the account is blocked by the broker for the rest of the day",
                            )
                        continue
                if self._row.requests_used >= self._daily_budget:
                    if self._mode is not GovernorMode.STOPPED_BUDGET:
                        self._set_mode_sync(
                            GovernorMode.STOPPED_BUDGET,
                            reason="the daily request budget is spent",
                        )
                    continue
                if self._mode is not GovernorMode.RUNNING:
                    continue
            # The in flight semaphore is taken BEFORE the rate windows, not after. Taking it last
            # lets a caller consume a window slot and then queue on the semaphore, so a batch of
            # callers that were metered seconds apart are released together and their requests
            # leave in one burst. The window bookkeeping stays self consistent while the real
            # outbound rate exceeds the limit, which is the one failure the three strikes rule
            # punishes with the rest of the day. Taking the semaphore first means the window slot
            # is consumed immediately before the request is issued, so a grant timestamp is a
            # send timestamp.
            await self._in_flight.acquire()
            # The windows are taken outside the state lock, because a caller can wait seconds here
            # and holding the state lock would block a pause from ever landing.
            # The minute window is taken BEFORE the second window, and the second window is the
            # last thing awaited before the request is issued. Order matters for the same reason
            # the semaphore does: whatever is taken first can then block on whatever follows, and
            # every caller released from that later wait fires immediately, in a burst. The minute
            # window is the one that blocks for a long time, so a caller holding a second slot
            # while waiting on it is exactly the burst to avoid. Taking the finest grained window
            # last keeps a grant timestamp within microseconds of a send timestamp even when the
            # machine is loaded enough to overshoot a sleep.
            try:
                await self.minute_bucket.take()
            except BaseException:
                self._in_flight.release()
                raise
            async with self._state_lock:
                if self._mode is not GovernorMode.RUNNING:
                    # The pipeline was paused while this caller waited. Give the slot back rather
                    # than spending a request into a paused pipeline. The minute slot taken above
                    # is forfeited, which costs throughput on a pause and nothing on correctness.
                    self._in_flight.release()
                    continue
                self._in_flight_count += 1
                self._row = replace(self._row, requests_used=self._row.requests_used + 1)
                self._unflushed += 1
                if self._unflushed >= BUDGET_FLUSH_INTERVAL:
                    # A synchronous SQLite write, holding the state lock. Everything queued behind
                    # it resumes at once when it completes, which is precisely why the second
                    # window must not have been granted yet.
                    self._persist_now()
            # The second window is taken LAST, after every other await and after the periodic
            # budget flush, because it is the one whose grant timestamp has to equal the moment
            # the request leaves. Anything that can block between the grant and the send lets a
            # stalled batch resume together and exceed the per second limit while the limiter's
            # own bookkeeping still looks correct. The budget flush above is exactly that: a
            # blocking write, every BUDGET_FLUSH_INTERVAL requests, under a contended lock.
            try:
                await self.second_bucket.take()
            except BaseException:
                self._in_flight.release()
                raise
            return

    def release(self) -> None:
        """Return the in flight slot. Always paired with a successful `acquire`."""
        self._in_flight_count = max(0, self._in_flight_count - 1)
        self._in_flight.release()

    @asynccontextmanager
    async def slot(self, endpoint: str | None = None):
        """The shape the client uses: one governed request per `async with`."""
        await self.acquire(endpoint)
        try:
            yield
        finally:
            self.release()

    # Rate limit reporting

    async def note_rate_limited(
        self,
        *,
        endpoint: str | None = None,
        http_status: int | None = None,
        code: int | None = None,
    ) -> None:
        """Record one rate limit response and stop the pipeline.

        The first strike stops. It does not back off and retry, because the fourth violation in a
        day costs the whole day and an automatic resume is exactly how a crash loop spends all
        three strikes in minutes. The counter is persisted immediately, not on the next flush: a
        strike that a crash forgets is a strike spent twice.
        """
        async with self._state_lock:
            self._roll_day_if_needed()
            violations = self._row.minute_violations + 1
            now = self._now_ist()
            blocked_until = self._row.blocked_until
            if violations > MAX_MINUTE_VIOLATIONS:
                blocked_until = _next_ist_midnight(now).isoformat()
            self._row = replace(
                self._row,
                minute_violations=violations,
                last_429_at=now.isoformat(),
                blocked_until=blocked_until,
            )
            self._persist_now()
            self._record_event(
                kind="http_429" if http_status == 429 else "fyers_429",
                endpoint=endpoint,
                detail=f"violation {violations} of {MAX_MINUTE_VIOLATIONS}, code {code}",
            )
            if violations > MAX_MINUTE_VIOLATIONS:
                self._set_mode_sync(
                    GovernorMode.STOPPED_FATAL,
                    reason="the account is blocked by the broker for the rest of the day",
                )
            else:
                self._set_mode_sync(
                    GovernorMode.PAUSED_RATE,
                    reason=(
                        f"rate limited, {MAX_MINUTE_VIOLATIONS - violations} of "
                        f"{MAX_MINUTE_VIOLATIONS} strikes remain today"
                    ),
                )

    def _record_event(self, *, kind: str, endpoint: str | None, detail: str | None) -> None:
        recorder = getattr(self._store, "record_event", None)
        if recorder is None:
            return
        try:
            recorder(kind=kind, endpoint=endpoint, detail=detail)
        except Exception:  # pragma: no cover - provenance must never break the gate
            log.warning("could not record a rate event", extra={"kind": kind})

    async def aclose(self) -> None:
        """Persist whatever the counter has not flushed yet. Called from the lifespan."""
        self.flush()
