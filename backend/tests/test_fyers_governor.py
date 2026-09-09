"""The outbound governor: rate windows under concurrency, durable counters, the mode machine.

The window lengths are scaled down for the concurrency tests, so the guarantee is exercised in
milliseconds rather than in a minute of wall clock. The invariant asserted is the real one and is
checked against the configured window, not against a wall clock approximation of it: no window of
that length may ever hold more than its limit of grants. A slow machine only spreads grants further
apart, so the assertion cannot fail spuriously.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from expirymanager.brokers.fyers.governor import FyersGovernor as GovernorAlias
from expirymanager.brokers.fyers.throttle import (
    BUDGET_FLUSH_INTERVAL,
    IST,
    MAX_MINUTE_VIOLATIONS,
    AccountBlocked,
    BudgetRow,
    FyersGovernor,
    GovernorMode,
    InMemoryBudgetStore,
    SlidingWindowLimiter,
)

# Short enough to run fast, long enough that scheduling noise cannot make a window look empty.
FAST_SECOND = 0.05
FAST_MINUTE = 3.0

# Long enough that a blocked caller cannot be mistaken for a slow one.
BLOCKED_FOR = 0.25

FIXED_IST_NOW = datetime(2026, 9, 9, 10, 30, tzinfo=IST)


def _governor(
    *,
    store: InMemoryBudgetStore | None = None,
    per_second: int = 8,
    per_minute: int = 170,
    in_flight: int = 6,
    daily_budget: int = 100_000,
    now_ist=None,
    second_window: float = FAST_SECOND,
    minute_window: float = FAST_MINUTE,
) -> FyersGovernor:
    return FyersGovernor(
        budget_store=store or InMemoryBudgetStore(),
        per_second=per_second,
        per_minute=per_minute,
        in_flight=in_flight,
        daily_budget=daily_budget,
        now_ist=now_ist or (lambda: FIXED_IST_NOW),
        second_window=second_window,
        minute_window=minute_window,
    )


def _assert_never_exceeds(grants: list[float], limit: int, window: float) -> None:
    for index, at in enumerate(grants):
        inside = [other for other in grants[: index + 1] if other > at - window]
        assert len(inside) <= limit, f"{len(inside)} grants inside one window of {window}s"


async def _blocks(coro_task: asyncio.Task) -> bool:
    """True when the task is still waiting after a grace period."""
    done, _pending = await asyncio.wait({coro_task}, timeout=BLOCKED_FOR)
    return not done


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# The window limiter


async def test_the_limiter_never_grants_more_than_its_limit_in_one_window() -> None:
    limiter = SlidingWindowLimiter(8, FAST_SECOND)
    grants: list[float] = []

    async def caller() -> None:
        await limiter.take()
        grants.append(asyncio.get_running_loop().time())

    await asyncio.gather(*[caller() for _ in range(200)])

    assert len(grants) == 200
    _assert_never_exceeds(grants, 8, FAST_SECOND)


async def test_a_full_window_makes_the_next_caller_wait() -> None:
    limiter = SlidingWindowLimiter(2, 0.5)
    await limiter.take()
    await limiter.take()

    waiter = asyncio.create_task(limiter.take())
    done, _ = await asyncio.wait({waiter}, timeout=0.05)
    assert not done
    await asyncio.wait_for(waiter, timeout=2.0)


async def test_the_limiter_reports_how_long_it_made_a_caller_wait() -> None:
    limiter = SlidingWindowLimiter(1, 0.2)
    assert await limiter.take() == 0.0
    assert await limiter.take() > 0.0


def test_a_limiter_of_zero_is_refused() -> None:
    with pytest.raises(ValueError):
        SlidingWindowLimiter(0, 1.0)


# The governor under concurrency


async def test_two_hundred_concurrent_callers_respect_both_windows() -> None:
    governor = _governor()
    grants: list[float] = []

    async def caller() -> None:
        async with governor.slot("expired-historical-data"):
            grants.append(asyncio.get_running_loop().time())

    await asyncio.gather(*[caller() for _ in range(200)])

    assert len(grants) == 200
    _assert_never_exceeds(grants, 8, FAST_SECOND)
    _assert_never_exceeds(grants, 170, FAST_MINUTE)
    assert governor.requests_used == 200


async def test_the_per_minute_window_is_the_binding_constraint_beyond_its_limit() -> None:
    # 200 requests against a limit of 170 means the last 30 cannot be granted until the window
    # has slid, which is what stops a burst from spending a strike.
    governor = _governor(per_second=100, per_minute=170)
    grants: list[float] = []

    async def caller() -> None:
        async with governor.slot("history"):
            grants.append(asyncio.get_running_loop().time())

    await asyncio.gather(*[caller() for _ in range(200)])
    _assert_never_exceeds(grants, 170, FAST_MINUTE)
    assert grants[-1] - grants[0] >= FAST_MINUTE * 0.9


async def test_the_in_flight_semaphore_caps_concurrent_requests() -> None:
    governor = _governor(in_flight=6)
    concurrent = 0
    peak = 0
    release = asyncio.Event()

    async def caller() -> None:
        nonlocal concurrent, peak
        async with governor.slot("history"):
            concurrent += 1
            peak = max(peak, concurrent)
            await release.wait()
            concurrent -= 1

    tasks = [asyncio.create_task(caller()) for _ in range(40)]
    await asyncio.sleep(FAST_SECOND * 2)
    assert peak == 6

    release.set()
    await asyncio.gather(*tasks)
    assert peak == 6
    assert governor.snapshot().in_flight == 0


async def test_the_in_flight_slot_is_returned_even_when_the_body_raises() -> None:
    governor = _governor(in_flight=1)
    with pytest.raises(RuntimeError):
        async with governor.slot("history"):
            raise RuntimeError("boom")
    # A leaked slot would deadlock the very next caller.
    await asyncio.wait_for(governor.acquire("history"), timeout=1.0)
    governor.release()
    assert governor.snapshot().in_flight == 0


# The durable counters


async def test_the_daily_counter_survives_a_restart() -> None:
    store = InMemoryBudgetStore()
    governor = _governor(store=store)
    for _ in range(BUDGET_FLUSH_INTERVAL):
        async with governor.slot("profile"):
            pass
    assert governor.requests_used == BUDGET_FLUSH_INTERVAL

    # A restart reads the same store, so the ceiling cannot be reset by cycling the process.
    restarted = _governor(store=store)
    assert restarted.requests_used == BUDGET_FLUSH_INTERVAL
    assert restarted.requests_remaining == 100_000 - BUDGET_FLUSH_INTERVAL


async def test_the_counter_is_flushed_on_the_interval_and_on_close() -> None:
    store = InMemoryBudgetStore()
    governor = _governor(store=store)
    today = governor.snapshot().ist_date

    async with governor.slot("profile"):
        pass
    # Not yet flushed: one write per request would be write amplification for no benefit.
    assert store.load(today).requests_used == 0

    await governor.aclose()
    assert store.load(today).requests_used == 1


async def test_the_counter_is_flushed_once_the_interval_is_reached() -> None:
    store = InMemoryBudgetStore()
    governor = _governor(store=store)
    today = governor.snapshot().ist_date
    for _ in range(BUDGET_FLUSH_INTERVAL):
        async with governor.slot("profile"):
            pass
    assert store.load(today).requests_used == BUDGET_FLUSH_INTERVAL


async def test_spending_the_daily_budget_stops_the_pipeline() -> None:
    governor = _governor(daily_budget=3)
    for _ in range(3):
        async with governor.slot("profile"):
            pass
    assert governor.requests_remaining == 0

    blocked = asyncio.create_task(governor.acquire("profile"))
    assert await _blocks(blocked)
    assert governor.mode is GovernorMode.STOPPED_BUDGET
    await _cancel(blocked)


async def test_a_new_ist_day_lifts_a_budget_stop_and_restores_the_quota() -> None:
    day = datetime(2026, 9, 9, 23, 59, tzinfo=IST)
    governor = _governor(daily_budget=1, now_ist=lambda: day)

    async with governor.slot("profile"):
        pass
    blocked = asyncio.create_task(governor.acquire("profile"))
    assert await _blocks(blocked)
    assert governor.mode is GovernorMode.STOPPED_BUDGET

    day = day + timedelta(minutes=2)
    assert await governor.refresh_day() is GovernorMode.RUNNING
    await asyncio.wait_for(blocked, timeout=2.0)
    governor.release()
    assert governor.requests_used == 1
    assert governor.snapshot().ist_date == "2026-09-10"


# The three strikes rule


async def test_the_first_rate_limit_stops_the_pipeline_rather_than_retrying() -> None:
    governor = _governor()
    await governor.note_rate_limited(
        endpoint="expired-historical-data", http_status=429, code=-429
    )

    assert governor.mode is GovernorMode.PAUSED_RATE
    assert governor.minute_violations == 1
    assert governor.strikes_remaining == MAX_MINUTE_VIOLATIONS - 1
    assert "2 of 3 strikes remain" in (governor.reason or "")

    # Automatic resume after a rate violation is how a crash loop burns all three strikes in
    # minutes, so the pipeline stays parked until a person resumes it.
    waiting = asyncio.create_task(governor.acquire("history"))
    assert await _blocks(waiting)

    await governor.resume(by="test")
    await asyncio.wait_for(waiting, timeout=2.0)
    governor.release()
    assert governor.mode is GovernorMode.RUNNING


async def test_a_violation_is_persisted_immediately_not_on_the_next_flush() -> None:
    store = InMemoryBudgetStore()
    governor = _governor(store=store)
    await governor.note_rate_limited(endpoint="history", http_status=429, code=-429)
    # A strike that a crash forgets is a strike that gets spent twice.
    row = store.load(governor.snapshot().ist_date)
    assert row.minute_violations == 1
    assert row.last_429_at is not None


async def test_a_fourth_violation_blocks_the_account_for_the_rest_of_the_day() -> None:
    now = datetime(2026, 9, 9, 14, 0, tzinfo=IST)
    governor = _governor(now_ist=lambda: now)

    for _ in range(MAX_MINUTE_VIOLATIONS):
        await governor.note_rate_limited(endpoint="history", http_status=429, code=-429)
        await governor.resume(by="test")
    assert governor.strikes_remaining == 0
    assert governor.mode is GovernorMode.RUNNING

    await governor.note_rate_limited(endpoint="history", http_status=429, code=-429)
    assert governor.mode is GovernorMode.STOPPED_FATAL
    snapshot = governor.snapshot()
    assert snapshot.blocked_until is not None
    assert datetime.fromisoformat(snapshot.blocked_until) == datetime(
        2026, 9, 10, 0, 0, tzinfo=IST
    )

    # Resume cannot talk the broker out of its block.
    with pytest.raises(AccountBlocked):
        await governor.resume(by="test")


async def test_a_block_read_back_from_the_store_still_stops_the_pipeline() -> None:
    now = datetime(2026, 9, 9, 14, 0, tzinfo=IST)
    store = InMemoryBudgetStore()
    store.save(
        BudgetRow(
            ist_date="2026-09-09",
            plan="standard",
            plan_limit_day=100_000,
            plan_limit_minute=200,
            requests_used=400,
            minute_violations=4,
            blocked_until=datetime(2026, 9, 10, 0, 0, tzinfo=IST).isoformat(),
        )
    )
    governor = _governor(store=store, now_ist=lambda: now)
    assert governor.minute_violations == 4
    assert governor.strikes_remaining == 0

    waiting = asyncio.create_task(governor.acquire("history"))
    assert await _blocks(waiting)
    assert governor.mode is GovernorMode.STOPPED_FATAL
    await _cancel(waiting)


# The mode machine


async def test_a_paused_pipeline_spends_no_requests() -> None:
    governor = _governor()
    await governor.pause_auth(reason="the broker rejected the access token")
    assert governor.mode is GovernorMode.PAUSED_AUTH

    waiting = asyncio.create_task(governor.acquire("history"))
    assert await _blocks(waiting)
    assert governor.requests_used == 0
    await _cancel(waiting)


async def test_pausing_the_user_and_resuming_round_trips() -> None:
    governor = _governor()
    await governor.pause_user()
    assert governor.mode is GovernorMode.PAUSED_USER
    assert not governor.is_running
    await governor.resume(by="test")
    assert governor.is_running
    assert governor.reason is None


async def test_a_pause_that_lands_while_a_caller_waits_does_not_spend_a_request() -> None:
    governor = _governor(in_flight=1)
    await governor.acquire("history")
    assert governor.requests_used == 1

    waiting = asyncio.create_task(governor.acquire("history"))
    await asyncio.sleep(0)
    await governor.pause_user()
    governor.release()

    assert await _blocks(waiting)
    assert governor.requests_used == 1
    await _cancel(waiting)


async def test_the_snapshot_reports_what_the_budget_screen_needs() -> None:
    governor = _governor()
    async with governor.slot("profile"):
        pass
    snapshot = governor.snapshot()
    assert snapshot.mode is GovernorMode.RUNNING
    assert snapshot.requests_used == 1
    assert snapshot.requests_remaining == 99_999
    assert snapshot.strikes_remaining == MAX_MINUTE_VIOLATIONS
    assert snapshot.per_second == 8
    assert snapshot.per_minute == 170
    assert snapshot.plan == "standard"


def test_the_governor_alias_names_the_same_class() -> None:
    assert GovernorAlias is FyersGovernor


def test_the_targets_default_below_the_published_ceilings() -> None:
    # Published Standard limits are 10 per second and 200 per minute. The margin buys cover for
    # clock skew and in flight retries against the cost of losing a whole day of quota.
    governor = FyersGovernor(budget_store=InMemoryBudgetStore())
    snapshot = governor.snapshot()
    assert snapshot.per_second == 8
    assert snapshot.per_minute == 170
    assert governor.daily_budget == 100_000
    assert snapshot.plan_limit_day == 100_000
    assert snapshot.plan_limit_minute == 200
    assert governor.second_bucket.limit == 8
    assert governor.minute_bucket.limit == 170


def test_the_governor_reads_its_targets_from_the_settings_store() -> None:
    from expirymanager.settings_store import SettingsStore

    settings = SettingsStore()
    governor = FyersGovernor(settings=settings, budget_store=InMemoryBudgetStore())
    assert governor.second_bucket.limit == settings.get_int("throttle_per_second")
    assert governor.minute_bucket.limit == settings.get_int("throttle_per_minute")
    assert governor.daily_budget == settings.get_int("daily_budget")
    assert governor.snapshot().plan == settings.get_str("plan_tier")


# The real SQLite budget store


@pytest.fixture()
def engine(tmp_path):
    from expirymanager.db import migrate, sqlite

    engine = sqlite.create_engine(tmp_path / "expirymanager.sqlite3")
    migrate.migrate(engine)
    try:
        yield engine
    finally:
        engine.dispose()


async def test_the_daily_counter_survives_a_restart_through_sqlite(engine) -> None:
    from sqlalchemy import text as sa_text

    from expirymanager.brokers.fyers.throttle import SqliteBudgetStore

    store = SqliteBudgetStore(engine)
    governor = _governor(store=store, daily_budget=100_000)
    today = governor.snapshot().ist_date

    for _ in range(BUDGET_FLUSH_INTERVAL):
        async with governor.slot("expired-historical-data"):
            pass
    await governor.note_rate_limited(endpoint="history", http_status=429, code=-429)

    with engine.connect() as connection:
        row = connection.execute(
            sa_text(
                "SELECT requests_used, minute_violations, plan_limit_day, last_429_at"
                " FROM api_budget WHERE ist_date = :d"
            ),
            {"d": today},
        ).fetchone()
    assert row[0] == BUDGET_FLUSH_INTERVAL
    assert row[1] == 1
    assert row[2] == 100_000
    assert row[3] is not None

    # A restart cannot reset either counter, which is the whole point of persisting them.
    restarted = _governor(store=SqliteBudgetStore(engine))
    assert restarted.requests_used == BUDGET_FLUSH_INTERVAL
    assert restarted.minute_violations == 1
    assert restarted.strikes_remaining == MAX_MINUTE_VIOLATIONS - 1


async def test_a_rate_event_row_is_written_for_provenance(engine) -> None:
    from sqlalchemy import text as sa_text

    from expirymanager.brokers.fyers.throttle import SqliteBudgetStore

    governor = _governor(store=SqliteBudgetStore(engine))
    await governor.note_rate_limited(endpoint="history", http_status=429, code=-429)

    with engine.connect() as connection:
        row = connection.execute(
            sa_text("SELECT kind, endpoint, detail FROM rate_event")
        ).fetchone()
    assert row[0] == "http_429"
    assert row[1] == "history"
    assert "violation 1 of 3" in row[2]
