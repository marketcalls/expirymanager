"""The four registrations that turn a running application into a working one.

Every one of these components exposes an explicit install() rather than registering as an import
side effect, which is the right call: registering on import makes the installed set depend on
import order and changes the behaviour of any test that merely imports the module. The cost is
that something has to call them, and for a while nothing did.

That omission is invisible from outside. The application starts, every route answers, the health
check is green. What actually happens is that the three lifespan slots stay empty, the worker
handler registry stays empty, api.deps.get_supervisor raises its documented 503, no schedule fires,
and any leased task dies with "no handler is registered for task kind". A download appears to start
and then silently does nothing.

These tests exist so that failure is loud and immediate instead.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from expirymanager import lifespan
from expirymanager.app import create_app
from expirymanager.pipeline import worker

# Every kind the planner and the scheduler can write onto a task row. A handler missing here means
# jobs of that kind fail at lease time, in production, with no test having noticed.
EXPECTED_TASK_KINDS = {
    "candle_chunk",
    "chain_snapshot",
    "expiry_dates",
    "spot_chunk",
    "symbol_master",
    "underlying_symbols",
}


@pytest.fixture
def built_app():
    with tempfile.TemporaryDirectory() as directory:
        yield create_app(root=pathlib.Path(directory), serve_static=False)


def test_every_lifespan_slot_has_a_factory(built_app) -> None:
    registered = set(lifespan.registered_components())
    missing = set(lifespan.COMPONENT_SLOTS) - registered
    assert not missing, f"lifespan slots with no factory: {sorted(missing)}"


def test_the_worker_registry_holds_a_handler_for_every_task_kind(built_app) -> None:
    registry = worker.handler_registry()
    kinds = set(registry.kinds()) if hasattr(registry, "kinds") else set(registry)
    missing = EXPECTED_TASK_KINDS - kinds
    assert not missing, f"task kinds with no handler: {sorted(missing)}"


def test_building_the_app_twice_does_not_double_register(built_app) -> None:
    # create_app runs per process in production but repeatedly across a test session, and an
    # installer that appends rather than replaces would grow the registry every time.
    before = len(lifespan.registered_components())
    with tempfile.TemporaryDirectory() as directory:
        create_app(root=pathlib.Path(directory), serve_static=False)
    assert len(lifespan.registered_components()) == before
