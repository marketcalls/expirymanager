"""The local passcode routes: setup, login, logout, me, password change. API.md section 1.

This is the front door of a single user application that holds a live broker credential, so the
rules here are deliberately stricter than the size of the app suggests.

**One failure body.** An unknown username and a wrong password return the same 401 with the same
message, and they cost the same wall clock time, because the unknown branch spends one real
Argon2id verification against a dummy hash. A cheap unknown-user path is a username oracle that no
amount of rate limiting hides.

**Every hash runs in the thread pool.** Argon2id at these parameters takes 50 to 100 ms. Running
it on the event loop would stall every other request and make the scheduler stutter once per
login attempt.

**The session id is replaced on login, never reused.** The pre-authentication cookie grants
nothing afterwards, which is what closes session fixation.

Two rate limits guard this file and they are not the same limit. The middleware applies
`login_per_ip`, 20 per 15 minutes, which is what makes brute force useless against an attacker who
rotates the username. The route applies the documented `login_per_username`, 5 per 15 minutes per
(ip, username), by hand, because the middleware cannot read the request body without consuming it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from expirymanager.api.deps import (
    ClientIpDep,
    CurrentUserDep,
    EngineDep,
    RateLimiterDep,
    SessionManagerDep,
)
from expirymanager.api.errors import ApiError, CODE_VALIDATION_ERROR
from expirymanager.api.schemas.auth import (
    CurrentUserResponse,
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    SetupRequest,
    SetupResponse,
)
from expirymanager.security import passwords
from expirymanager.security.sessions import (
    IssuedSession,
    SessionManager,
    clear_session_cookies,
    read_session_cookie,
    set_session_cookies,
)

__all__ = [
    "router",
    "CODE_ALREADY_PROVISIONED",
    "CODE_INVALID_CREDENTIALS",
    "CODE_ACCOUNT_LOCKED",
    "CODE_USERNAME_TAKEN",
    "INVALID_CREDENTIALS_MESSAGE",
]

log = logging.getLogger(__name__)

router = APIRouter()

CODE_ALREADY_PROVISIONED = "already_provisioned"
CODE_INVALID_CREDENTIALS = "invalid_credentials"
CODE_ACCOUNT_LOCKED = "account_locked"
CODE_USERNAME_TAKEN = "username_taken"

# One sentence for both halves of a failed login. It names neither the username nor the password,
# so the body is identical whichever was wrong.
INVALID_CREDENTIALS_MESSAGE = "That username and passcode combination was not accepted."

_INSERT_FIRST_USER = """
INSERT INTO app_user (user_id, username, password_phc, created_at, last_login_at,
                      failed_attempts, locked_until)
SELECT :user_id, :username, :password_phc, :created_at, :created_at, 0, NULL
 WHERE NOT EXISTS (SELECT 1 FROM app_user)
"""

_SELECT_USER_BY_NAME = """
SELECT user_id, username, password_phc, failed_attempts, locked_until
  FROM app_user
 WHERE username = :username
"""

_SELECT_USER_BY_ID = """
SELECT user_id, username, password_phc FROM app_user WHERE user_id = :user_id
"""

_RECORD_FAILURE = """
UPDATE app_user SET failed_attempts = :failed_attempts, locked_until = :locked_until
 WHERE user_id = :user_id
"""

_RECORD_SUCCESS = """
UPDATE app_user
   SET failed_attempts = 0, locked_until = NULL, last_login_at = :now, password_phc = :password_phc
 WHERE user_id = :user_id
"""

_SET_PASSWORD = """
UPDATE app_user SET password_phc = :password_phc, failed_attempts = 0, locked_until = NULL
 WHERE user_id = :user_id
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _invalid_credentials() -> ApiError:
    return ApiError(401, CODE_INVALID_CREDENTIALS, INVALID_CREDENTIALS_MESSAGE)


def _account_locked(retry_after: int) -> ApiError:
    return ApiError(
        423,
        CODE_ACCOUNT_LOCKED,
        "Too many failed attempts. The account is locked for a short period.",
        headers={"Retry-After": str(retry_after)},
    )


def _weak_password(message: str) -> ApiError:
    # 422 rather than a code of its own: this is the documented shape for a body that did not
    # satisfy its constraints, and the message is written by security/passwords.py.
    return ApiError(422, CODE_VALIDATION_ERROR, message)


