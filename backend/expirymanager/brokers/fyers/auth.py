"""The Fyers OAuth login flow, end to end.

Three steps and one hash:

  1  Send the user to /api/v3/generate-authcode with client_id, redirect_uri, response_type=code
     and an unguessable state.
  2  Fyers redirects to https://127.0.0.1:8000/fyers/callback with auth_code and the same state.
  3  POST /api/v3/validate-authcode with grant_type=authorization_code, the appIdHash and the code.

`appIdHash` is the lowercase hex SHA-256 of the literal string "app_id:app_secret", where the app
id is the api key and the app secret is the api secret. The colon is part of the hashed string, not
a separator this code invents: hashing the concatenation without it produces a different digest
and a -352 that reads like a wrong app id.

There is no pin field and no refresh path. SEBI discontinued the refresh token flow from
1 April 2026, so the only way back from a rejected token is another login.

The redirect URI is fixed at https://127.0.0.1:8000/fyers/callback. Fyers matches it character for
character against the registration, so the scheme, the host, the port and the root path are all
load bearing and none of them are configurable.
"""

from __future__ import annotations

from expirymanager import runtime_scheme

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers.client import FyersClient, encode_query
from expirymanager.brokers.fyers.errors import Classification, RetryClass
from expirymanager.brokers.fyers.tokens import (
    FyersCredentials,
    TokenBroker,
    TokenRecord,
    decode_jwt_expiry,
    token_fingerprint,
)

__all__ = [
    "DEFAULT_REDIRECT_URI",
    "RESPONSE_TYPE",
    "GRANT_TYPE_AUTHORIZATION_CODE",
    "STATE_BYTES",
    "STATE_TTL_SECONDS",
    "AuthError",
    "AuthCodeExchangeFailed",
    "AuthorizeRequest",
    "CallbackParams",
    "TokenExchange",
    "app_id_hash",
    "new_state",
    "state_digest",
    "state_matches",
    "build_authorize_url",
    "start_login",
    "parse_callback_query",
    "parse_redirected_url",
    "exchange_auth_code",
    "complete_login",
]

log = logging.getLogger(__name__)

# Registered on the Fyers dashboard, matched character for character. Not a setting.
# Follows the scheme the server serves. Only a fallback: the redirect URI the user registered
# is stored with the credential and is what is actually sent to Fyers.
DEFAULT_REDIRECT_URI = runtime_scheme.callback_url()

RESPONSE_TYPE = "code"
GRANT_TYPE_AUTHORIZATION_CODE = "authorization_code"

# 32 bytes through token_urlsafe is 43 characters of url safe base64, which is 256 bits of entropy.
STATE_BYTES = 32
STATE_TTL_SECONDS = 600


class AuthError(Exception):
    """Base class for login failures."""


class AuthCodeExchangeFailed(AuthError):
    """validate-authcode did not return an access token.

    Carries the classification so the caller can tell a bad auth code, which is the user's problem
    and needs a fresh login, from a transient broker fault, which is worth one more attempt.
    """

    def __init__(self, message: str, *, classification: Classification | None = None) -> None:
        super().__init__(message)
        self.classification = classification


def app_id_hash(app_id: str, app_secret: str) -> str:
    """SHA-256 of the literal string "app_id:app_secret", lowercase hex.

    The docs call this appIdHash and give SHA-256 of app_id:app_secret as the definition. The
    colon is inside the hashed string. This is the single value that authenticates the exchange,
    so it is computed here and nowhere else.
    """
    if not app_id or not app_secret:
        raise AuthError("both the app id and the app secret are required")
    return hashlib.sha256(f"{app_id}:{app_secret}".encode("utf-8")).hexdigest()


def new_state() -> str:
    """An unguessable single use state value.

    It is what binds the callback to the browser session that started the login. Without it, any
    page that can make the browser hit the callback URL can complete a login on the user's behalf.
    """
    return secrets.token_urlsafe(STATE_BYTES)


def state_digest(state: str) -> bytes:
    """sha256 of the raw state. Only the digest is stored, never the value itself."""
    return hashlib.sha256(state.encode("utf-8")).digest()


def state_matches(stored_digest: bytes, presented_state: str) -> bool:
    """Constant time comparison, because a timing oracle on the state defeats its purpose."""
    return hmac.compare_digest(stored_digest, state_digest(presented_state))


@dataclass(frozen=True, slots=True)
class AuthorizeRequest:
    """What step one produced: the URL to open and the state to remember."""

    # The URL embeds the state, so it is as sensitive as the state itself and stays out of repr.
    authorize_url: str = field(repr=False)
    state: str = field(repr=False)
    state_hash: bytes = field(repr=False)
    expires_at: datetime
    credential_id: str

    @property
    def state_expires_at(self) -> str:
        return self.expires_at.isoformat()


