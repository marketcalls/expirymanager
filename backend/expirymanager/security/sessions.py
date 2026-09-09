"""Opaque server-side sessions in SQLite.

Not JWT. A JWT cannot be revoked without a server-side denylist, at which point there is
server-side state anyway and nothing has been gained but a signing key to protect and a family of
algorithm confusion bugs. SECURITY.md section 5 states this as a decision, so it is written down
here next to the code that would otherwise be tempted to change it.

The raw session id never touches the database. `secrets.token_urlsafe(32)` is 256 bits of entropy;
only its sha256 digest is stored, so a stolen `config.sqlite3` yields no usable cookie.

Two independent lifetimes, both enforced on every lookup:

- an 8 hour idle window that slides on use, written back at most once a minute so a busy tab does
  not turn every GET into a WAL write;
- a 7 day absolute window that never extends.

The cookie attributes carry one constraint that must not be relaxed. `SameSite=Lax`, never
`Strict`: the Fyers OAuth redirect back to `/fyers/callback` is a cross-site top-level GET
navigation and `Strict` withholds cookies on exactly that navigation, so the callback would arrive
with no session and could not verify the state-to-session binding. The constant carries the reason
because changing it breaks OAuth silently rather than loudly.
"""

from __future__ import annotations

from expirymanager import runtime_scheme

import hashlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session as OrmSession

from expirymanager.db.models import AppSession

__all__ = [
    "SESSION_COOKIE_NAME",
    "CSRF_COOKIE_NAME",
    "COOKIE_PATH",
    "COOKIE_SAMESITE",
    "COOKIE_SECURE",
    "IDLE_TIMEOUT",
    "ABSOLUTE_TIMEOUT",
    "LAST_SEEN_WRITE_INTERVAL",
    "SESSION_ID_BYTES",
    "SessionRecord",
    "IssuedSession",
    "SessionManager",
    "SessionMiddleware",
    "new_session_id",
    "new_csrf_token",
    "hash_session_id",
    "set_session_cookies",
    "clear_session_cookies",
    "read_session_cookie",
]

log = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "em_session"
CSRF_COOKIE_NAME = "em_csrf"
COOKIE_PATH = "/"

# Never `strict`. See the module docstring: `strict` withholds the cookie on the Fyers OAuth
# redirect, which is the one cross-site top-level navigation this app depends on.
COOKIE_SAMESITE = "lax"

# Follows the scheme the server is actually serving on, which the registered Fyers redirect URI
# dictates. It must NOT be hardcoded true: a `Secure` cookie is never sent back over http, so
# asserting it while serving http does not harden anything, it breaks login silently. The browser
# accepts the Set-Cookie, declines to send it, and every later request looks unauthenticated with
# no error to trace. Resolved per call rather than at import, because a default argument is bound
# once at function definition and would freeze whatever the value was at import time.
def cookie_secure() -> bool:
    return runtime_scheme.is_https()

# No `Domain=` attribute is ever set, so the cookies are host-only, which is what a loopback app
# wants. The `__Host-` prefix is deliberately not used: it would only re-assert Secure, Path=/ and
# the absence of Domain, all three of which are set explicitly below, while forcing the frontend to
# know two names for the same cookie depending on how it was started.

SESSION_ID_BYTES = 32
CSRF_TOKEN_BYTES = 32

IDLE_TIMEOUT = timedelta(hours=8)
ABSOLUTE_TIMEOUT = timedelta(days=7)

# `last_seen_at` is a write, and this store runs in WAL. Writing it on every request turns a page
# of read-only polling into a page of transactions. One minute of drift on an eight hour window is
# not observable to anyone.
LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=1)

Clock = Callable[[], datetime]


def new_session_id() -> str:
    """A fresh opaque session id. 256 bits, url safe, never stored."""
    return secrets.token_urlsafe(SESSION_ID_BYTES)


def new_csrf_token() -> str:
    """A fresh synchronizer token for the session row and the readable cookie."""
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def hash_session_id(raw_id: str) -> bytes:
    """The only form of the session id that reaches disk."""
    return hashlib.sha256(raw_id.encode("utf-8")).digest()


