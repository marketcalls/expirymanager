"""`GET /fyers/callback`, mounted at the ROOT of the application.

This module exists as its own file for one reason. The redirect URI registered on the Fyers
dashboard is exactly `https://127.0.0.1:8000/fyers/callback`, Fyers matches it character for
character, and so the scheme, the host, the port and the root path are all fixed. It cannot live
under `/api/v1` and it cannot be renamed. `app.py` includes this router at the root with no
prefix; `api/v1/__init__.py` deliberately does not list it.

Three properties matter here and none of them are optional.

**It is the one CSRF-exempt route.** It arrives as a cross-site top-level GET navigation from
Fyers, so it carries no header this application set and no Origin it recognises. The single-use
`state` value, bound to the session that started the login, is what protects it instead, and that
check is stronger than a token an attacker could not read anyway.

**It answers with a redirect, never with a body.** The URL it was reached at carries `auth_code`
in its query string, which puts it in browser history, in the Referer header of anything the page
would load, and in the access log. A `303 See Other` to a clean path is what gets the browser off
that URL. `Referrer-Policy: no-referrer` and the access log filter are the other two halves, and
both already ship in W07.

**Every failure looks the same from outside.** A caller cannot learn whether a state was missing,
expired, already used or bound to another session, because knowing which would turn this route
into an oracle for guessing the other three.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse

from expirymanager.api.v1.broker import (
    OAuthLoginFailed,
    OAuthStateRejected,
    complete_oauth_login,
)
from expirymanager.brokers.fyers.auth import AuthCodeExchangeFailed, parse_callback_query
from expirymanager.brokers.fyers.tokens import NoCredentialsError

__all__ = [
    "router",
    "CALLBACK_PATH",
    "SUCCESS_REDIRECT",
    "FAILURE_REDIRECT",
    "REASON_STATE_INVALID",
    "REASON_LOGIN_FAILED",
    "REASON_EXCHANGE_FAILED",
    "REASON_NO_CREDENTIALS",
    "REASON_UNEXPECTED",
    "failure_url",
]

log = logging.getLogger(__name__)

router = APIRouter()

# Fixed by the Fyers registration. Changing this string breaks the login and nothing else says so.
CALLBACK_PATH = "/fyers/callback"

# Clean URLs. Neither carries the auth code, which is the whole point of the redirect.
SUCCESS_REDIRECT = "/settings?broker=connected"
FAILURE_REDIRECT = "/settings?broker=failed"

# The reason the settings screen renders. Deliberately coarse: one reason covers every way the
# state check can fail, so the four cases stay indistinguishable.
REASON_STATE_INVALID = "state_invalid"
REASON_LOGIN_FAILED = "login_failed"
REASON_EXCHANGE_FAILED = "exchange_failed"
REASON_NO_CREDENTIALS = "no_credentials"
REASON_UNEXPECTED = "unexpected"

# 303 rather than 302: the browser must issue a GET to the clean URL whatever method it used to
# arrive, and 303 is the status that says so rather than leaving it to the implementation.
SEE_OTHER = 303


def failure_url(reason: str) -> str:
    return f"{FAILURE_REDIRECT}&reason={reason}"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=SEE_OTHER)


@router.get(
    CALLBACK_PATH,
    include_in_schema=False,
    summary="Fyers OAuth redirect target",
)
async def fyers_callback(request: Request) -> RedirectResponse:
    """Finish the login Fyers just redirected back from.

    The query string is parsed off the raw URL rather than declared as FastAPI query parameters on
    purpose. Declared parameters appear in the OpenAPI schema and in 422 validation bodies, and
    `auth_code` must appear in neither. A malformed query is not a validation error here either:
    it is one more way the state check fails, and it redirects like every other failure.
    """
    params = parse_callback_query(request.url.query)
    session = getattr(request.state, "session", None)
    state = getattr(request.app.state, "services", None)

    if state is None:  # pragma: no cover - the app cannot serve a request before startup
        return _redirect(failure_url(REASON_UNEXPECTED))

    # No session is not a separate outcome. The cookies are SameSite=Lax precisely so this
    # navigation carries them, and a callback without one cannot verify the binding, which is the
    # same conclusion as a state that does not match.
    session_id_hash = session.id_hash if session is not None else None

    try:
        record = await complete_oauth_login(
            state, params=params, session_id_hash=session_id_hash
        )
    except OAuthStateRejected:
        # No detail, not even in the log line: the raw state is a credential and the reason is an
        # oracle. The session prefix is enough to find the attempt.
        log.warning(
            "fyers callback rejected",
            extra={"session_prefix": session.id_prefix if session else "none"},
        )
        return _redirect(failure_url(REASON_STATE_INVALID))
    except NoCredentialsError:
        log.warning("fyers callback arrived with no stored credentials")
        return _redirect(failure_url(REASON_NO_CREDENTIALS))
    except OAuthLoginFailed:
        log.warning(
            "fyers returned no auth code",
            extra={"fyers_status": params.status or "none", "fyers_code": params.code},
        )
        return _redirect(failure_url(REASON_LOGIN_FAILED))
    except AuthCodeExchangeFailed as exc:
        classification = exc.classification
        log.warning(
            "fyers auth code exchange failed",
            extra={
                "retry_class": str(classification.retry_class) if classification else "unknown",
                "fyers_code": classification.code if classification else None,
            },
        )
        return _redirect(failure_url(REASON_EXCHANGE_FAILED))

    log.info(
        "fyers login completed through the callback",
        extra={"generation": record.generation, "fingerprint": record.fingerprint[:8]},
    )
    return _redirect(SUCCESS_REDIRECT)
