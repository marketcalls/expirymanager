"""Startup and shutdown, written as one explicit ordered sequence.

The order matters and it is not obvious, so it is data rather than a long function body. Each step
names what it starts and how it is torn down, `run_startup` walks the list forward recording what
actually started, and `run_shutdown` walks the record backward. A step that fails half way through
startup therefore tears down exactly the steps that came before it and nothing else, which is the
one property a try/finally ladder gets wrong as soon as a sixth resource is added.

The sequence is:

  1  sqlite              open the engine over config.sqlite3
  2  migrations          bring the schema up to date
  3  settings            construct the one SettingsStore
  4  keys                provision the KEK file and the DEK, load the hierarchy
  5  duckdb              open market.duckdb and start the single writer task
  6  http_security       the SessionManager and the RateLimiter the middleware resolves
  7  fyers               the governor, the token broker and the shared HTTP client
  8  pipeline_supervisor W11 fills this slot
  9  job_recovery        W11 and W12 fill this slot
 10  scheduler           W14 fills this slot

Steps 8 to 10 are registration points, not stubs. Nothing is written there that has to be deleted
later: `register_component` takes a factory, the step calls it when one is registered and does
nothing when one is not, and the application serves correctly today with all three empty. The
process model in ARCHITECTURE.md section 3 is what forces them into this one file: the scheduler
and the download workers run as tasks inside this process, so their lifetime is the lifetime of
the app and there is nowhere else for them to be started.

`__main__` already holds the advisory instance lock for the life of the process. Acquiring a
second one here would raise `SingleInstanceError` from the same process, so this file never
touches the lock.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from expirymanager import bootstrap as bootstrap_module
from expirymanager import paths as paths_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.duck import DuckStore
from expirymanager.security.headers import ENVIRONMENT_DEVELOPMENT
from expirymanager.security.ratelimit import ConcurrencyLimiter, RateLimiter
from expirymanager.security.sessions import SessionManager
from expirymanager.version import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy import Engine

    from expirymanager.db.reader import DuckReader
    from expirymanager.db.writer import DuckWriter
    from expirymanager.security.keys import KeyManager
    from expirymanager.settings_store import SettingsStore

__all__ = [
    "AppState",
    "LifecycleStep",
    "STARTUP_SEQUENCE",
    "SLOT_PIPELINE_SUPERVISOR",
    "SLOT_JOB_RECOVERY",
    "SLOT_SCHEDULER",
    "COMPONENT_SLOTS",
    "register_component",
    "unregister_component",
    "registered_components",
    "run_startup",
    "run_shutdown",
    "lifespan_context",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The container every dependency reads from
# ---------------------------------------------------------------------------


@dataclass
class AppState:
    """Everything the lifespan built, hung off `app.state.services`.

    Fields are optional because they are filled in sequence, and because a route asking for a
    service that failed to start must get a clean 503 from `api/deps.py` rather than an
    AttributeError rendered as a 500.
    """

    paths: paths_module.Paths
    environment: str = ENVIRONMENT_DEVELOPMENT
    app_version: str = __version__

    engine: "Engine | None" = None
    settings: "SettingsStore | None" = None
    key_manager: "KeyManager | None" = None
    duck: DuckStore | None = None

    session_manager: SessionManager | None = None
    rate_limiter: RateLimiter | None = None
    stream_limiter: ConcurrencyLimiter | None = None

    governor: Any = None
    token_broker: Any = None
    fyers_client: Any = None

    # Filled by the registration points below. Empty is a valid state.
    components: dict[str, Any] = field(default_factory=dict)

    started_at: datetime | None = None
    ready: bool = False

    @property
    def duck_reader(self) -> "DuckReader | None":
        return None if self.duck is None else self.duck.reader

    @property
    def duck_writer(self) -> "DuckWriter | None":
        return None if self.duck is None else self.duck.writer

    @property
    def supervisor(self) -> Any:
        """The pipeline supervisor once W11 has registered it, otherwise None."""
        return self.components.get(SLOT_PIPELINE_SUPERVISOR)

    @property
    def scheduler(self) -> Any:
        """The scheduler service once W14 has registered it, otherwise None."""
        return self.components.get(SLOT_SCHEDULER)


# ---------------------------------------------------------------------------
# The step machinery
# ---------------------------------------------------------------------------

StepFn = Callable[[AppState], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class LifecycleStep:
    """One startup step and its matching teardown.

    `stop` is only ever called for a step whose `start` returned, which is what makes a partial
    startup tear down cleanly.
    """

    name: str
    start: StepFn
    stop: StepFn | None = None


async def _call(fn: StepFn, state: AppState) -> None:
    result = fn(state)
    if inspect.isawaitable(result):
        await result


# ---------------------------------------------------------------------------
# Registration points for the items that are not built yet
# ---------------------------------------------------------------------------

# W11 owns pipeline/supervisor.py. It registers a factory that returns the PipelineSupervisor.
SLOT_PIPELINE_SUPERVISOR = "pipeline_supervisor"

# W11 (queue.reclaim_expired_leases) and W12 (JobService) own the recovery listed in PIPELINE.md
# section 6: return leased rows to pending, move interrupted running jobs to paused, roll a
# deferred_budget job whose IST date has passed back to queued, and preserve a paused_rate or
# stopped_fatal pipeline mode so a restart cannot resume into a block. The factory registered here
# performs that work and may return None; there is nothing to shut down.
SLOT_JOB_RECOVERY = "job_recovery"

# W14 owns scheduler/service.py. It registers a factory that returns the SchedulerService, whose
# start() builds the AsyncIOScheduler and calls sync().
SLOT_SCHEDULER = "scheduler"

# Started in this order, shut down in the reverse. Recovery sits between the supervisor and the
# scheduler on purpose: the workers must exist before interrupted work is requeued, and the
# scheduler must not fire a job before that requeue has settled.
COMPONENT_SLOTS: tuple[str, ...] = (
    SLOT_PIPELINE_SUPERVISOR,
    SLOT_JOB_RECOVERY,
    SLOT_SCHEDULER,
)

ComponentFactory = Callable[[AppState], Any]

_component_factories: dict[str, ComponentFactory] = {}


def register_component(slot: str, factory: ComponentFactory) -> None:
    """Register the factory for one lifespan slot.

    The factory is called with the `AppState` once every service it can depend on has started. It
    returns the component, or None when the slot only has work to do and nothing to hold. When the
    returned object has `start`, it is awaited; on shutdown the first of `stop`, `shutdown` or
    `aclose` that exists is awaited. Either method may be synchronous.
    """
    if slot not in COMPONENT_SLOTS:
        raise ValueError(f"unknown lifespan slot: {slot!r}")
    _component_factories[slot] = factory


def unregister_component(slot: str) -> None:
    """Drop a registered factory. Used by tests, and by nothing in production."""
    _component_factories.pop(slot, None)


def registered_components() -> tuple[str, ...]:
    """The slots that currently have a factory. Reported by the startup log line."""
    return tuple(slot for slot in COMPONENT_SLOTS if slot in _component_factories)


async def _start_component(state: AppState, slot: str) -> None:
    factory = _component_factories.get(slot)
    if factory is None:
        log.debug("lifespan slot is empty", extra={"lifespan_slot": slot})
        return
    component = factory(state)
    if inspect.isawaitable(component):
        component = await component
    if component is None:
        return
    starter = getattr(component, "start", None)
    if callable(starter):
        result = starter()
        if inspect.isawaitable(result):
            await result
    state.components[slot] = component
    log.info("lifespan slot started", extra={"lifespan_slot": slot})


async def _stop_component(state: AppState, slot: str) -> None:
    component = state.components.pop(slot, None)
    if component is None:
        return
    for name in ("stop", "shutdown", "aclose"):
        stopper = getattr(component, name, None)
        if callable(stopper):
            result = stopper()
            if inspect.isawaitable(result):
                await result
            break
    log.info("lifespan slot stopped", extra={"lifespan_slot": slot})


def _component_step(slot: str) -> LifecycleStep:
    return LifecycleStep(
        name=slot,
        start=lambda state, _slot=slot: _start_component(state, _slot),
        stop=lambda state, _slot=slot: _stop_component(state, _slot),
    )


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def _open_sqlite(state: AppState) -> None:
    # A module-level engine as well as the one on the state, because db/sqlite.py exposes
    # get_engine() for callers that reach for it without a request in hand.
    #
    # The dispose first is what makes a second create_app in one process correct: init_engine
    # returns the existing engine whatever path it was opened against, so without this a test
    # building an app over a fresh temporary directory would silently get the previous database.
    # One process serves one instance, which is enforced by the advisory lock, so there is never
    # a live engine here worth keeping.
    sqlite_module.dispose_engine()
    state.engine = sqlite_module.init_engine(state.paths.sqlite_db)


def _close_sqlite(state: AppState) -> None:
    sqlite_module.dispose_engine()
    state.engine = None


async def _run_migrations(state: AppState) -> None:
    engine = _require(state.engine, "sqlite engine")
    await run_in_threadpool(bootstrap_module.apply_migrations, engine)


def _build_settings(state: AppState) -> None:
    from expirymanager.settings_store import SettingsStore

    # Exactly one, injected everywhere. It is a read-through cache, so a second instance would
    # serve a stale value after the first one wrote.
    state.settings = SettingsStore(_require(state.engine, "sqlite engine"))


async def _init_keys(state: AppState) -> None:
    engine = _require(state.engine, "sqlite engine")
    manager = bootstrap_module.build_key_manager(engine, state.paths)
    await run_in_threadpool(bootstrap_module.ensure_key_hierarchy, manager)
    state.key_manager = manager


def _clear_keys(state: AppState) -> None:
    if state.key_manager is not None:
        # Unwrapped DEKs are process memory, so they are dropped explicitly rather than left to
        # the garbage collector.
        state.key_manager.clear_cache()
    state.key_manager = None


async def _open_duckdb(state: AppState) -> None:
    store = DuckStore(
        state.paths.duckdb_file,
        temp_directory=state.paths.tmp_dir,
        app_version=state.app_version,
    )
    # start() opens the file, applies the DDL and the macros, and starts the single writer task.
    await store.start()
    state.duck = store


async def _close_duckdb(state: AppState) -> None:
    if state.duck is not None:
        # Drains the writer queue, CHECKPOINTs the write ahead log into the main file and closes.
        await state.duck.aclose()
    state.duck = None


def _build_http_security(state: AppState) -> None:
    engine = _require(state.engine, "sqlite engine")
    state.session_manager = SessionManager(engine)
    # One limiter, shared by the middleware and by the login route, so both hit one storage.
    state.rate_limiter = RateLimiter()
    state.stream_limiter = ConcurrencyLimiter()


async def _build_fyers(state: AppState) -> None:
    from expirymanager.brokers.fyers.client import FyersClient
    from expirymanager.brokers.fyers.throttle import FyersGovernor, SqliteBudgetStore
    from expirymanager.brokers.fyers.tokens import (
        SqliteCredentialStore,
        SqliteTokenStore,
        TokenBroker,
    )

    engine = _require(state.engine, "sqlite engine")
    key_manager = _require(state.key_manager, "key manager")

    governor = FyersGovernor(settings=state.settings, budget_store=SqliteBudgetStore(engine))
    broker = TokenBroker(
        credentials=SqliteCredentialStore(engine, key_manager),
        tokens=SqliteTokenStore(engine, key_manager),
        governor=governor,
    )
    # Prime the broker so the auth gate reflects the stored token before any worker waits on it.
    # Synchronous SQLite plus one decryption, so it goes through the thread pool.
    await run_in_threadpool(broker.reload)

    state.governor = governor
    state.token_broker = broker
    state.fyers_client = FyersClient(tokens=broker, governor=governor)


async def _close_fyers(state: AppState) -> None:
    if state.fyers_client is not None:
        await state.fyers_client.aclose()
    if state.governor is not None:
        # Flushes the unpersisted part of the daily request counter. Without it a restart loses
        # up to 25 requests of budget, which is how a daily ceiling drifts upward over a week.
        await state.governor.aclose()
    state.fyers_client = None
    state.token_broker = None
    state.governor = None


STARTUP_SEQUENCE: tuple[LifecycleStep, ...] = (
    LifecycleStep("sqlite", _open_sqlite, _close_sqlite),
    LifecycleStep("migrations", _run_migrations),
    LifecycleStep("settings", _build_settings),
    LifecycleStep("keys", _init_keys, _clear_keys),
    LifecycleStep("duckdb", _open_duckdb, _close_duckdb),
    LifecycleStep("http_security", _build_http_security),
    LifecycleStep("fyers", _build_fyers, _close_fyers),
    _component_step(SLOT_PIPELINE_SUPERVISOR),
    _component_step(SLOT_JOB_RECOVERY),
    _component_step(SLOT_SCHEDULER),
)


def _require(value: Any, what: str) -> Any:
    if value is None:
        raise RuntimeError(f"{what} is not available, startup did not reach that step")
    return value


# ---------------------------------------------------------------------------
# Running the sequence
# ---------------------------------------------------------------------------


async def run_startup(
    state: AppState, sequence: tuple[LifecycleStep, ...] = STARTUP_SEQUENCE
) -> list[LifecycleStep]:
    """Walk the sequence forward. On failure, unwind what started and re-raise."""
    started: list[LifecycleStep] = []
    try:
        for step in sequence:
            await _call(step.start, state)
            started.append(step)
    except Exception:
        log.exception("startup failed", extra={"lifespan_step": len(started)})
        await run_shutdown(state, started)
        raise
    state.started_at = datetime.now(UTC)
    state.ready = True
    log.info(
        "application ready",
        extra={
            "data_dir": str(state.paths.root),
            "app_version": state.app_version,
            "environment": state.environment,
            "lifespan_slots_filled": list(registered_components()),
        },
    )
    return started


async def run_shutdown(state: AppState, started: list[LifecycleStep]) -> None:
    """Tear down in the exact reverse of the order things started.

    Every teardown runs even if one of them raises, because a failure to stop the scheduler must
    not leave the DuckDB write ahead log uncheckpointed.
    """
    state.ready = False
    for step in reversed(started):
        if step.stop is None:
            continue
        try:
            await _call(step.stop, state)
        except Exception:  # noqa: BLE001 - one bad teardown must not skip the rest
            log.exception("shutdown step failed", extra={"lifespan_step": step.name})


@asynccontextmanager
async def lifespan_context(app: Any, state: AppState):
    """The FastAPI lifespan. `app.state.services` is set before the first step runs."""
    app.state.services = state
    started = await run_startup(state)
    try:
        yield
    finally:
        await run_shutdown(state, started)
