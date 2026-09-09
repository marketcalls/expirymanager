"""The outbound governor, under the name the pipeline documentation uses for the concept.

The implementation lives in `throttle.py`, which is the filename ARCHITECTURE.md section 6 and
PIPELINE.md section 2 both name. This module exists so `from ... import governor` reads the way
the design talks about it, and so there is one obvious place to look for either name. It adds no
behaviour: there is still exactly one `FyersGovernor` class and exactly one instance per process.
"""

from __future__ import annotations

from expirymanager.brokers.fyers.throttle import (
    BUDGET_FLUSH_INTERVAL,
    IST,
    MAX_MINUTE_VIOLATIONS,
    PLAN_LIMITS,
    AccountBlocked,
    BudgetRow,
    BudgetStore,
    FyersGovernor,
    GovernorError,
    GovernorMode,
    GovernorSnapshot,
    InMemoryBudgetStore,
    SlidingWindowLimiter,
    SqliteBudgetStore,
)

__all__ = [
    "IST",
    "MAX_MINUTE_VIOLATIONS",
    "BUDGET_FLUSH_INTERVAL",
    "PLAN_LIMITS",
    "AccountBlocked",
    "BudgetRow",
    "BudgetStore",
    "FyersGovernor",
    "GovernorError",
    "GovernorMode",
    "GovernorSnapshot",
    "InMemoryBudgetStore",
    "SlidingWindowLimiter",
    "SqliteBudgetStore",
]
