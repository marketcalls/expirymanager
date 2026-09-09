"""The dependency providers every route reads its services from.

There is exactly one of each service in this process and it is built by the lifespan, so nothing
here constructs anything. Each provider reads `app.state.services` and hands back the instance,
which is what makes the singleton rule from PIPELINE.md section 3 mechanically true rather than a
convention: a route cannot get a second governor or a second settings cache, because there is no
constructor in reach.

Two shapes are exported for each service, a function and an `Annotated` alias. Routes should use
the alias, so the signature reads `settings: SettingsDep` rather than repeating `Depends(...)` at
every call site.

Every service is optional on `AppState`, because a startup that failed part way through leaves the
later ones unset. Asking for one that is missing is a 503 with the documented envelope, never an
AttributeError rendered as a 500.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from expirymanager.api.errors import (
    ApiError,
    CODE_SERVICE_UNAVAILABLE,
    needs_reauth,
    not_authenticated,
    pipeline_stopped,
)
from expirymanager.lifespan import AppState
from expirymanager.paths import Paths
from expirymanager.security.ratelimit import ConcurrencyLimiter, RateLimiter
from expirymanager.security.sessions import SessionManager, SessionRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from expirymanager.db.reader import DuckReader
    from expirymanager.db.writer import DuckWriter
    from expirymanager.security.keys import KeyManager
    from expirymanager.settings_store import SettingsStore

__all__ = [
    "CurrentUser",
    "get_state",
    "get_paths",
    "get_engine",
    "get_settings",
    "get_key_manager",
    "get_duck_reader",
    "get_duck_writer",
    "get_session_manager",
    "get_rate_limiter",
    "get_stream_limiter",
    "get_fyers_client",
    "get_governor",
    "get_token_broker",
    "get_supervisor",
    "get_scheduler",
    "get_session",
    "require_session",
    "current_user",
    "require_broker_token",
    "client_ip",
    "StateDep",
    "PathsDep",
    "EngineDep",
    "SettingsDep",
    "KeyManagerDep",
    "ReaderDep",
    "WriterDep",
    "SessionManagerDep",
    "RateLimiterDep",
    "StreamLimiterDep",
    "FyersClientDep",
    "GovernorDep",
    "TokenBrokerDep",
    "SupervisorDep",
    "SchedulerDep",
    "SessionDep",
    "RequiredSessionDep",
    "CurrentUserDep",
    "ClientIpDep",
]


def _unavailable(what: str) -> ApiError:
    return ApiError(
        503,
        CODE_SERVICE_UNAVAILABLE,
        f"The {what} is not available. The server did not finish starting.",
    )


def get_state(request: Request) -> AppState:
    """The container the lifespan filled."""
    state = getattr(request.app.state, "services", None)
    if state is None:
        raise _unavailable("application state")
    return state


StateDep = Annotated[AppState, Depends(get_state)]


def get_paths(state: StateDep) -> Paths:
    return state.paths


def get_engine(state: StateDep) -> Engine:
    if state.engine is None:
        raise _unavailable("operational database")
    return state.engine


def get_settings(state: StateDep) -> "SettingsStore":
    if state.settings is None:
        raise _unavailable("settings store")
    return state.settings


def get_key_manager(state: StateDep) -> "KeyManager":
    if state.key_manager is None:
        raise _unavailable("key manager")
    return state.key_manager


def get_duck_reader(state: StateDep) -> "DuckReader":
    """Bounded read access to the single DuckDB instance.

    Never open a second connection and never pass `read_only=True`: DuckDB refuses a second
    handle on a file this process already holds read-write.
    """
    reader = state.duck_reader
    if reader is None:
        raise _unavailable("market database")
    return reader


def get_duck_writer(state: StateDep) -> "DuckWriter":
    """The one writer task. Every write goes through `submit`."""
    writer = state.duck_writer
    if writer is None:
        raise _unavailable("market database writer")
    return writer


def get_session_manager(state: StateDep) -> SessionManager:
    if state.session_manager is None:
        raise _unavailable("session manager")
    return state.session_manager


def get_rate_limiter(state: StateDep) -> RateLimiter:
    """The same limiter the middleware uses, so the login route shares its storage."""
    if state.rate_limiter is None:
        raise _unavailable("rate limiter")
    return state.rate_limiter


def get_stream_limiter(state: StateDep) -> ConcurrencyLimiter:
    """The ten concurrent SSE streams per session ceiling."""
    if state.stream_limiter is None:
        raise _unavailable("stream limiter")
    return state.stream_limiter


def get_fyers_client(state: StateDep) -> Any:
    if state.fyers_client is None:
        raise _unavailable("broker client")
    return state.fyers_client


def get_governor(state: StateDep) -> Any:
    if state.governor is None:
        raise _unavailable("request governor")
    return state.governor


def get_token_broker(state: StateDep) -> Any:
    if state.token_broker is None:
        raise _unavailable("token broker")
    return state.token_broker


def get_supervisor(state: StateDep) -> Any:
    """The pipeline supervisor.

    Raises the documented 503 `pipeline_stopped` while the slot is empty, which is what a route
    that starts or controls a job needs today: W11 registers the supervisor and the same routes
    then work with no edit here.
    """
    supervisor = state.supervisor
    if supervisor is None:
        raise pipeline_stopped()
    return supervisor


def get_scheduler(state: StateDep) -> Any:
    """The scheduler service, once W14 registers it."""
    scheduler = state.scheduler
    if scheduler is None:
        raise _unavailable("scheduler")
    return scheduler


def get_session(request: Request) -> SessionRecord | None:
    """The session `SessionMiddleware` already resolved, or None.

    Never a database read: the middleware did it once for this request and slid the idle window
    while it was there.
    """
    return getattr(request.state, "session", None)


def require_session(
    session: Annotated[SessionRecord | None, Depends(get_session)],
) -> SessionRecord:
    if session is None:
        raise not_authenticated()
    return session


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated user, and the session that authenticated them."""

    user_id: str
    username: str
    session: SessionRecord