def _validated(password: str) -> str:
    try:
        passwords.validate_password(password)
    except passwords.PasswordPolicyError as exc:
        raise _weak_password(str(exc)) from exc
    return password


# ---------------------------------------------------------------------------
# Authentication, off the event loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AuthOutcome:
    """What one credential check concluded. Carries no hash and no password."""

    ok: bool
    user_id: str = ""
    username: str = ""
    locked: bool = False
    retry_after: int = 0


def _authenticate(engine: Engine, username: str, password: str) -> _AuthOutcome:
    """Read the user, verify the password and record the result. Synchronous, thread pool only.

    The read, the verification and the counter write are one function so that the unknown-user
    path and the wrong-password path run the same number of Argon2id verifications. Splitting them
    across `await` boundaries is how the two branches drift apart in cost.
    """
    with engine.connect() as connection:
        row = connection.execute(text(_SELECT_USER_BY_NAME), {"username": username}).first()

    if row is None:
        # One real verification against a throwaway hash, so an unknown username costs what a
        # wrong password costs.
        passwords.verify_dummy()
        return _AuthOutcome(ok=False)

    user_id, stored_username, password_phc, failed_attempts, locked_until = row
    now = datetime.now(UTC)

    if passwords.is_locked(locked_until, now=now):
        return _AuthOutcome(
            ok=False,
            locked=True,
            retry_after=passwords.lock_retry_after(locked_until, now=now),
        )

    result = passwords.verify_password(password_phc, password)

    if not result.ok:
        attempts, new_lock = passwords.record_failure(int(failed_attempts or 0), now=now)
        with engine.begin() as connection:
            connection.execute(
                text(_RECORD_FAILURE),
                {
                    "failed_attempts": attempts,
                    "locked_until": new_lock.isoformat() if new_lock else None,
                    "user_id": user_id,
                },
            )
        # The attempt that trips the lock still answers 401, identically to any other wrong
        # password. Answering 423 on that attempt would confirm the username exists at the exact
        # moment an attacker learns the most from it. The 423 is for the attempts after it, which
        # is where the Retry-After is actually useful to the legitimate user.
        return _AuthOutcome(ok=False)

    with engine.begin() as connection:
        connection.execute(
            text(_RECORD_SUCCESS),
            {
                "now": now.isoformat(),
                # Written back only when the stored hash predates the current Argon2id
                # parameters. Dropping this is how an installation stays on weak parameters for
                # the life of the account.
                "password_phc": result.upgraded_phc or password_phc,
                "user_id": user_id,
            },
        )
    return _AuthOutcome(ok=True, user_id=str(user_id), username=str(stored_username))