@dataclass(frozen=True, slots=True)
class CallbackParams:
    """What Fyers put on the redirect. `auth_code` is a credential and stays out of repr."""

    auth_code: str | None = field(default=None, repr=False)
    state: str | None = field(default=None, repr=False)
    status: str | None = None
    code: int | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class TokenExchange:
    """The result of validate-authcode. Never logged, never returned across the API boundary."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", token_fingerprint(self.access_token))


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    base_url: str = ep.API_BASE_URL,
) -> str:
    """Assemble the step one URL.

    Parameter order follows the documented sample so the URL is comparable by eye against the
    docs, and every value is percent encoded, which matters most for the redirect URI: an
    unescaped `://` in a query value is what turns a working registration into a mismatch.
    """
    if not client_id:
        raise AuthError("the client id is required")
    if not redirect_uri:
        raise AuthError("the redirect uri is required")
    query = encode_query(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": RESPONSE_TYPE,
            "state": state,
        },
        safe="",
    )
    return f"{base_url.rstrip('/')}{ep.AUTHORIZE.path}?{query}"


def start_login(
    credentials: FyersCredentials,
    *,
    now: datetime | None = None,
    base_url: str = ep.API_BASE_URL,
) -> AuthorizeRequest:
    """Step one. Mints the state and returns everything the caller must persist.

    The caller writes `state_hash` into `oauth_state` bound to the current session, and returns
    only `authorize_url` and `state_expires_at` to the browser.
    """
    issued_at = now or datetime.now(UTC)
    state = new_state()
    return AuthorizeRequest(
        authorize_url=build_authorize_url(
            client_id=credentials.app_id,
            redirect_uri=credentials.redirect_uri,
            state=state,
            base_url=base_url,
        ),
        state=state,
        state_hash=state_digest(state),
        expires_at=issued_at + timedelta(seconds=STATE_TTL_SECONDS),
        credential_id=credentials.credential_id,
    )


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    value = values[0]
    return value or None


def parse_callback_query(query: str) -> CallbackParams:
    """Parse the callback query string into its four documented fields."""
    parsed = parse_qs(query, keep_blank_values=True)
    raw_code = _first(parsed.get("code"))
    code: int | None
    try:
        code = int(raw_code) if raw_code is not None else None
    except ValueError:
        code = None
    return CallbackParams(
        auth_code=_first(parsed.get("auth_code")),
        state=_first(parsed.get("state")),
        status=_first(parsed.get("s")),
        code=code,
        message=_first(parsed.get("message")) or "",
    )


def parse_redirected_url(url: str) -> CallbackParams:
    """The manual fallback: the user pastes the whole URL they landed on.

    Needed because the callback rides on a self signed certificate, and a user who declines the
    browser warning never reaches the automatic handler.
    """
    split = urlsplit(url.strip())
    if not split.query:
        raise AuthError("that url carries no query string, so it holds no auth code")
    return parse_callback_query(split.query)


async def exchange_auth_code(
    client: FyersClient,
    *,
    app_id: str,
    app_secret: str,
    auth_code: str,
) -> TokenExchange:
    """Step three. Trade the auth code for an access token.

    Sends exactly the three documented fields. There is no pin field: the flow a pin unlocked was
    discontinued from 1 April 2026, and sending an unexpected field to an auth endpoint is a good
    way to earn a -50.

    This call deliberately bypasses the outbound limiter. If it did not, a pipeline parked in
    paused_auth could never be unparked, because the limiter blocks while the pipeline is paused
    and a successful login is the only thing that can unpause it.
    """
    if not auth_code:
        raise AuthError("the auth code is required")
    response = await client.request_standard(
        ep.VALIDATE_AUTHCODE,
        json_body={
            "grant_type": GRANT_TYPE_AUTHORIZATION_CODE,
            "appIdHash": app_id_hash(app_id, app_secret),
            "code": auth_code,
        },
    )
    access_token = response.payload.get("access_token")
    if not response.ok or not isinstance(access_token, str) or not access_token:
        classification = response.classification()
        if classification is None:
            classification = Classification(
                retry_class=RetryClass.AUTH_FATAL,
                reason="validate-authcode returned no access token",
                http_status=response.http_status,
            )
        # The broker's message is kept for the log record and dropped before the API boundary: a
        # verbatim upstream error is how an oracle leaks which half of a login attempt was wrong.
        log.warning(
            "fyers auth code exchange failed",
            extra={
                "http_status": response.http_status,
                "fyers_code": classification.code,
                "retry_class": str(classification.retry_class),
            },
        )
        raise AuthCodeExchangeFailed(
            "the broker did not return an access token", classification=classification
        )

    refresh_token = response.payload.get("refresh_token")
    return TokenExchange(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        expires_at=decode_jwt_expiry(access_token),
    )


async def complete_login(
    client: FyersClient,
    broker: TokenBroker,
    *,
    auth_code: str,
    credentials: FyersCredentials | None = None,
) -> TokenRecord:
    """Steps three and four: exchange the code, then persist the token encrypted.

    Returns the `TokenRecord`, which carries the fingerprint and the expiry and deliberately does
    not carry the token. The caller opens the auth gate by way of the broker, moves blocked_auth
    jobs back to queued and redirects to a clean URL.
    """
    resolved = credentials or broker.credentials()
    exchange = await exchange_auth_code(
        client,
        app_id=resolved.app_id,
        app_secret=resolved.app_secret,
        auth_code=auth_code,
    )
    record = await broker.store_login(
        access_token=exchange.access_token,
        refresh_token=exchange.refresh_token,
        credential_id=resolved.credential_id,
    )
    log.info(
        "fyers login completed",
        extra={"generation": record.generation, "fingerprint": record.fingerprint[:8]},
    )
    return record
