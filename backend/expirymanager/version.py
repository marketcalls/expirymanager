"""Single source of the application version string.

Written into the DuckDB `meta` table at bootstrap, reported by `GET /api/v1/system/health`, and
sent as the outbound User-Agent, so a stored dataset can always be traced to the build that wrote
it.
"""

from __future__ import annotations

APP_NAME = "ExpiryManager"
APP_SLUG = "expirymanager"

__version__ = "1.0.0"

# Bumped independently of the app version. A dataset written by an older schema is readable, a
# dataset written by a newer one is not, so bootstrap compares this and refuses to open forward.
SCHEMA_GENERATION = 1


def user_agent() -> str:
    """Outbound User-Agent for every Fyers request and for the public symbol master fetch."""
    return f"{APP_NAME}/{__version__}"