def _iso(moment: datetime) -> str:
    """Fixed width UTC ISO-8601.

    Fixed width matters: the pruning query compares these values as TEXT, and a format whose length
    varies with the value would sort wrongly the moment one row was written by a different path.
    """
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One row of `session`, with its timestamps parsed. The raw id is not part of this."""

    id_hash: bytes
    user_id: str
    csrf_token: str
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    user_agent: str | None = None
    client_ip: str | None = None

    @property
    def expires_at(self) -> datetime:
        """Whichever boundary comes first. This is what `GET /auth/me` reports."""
        return min(self.idle_expires_at, self.absolute_expires_at)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.idle_expires_at or now >= self.absolute_expires_at

    @property
    def id_prefix(self) -> str:
        """The first 8 hex characters of the stored hash. Safe to log; the raw id is not."""
        return self.id_hash.hex()[:8]


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A newly created session. The raw id exists only here and in the Set-Cookie header."""

    raw_id: str
    record: SessionRecord

    @property
    def csrf_token(self) -> str:
        return self.record.csrf_token

    @property
    def user_id(self) -> str:
        return self.record.user_id

    @property
    def expires_at(self) -> datetime:
        return self.record.expires_at


class SessionManager:
    """Issue, resolve, rotate and revoke sessions against the operational SQLite store.

    Every call is synchronous SQLAlchemy. Routes reach it through `run_in_threadpool`, the same way
    they reach the rest of `db/`.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        idle_timeout: timedelta = IDLE_TIMEOUT,
        absolute_timeout: timedelta = ABSOLUTE_TIMEOUT,
        last_seen_interval: timedelta = LAST_SEEN_WRITE_INTERVAL,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._idle_timeout = idle_timeout
        self._absolute_timeout = absolute_timeout
        self._last_seen_interval = last_seen_interval
        # Injected so the expiry boundaries can be tested at the second rather than after 8 hours.
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def idle_timeout(self) -> timedelta:
        return self._idle_timeout

    @property
    def absolute_timeout(self) -> timedelta:
        return self._absolute_timeout

    def now(self) -> datetime:
        return self._clock()

    def create(
        self,
        user_id: str,
        *,
        user_agent: str | None = None,
        client_ip: str | None = None,
    ) -> IssuedSession:
        """Mint a session for a user who has just proved who they are."""
        raw_id = new_session_id()
        now = self.now()
        record = SessionRecord(
            id_hash=hash_session_id(raw_id),
            user_id=user_id,
            csrf_token=new_csrf_token(),
            created_at=now,
            last_seen_at=now,
            idle_expires_at=now + self._idle_timeout,
            absolute_expires_at=now + self._absolute_timeout,
            user_agent=_truncate(user_agent),
            client_ip=client_ip,
        )
        with OrmSession(self._engine) as db:
            db.add(
                AppSession(
                    id_hash=record.id_hash,
                    user_id=record.user_id,
                    csrf_token=record.csrf_token,
                    created_at=_iso(record.created_at),
                    last_seen_at=_iso(record.last_seen_at),
                    idle_expires_at=_iso(record.idle_expires_at),
                    absolute_expires_at=_iso(record.absolute_expires_at),
                    user_agent=record.user_agent,
                    client_ip=record.client_ip,
                )
            )
            db.commit()
        log.info("session issued", extra={"session_prefix": record.id_prefix})
        return IssuedSession(raw_id=raw_id, record=record)

    def lookup(self, raw_id: str | None, *, slide: bool = True) -> SessionRecord | None:
        """Resolve a cookie value to a live session, or None.

        An expired row is deleted rather than merely ignored, so a stale cookie cannot keep a dead
        row alive until the hourly prune runs.
        """
        if not raw_id:
            return None
        return self.lookup_by_hash(hash_session_id(raw_id), slide=slide)

    def lookup_by_hash(self, id_hash: bytes, *, slide: bool = True) -> SessionRecord | None:
        now = self.now()
        with OrmSession(self._engine) as db:
            row = db.get(AppSession, id_hash)
            if row is None:
                return None
            record = _to_record(row)
            if record.is_expired(now):
                db.delete(row)
                db.commit()
                return None
            if not slide:
                return record

            # The absolute window never extends, so the idle window is clamped to it. Without the
            # clamp a session used continuously for a week would report an idle expiry beyond the
            # absolute one and `expires_at` would name a moment the session cannot reach.
            idle_expires_at = min(now + self._idle_timeout, record.absolute_expires_at)
            if now - record.last_seen_at < self._last_seen_interval:
                return record

            row.last_seen_at = _iso(now)
            row.idle_expires_at = _iso(idle_expires_at)
            db.commit()
            return SessionRecord(
                id_hash=record.id_hash,
                user_id=record.user_id,
                csrf_token=record.csrf_token,
                created_at=record.created_at,
                last_seen_at=now,
                idle_expires_at=idle_expires_at,
                absolute_expires_at=record.absolute_expires_at,
                user_agent=record.user_agent,
                client_ip=record.client_ip,
            )

    def rotate(
        self,
        raw_id: str | None,
        *,
        user_agent: str | None = None,
        client_ip: str | None = None,
    ) -> IssuedSession | None:
        """Replace a session id with a fresh one for the same user, atomically.

        Called on successful login and on password change. Both windows restart: the point of
        rotation is that the pre-authentication identifier grants nothing afterwards, and carrying
        the old absolute deadline forward would let a fixated cookie shorten the new session.
        Returns None when the old id no longer resolves, which the caller treats as a plain login.
        """
        record = self.lookup(raw_id, slide=False)
        if record is None:
            return None
        issued = self.create(
            record.user_id,
            user_agent=user_agent if user_agent is not None else record.user_agent,
            client_ip=client_ip if client_ip is not None else record.client_ip,
        )
        self.revoke_by_hash(record.id_hash)
        return issued

    def revoke(self, raw_id: str | None) -> bool:
        """Delete one session. Logout."""
        if not raw_id:
            return False
        return self.revoke_by_hash(hash_session_id(raw_id))

    def revoke_by_hash(self, id_hash: bytes) -> bool:
        with OrmSession(self._engine) as db:
            deleted = db.execute(
                delete(AppSession).where(AppSession.id_hash == id_hash)
            ).rowcount
            db.commit()
        return bool(deleted)

    def revoke_all(self, user_id: str, *, keep_id_hash: bytes | None = None) -> int:
        """Delete every session for a user. Password change, and the 03:00 auto-logout path.

        `keep_id_hash` exists so a password change can drop every other session while leaving the
        caller's own freshly rotated one in place.
        """
        statement = delete(AppSession).where(AppSession.user_id == user_id)
        if keep_id_hash is not None:
            statement = statement.where(AppSession.id_hash != keep_id_hash)
        with OrmSession(self._engine) as db:
            deleted = db.execute(statement).rowcount
            db.commit()
        if deleted:
            log.info("sessions revoked", extra={"revoked_count": deleted})
        return int(deleted)

    def prune_expired(self) -> int:
        """Delete every session past either boundary. The hourly scheduled job calls this."""
        now = _iso(self.now())
        with OrmSession(self._engine) as db:
            deleted = db.execute(
                delete(AppSession).where(
                    (AppSession.absolute_expires_at <= now)
                    | (AppSession.idle_expires_at <= now)
                )
            ).rowcount
            db.commit()
        return int(deleted)

    def count_for_user(self, user_id: str) -> int:
        with OrmSession(self._engine) as db:
            return len(
                db.execute(
                    select(AppSession.id_hash).where(AppSession.user_id == user_id)
                ).all()
            )

    def refresh_csrf(self, id_hash: bytes) -> str | None:
        """Issue a new synchronizer token on the existing session row.

        The extension point for a future explicit token refresh. The token is rotated with the
        session id on login, so nothing calls this yet.
        """
        token = new_csrf_token()
        with OrmSession(self._engine) as db:
            updated = db.execute(
                update(AppSession)
                .where(AppSession.id_hash == id_hash)
                .values(csrf_token=token)
            ).rowcount
            db.commit()
        return token if updated else None


def _truncate(value: str | None, limit: int = 256) -> str | None:
    if value is None:
        return None
    return value[:limit]


def _to_record(row: AppSession) -> SessionRecord:
    return SessionRecord(
        id_hash=bytes(row.id_hash),
        user_id=row.user_id,
        csrf_token=row.csrf_token,
        created_at=_parse(row.created_at),
        last_seen_at=_parse(row.last_seen_at),
        idle_expires_at=_parse(row.idle_expires_at),
        absolute_expires_at=_parse(row.absolute_expires_at),
        user_agent=row.user_agent,
        client_ip=row.client_ip,
    )


def read_session_cookie(request) -> str | None:  # type: ignore[no-untyped-def]
    """The raw session id from the request cookies, or None."""
    return request.cookies.get(SESSION_COOKIE_NAME)


def set_session_cookies(
    response,  # type: ignore[no-untyped-def]
    issued: IssuedSession,
    *,
    secure: bool | None = None,
) -> None:
    """Write both cookies for a freshly issued session.

    `em_session` is HttpOnly so script cannot read it. `em_csrf` deliberately is not, because the
    frontend fetch wrapper reads it and echoes it in `X-CSRF-Token`. That asymmetry is the whole
    mechanism: an attacker on another origin can cause a request but cannot read the cookie to
    populate the header.
    """
    if secure is None:
        secure = cookie_secure()
    max_age = int(ABSOLUTE_TIMEOUT.total_seconds())
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.raw_id,
        max_age=max_age,
        path=COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=COOKIE_SAMESITE,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issued.csrf_token,
        max_age=max_age,
        path=COOKIE_PATH,
        secure=secure,
        httponly=False,
        samesite=COOKIE_SAMESITE,
    )


def clear_session_cookies(
    response,  # type: ignore[no-untyped-def]
    *,
    secure: bool | None = None,
) -> None:
    """Expire both cookies. The attributes must match the ones they were set with or the browser
    keeps the originals."""
    if secure is None:
        secure = cookie_secure()
    for name, httponly in ((SESSION_COOKIE_NAME, True), (CSRF_COOKIE_NAME, False)):
        response.delete_cookie(
            name,
            path=COOKIE_PATH,
            secure=secure,
            httponly=httponly,
            samesite=COOKIE_SAMESITE,
        )


class SessionMiddleware:
    """Resolve the session cookie once per request into `request.state`.

    Pure ASGI rather than `BaseHTTPMiddleware`, because `BaseHTTPMiddleware` wraps the response in
    an anyio task group and that breaks the SSE stream at `GET /events/stream`.

    It resolves and slides; it never issues and never rejects. Issuing belongs to the login route
    and rejecting belongs to the CSRF middleware and to the route dependencies, which is why an
    unauthenticated request passes straight through with `state.session` set to None.
    """

    def __init__(self, app, manager: SessionManager, *, runner=None) -> None:  # type: ignore[no-untyped-def]
        self._app = app
        self._manager = manager
        # The lookup is synchronous SQLite. `runner` is the awaitable offload, injected so a test
        # can run it inline. Defaults to Starlette's thread pool.
        self._runner = runner

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        from starlette.requests import Request

        request = Request(scope)
        state = scope.setdefault("state", {})
        raw_id = read_session_cookie(request)

        record: SessionRecord | None = None
        if raw_id:
            record = await self._lookup(raw_id)

        state["session"] = record
        state["user_id"] = record.user_id if record is not None else None
        await self._app(scope, receive, send)

    async def _lookup(self, raw_id: str) -> SessionRecord | None:
        if self._runner is not None:
            return await self._runner(self._manager.lookup, raw_id)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(self._manager.lookup, raw_id)