async def current_user(
    session: Annotated[SessionRecord, Depends(require_session)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> CurrentUser:
    """The signed-in user. The 401 for everything else.

    The username comes from `app_user` rather than from the session row, so that renaming a user
    cannot leave a stale name being served out of a session that was issued before the change.
    """

    def read() -> str | None:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT username FROM app_user WHERE user_id = :user_id"),
                {"user_id": session.user_id},
            ).first()
        return None if row is None else str(row[0])

    username = await run_in_threadpool(read)
    if username is None:
        # The user row was deleted while the session lived. Treated as unauthenticated rather
        # than as a 500, because the session is genuinely no longer valid.
        raise not_authenticated()
    return CurrentUser(user_id=session.user_id, username=username, session=session)


def require_broker_token(broker: Annotated[Any, Depends(get_token_broker)]) -> Any:
    """409 `needs_reauth` when there is no usable Fyers token.

    Any route that is about to spend a Fyers request depends on this, so that the frontend raises
    its reconnect banner instead of the route failing later with a broker error.
    """
    if not broker.has_valid_token():
        raise needs_reauth()
    return broker


def client_ip(request: Request) -> str:
    """The peer address.

    No `X-Forwarded-For`. The app binds loopback with no proxy in front of it, so honouring that
    header would let the browser pick its own rate limit bucket.
    """
    return request.client.host if request.client else "unknown"


PathsDep = Annotated[Paths, Depends(get_paths)]
EngineDep = Annotated[Engine, Depends(get_engine)]
SettingsDep = Annotated["SettingsStore", Depends(get_settings)]
KeyManagerDep = Annotated["KeyManager", Depends(get_key_manager)]
ReaderDep = Annotated["DuckReader", Depends(get_duck_reader)]
WriterDep = Annotated["DuckWriter", Depends(get_duck_writer)]
SessionManagerDep = Annotated[SessionManager, Depends(get_session_manager)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
StreamLimiterDep = Annotated[ConcurrencyLimiter, Depends(get_stream_limiter)]
FyersClientDep = Annotated[Any, Depends(get_fyers_client)]
GovernorDep = Annotated[Any, Depends(get_governor)]
TokenBrokerDep = Annotated[Any, Depends(get_token_broker)]
SupervisorDep = Annotated[Any, Depends(get_supervisor)]
SchedulerDep = Annotated[Any, Depends(get_scheduler)]
SessionDep = Annotated[SessionRecord | None, Depends(get_session)]
RequiredSessionDep = Annotated[SessionRecord, Depends(require_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]
ClientIpDep = Annotated[str, Depends(client_ip)]