def _issue_session(
    manager: SessionManager,
    *,
    user_id: str,
    request: Request,
    client_ip: str,
    revoke_presented: bool = True,
) -> IssuedSession:
    """Replace whatever session the browser presented with a fresh one for this user.

    Deliberately not `SessionManager.rotate`: rotate carries the old row's `user_id` forward, which
    is right for a password change and wrong for a login, where the browser may be presenting a
    session belonging to somebody else. Revoking the presented id and creating a new one is the
    same fixation defence and is correct in both cases.
    """
    if revoke_presented:
        manager.revoke(read_session_cookie(request))
    return manager.create(
        user_id,
        user_agent=request.headers.get("user-agent"),
        client_ip=client_ip,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/auth/setup",
    response_model=SetupResponse,
    summary="Create the first user account",
)
async def setup(
    body: SetupRequest,
    request: Request,
    response: Response,
    engine: EngineDep,
    sessions: SessionManagerDep,
    client_ip: ClientIpDep,
) -> SetupResponse:
    """Create the one account, and sign the browser in.

    Reachable only while `app_user` is empty. The insert carries its own `WHERE NOT EXISTS`, so
    two concurrent setup requests cannot both create an account: the second writes no row and is
    answered 409 from the row count rather than from a check that raced.
    """
    _validated(body.password)
    password_phc = await run_in_threadpool(passwords.hash_password, body.password)
    user_id = str(uuid.uuid4())

    def insert() -> bool:
        with engine.begin() as connection:
            created = connection.execute(
                text(_INSERT_FIRST_USER),
                {
                    "user_id": user_id,
                    "username": body.username,
                    "password_phc": password_phc,
                    "created_at": _now_iso(),
                },
            ).rowcount
        return bool(created)

    if not await run_in_threadpool(insert):
        raise ApiError(
            409,
            CODE_ALREADY_PROVISIONED,
            "This instance already has an account. Sign in instead.",
        )

    issued = await run_in_threadpool(
        _issue_session,
        sessions,
        user_id=user_id,
        request=request,
        client_ip=client_ip,
    )
    set_session_cookies(response, issued)
    log.info("first user account created", extra={"session_prefix": issued.record.id_prefix})
    return SetupResponse(user_id=user_id)


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    summary="Sign in with the local passcode",
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    engine: EngineDep,
    sessions: SessionManagerDep,
    limiter: RateLimiterDep,
    client_ip: ClientIpDep,
) -> LoginResponse:
    """Verify the passcode and issue a session.

    The per-(ip, username) limit is enforced here rather than in the middleware because the
    username is in the body, and reading the body in an ASGI middleware consumes the receive
    channel the route is about to read from.
    """
    limiter.enforce_login(client_ip, body.username)

    outcome = await run_in_threadpool(_authenticate, engine, body.username, body.password)

    if outcome.locked:
        raise _account_locked(outcome.retry_after)
    if not outcome.ok:
        # The username is not logged. It is user input, and a failed login is exactly the record
        # an operator reads out loud while debugging.
        log.warning("login rejected", extra={"client_ip": client_ip})
        raise _invalid_credentials()

    issued = await run_in_threadpool(
        _issue_session,
        sessions,
        user_id=outcome.user_id,
        request=request,
        client_ip=client_ip,
    )
    set_session_cookies(response, issued)
    log.info("login accepted", extra={"session_prefix": issued.record.id_prefix})
    return LoginResponse(user_id=outcome.user_id, username=outcome.username)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out and delete the session",
)
async def logout(request: Request, sessions: SessionManagerDep) -> Response:
    """Delete the session row and clear both cookies.

    No session is not an error. Logging out twice, or from a browser whose row was already pruned,
    must end in the same place: no cookies and no row.
    """
    raw_id = read_session_cookie(request)
    if raw_id:
        await run_in_threadpool(sessions.revoke, raw_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response)
    return response


@router.get(
    "/auth/me",
    response_model=CurrentUserResponse,
    summary="The signed-in user",
)
async def me(user: CurrentUserDep) -> CurrentUserResponse:
    """Who this browser is. The 401 from here is what the SPA turns into a redirect to /login."""
    return CurrentUserResponse(
        user_id=user.user_id,
        username=user.username,
        session_expires_at=user.session.expires_at.isoformat(),
    )


@router.post(
    "/auth/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the local passcode",
)
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    engine: EngineDep,
    sessions: SessionManagerDep,
    user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Response:
    """Replace the passcode, drop every session for the user, and re-issue one for this browser.

    Every other session is deleted because a password change is the action taken after a suspected
    compromise. A change that leaves the attacker's session alive achieves nothing.
    """
    _validated(body.new_password)

    def verify_current() -> bool:
        with engine.connect() as connection:
            row = connection.execute(
                text(_SELECT_USER_BY_ID), {"user_id": user.user_id}
            ).first()
        if row is None:
            return False
        return bool(passwords.verify_password(row[2], body.current_password).ok)

    if not await run_in_threadpool(verify_current):
        raise _invalid_credentials()

    password_phc = await run_in_threadpool(passwords.hash_password, body.new_password)

    def store() -> None:
        with engine.begin() as connection:
            connection.execute(
                text(_SET_PASSWORD),
                {"password_phc": password_phc, "user_id": user.user_id},
            )

    await run_in_threadpool(store)
    await run_in_threadpool(sessions.revoke_all, user.user_id)

    issued = await run_in_threadpool(
        _issue_session,
        sessions,
        user_id=user.user_id,
        request=request,
        client_ip=client_ip,
        revoke_presented=False,
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    set_session_cookies(response, issued)
    log.info("passcode changed", extra={"session_prefix": issued.record.id_prefix})
    return response
