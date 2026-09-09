"""`GET /api/v1/bootstrap`, the only fact the SPA reads before it decides which screen to render.

Public, because it is the answer to "is there anything to log in to". It is also the one route
that must keep working when nothing else does: the setup wizard cannot be reached any other way,
and a browser that cannot read this renders the retry screen and nothing else.
"""

from __future__ import annotations

import duckdb
from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from expirymanager import bootstrap as bootstrap_module
from expirymanager.api.deps import EngineDep, StateDep
from expirymanager.api.schemas.common import ApiModel

__all__ = ["router", "BootstrapResponse"]

router = APIRouter()


class BootstrapResponse(ApiModel):
    """API.md section 1. Read once at startup and cached by the SPA until setup changes it."""

    provisioned: bool
    has_user: bool
    has_credentials: bool
    broker_connected: bool
    token_state: str
    token_expires_at: str | None = None
    needs_reauth: bool
    data_dir: str
    app_version: str
    duckdb_version: str


@router.get("/bootstrap", response_model=BootstrapResponse, summary="Application bootstrap state")
async def read_bootstrap(state: StateDep, engine: EngineDep) -> BootstrapResponse:
    status = await run_in_threadpool(
        bootstrap_module.read_status,
        engine,
        paths=state.paths,
        token_broker=state.token_broker,
        duckdb_version=duckdb.__version__,
    )
    return BootstrapResponse(
        provisioned=status.provisioned,
        has_user=status.has_user,
        has_credentials=status.has_credentials,
        broker_connected=status.broker_connected,
        token_state=status.token_state,
        token_expires_at=status.token_expires_at,
        needs_reauth=status.needs_reauth,
        data_dir=status.data_dir,
        app_version=status.app_version,
        duckdb_version=status.duckdb_version,
    )
