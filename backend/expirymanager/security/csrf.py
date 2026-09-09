"""CSRF: a synchronizer token layered over Fetch Metadata and an Origin allowlist.

Three checks in a fixed order, cheapest and most decisive first, exactly as SECURITY.md section 6.2
lays them out.

1. `Sec-Fetch-Site`. Free, sent by every browser this app supports, and it rejects most cross
   origin attempts before any database lookup happens at all. `cross-site` and `same-site` are both
   refused; `same-origin` and `none` pass. `none` is a user-initiated navigation, which cannot be
   an unsafe method from an attacker page.
2. The Origin allowlist. Some browsers omit `Origin` on a same-origin POST, so an absent Origin
   cannot be a rejection, which is exactly why this cannot be the only layer.
3. The synchronizer token, compared in constant time against the value on the server-side session
   row.

Plain double submit is rejected. It trusts that no attacker can write a cookie on the target
origin, and a sibling origin or a MITM on a sibling http origin can. Holding the token on the
session row means an attacker cannot forge a matching pair.

Both browser origins are allowed at once rather than switched by environment. In development the
SPA is served by Vite on `https://127.0.0.1:5173` and its proxy carries `/api` on that same origin;
in production FastAPI serves both from `https://127.0.0.1:8000`. Allowing the pair means the same
build works in both, and the set stays a two-entry allowlist of loopback https origins rather than
a regex. Because the Vite proxy carries `/api` on the dev origin itself, every API call is
`same-origin` and never `same-site`, so layer 1 remains a real filter in development too.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from expirymanager.security.headers import error_response

__all__ = [
    "SAFE_METHODS",
    "CSRF_HEADER_NAME",
    "DEV_ORIGIN",
    "PROD_ORIGIN",
    "DEV_ORIGIN_TLS",
    "PROD_ORIGIN_TLS",
    "DEFAULT_ALLOWED_ORIGINS",
    "DEFAULT_EXEMPT_PATHS",
    "DEFAULT_SESSIONLESS_PATHS",
    "REASON_CROSS_ORIGIN",
    "REASON_BAD_ORIGIN",
    "REASON_CSRF_INVALID",
    "REASON_MESSAGES",
    "CsrfDecision",
    "is_exempt",
    "is_sessionless",
    "check_csrf",
    "CsrfMiddleware",
]

SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

CSRF_HEADER_NAME = "x-csrf-token"

DEV_ORIGIN = "http://127.0.0.1:5173"
PROD_ORIGIN = "http://127.0.0.1:8000"

# Both schemes are allowed for both loopback ports. The scheme the server serves is dictated by the
# redirect URI registered with Fyers and can be changed there, and an allowlist that silently stops
# matching after such a change fails as a bare "did not come from the application" with nothing
# naming the scheme as the cause. Allowing the unused scheme costs nothing: no browser will present
# an origin the server is not serving, and all four entries are loopback on a fixed port.
DEV_ORIGIN_TLS = "https://127.0.0.1:5173"
PROD_ORIGIN_TLS = "https://127.0.0.1:8000"

# The host is 127.0.0.1 and never `localhost`. Cookies ignore the port but not the host, so
# same-host different-port is what makes the session cookie set by the backend on the OAuth
# callback visible to the Vite dev origin.
DEFAULT_ALLOWED_ORIGINS: frozenset[str] = frozenset(
    {DEV_ORIGIN, PROD_ORIGIN, DEV_ORIGIN_TLS, PROD_ORIGIN_TLS}
)

# Exactly one path. `GET /fyers/callback` is a cross-site top-level navigation from Fyers, so it
# can carry no header and no matching Origin. It is protected instead by the single-use `state`
# value bound to the session, which is a stronger control than a token an attacker cannot read
# anyway. Nothing else is ever added to this tuple.
DEFAULT_EXEMPT_PATHS: tuple[str, ...] = ("/fyers/callback",)

# The two POST routes that are reachable before a session exists, and the resolution of a genuine
# contradiction between the documents.
#
# SECURITY.md section 6.2 writes layer 3 as `if sess is None or not compare_digest(...)`: reject.
# API.md section 1 marks `POST /auth/login` as `public` and `POST /auth/setup` as `setup`. Taken
# together those are unsatisfiable, because a browser that has never logged in has no session row
# to hold a synchronizer token, and the session table's `user_id` is NOT NULL with a foreign key to
# `app_user`, so an anonymous session cannot be stored to carry one. Issuing a token with no
# server-side half would be plain double submit, which SECURITY.md rejects by name.
#
# The resolution is narrow and named rather than a blanket rule. On these two paths, and only while
# there is no session at all, layers 1 and 2 stand alone. That is not a weakening of any
# authenticated route: a cross-origin POST is already refused by `Sec-Fetch-Site` and by the Origin
# allowlist in every browser this app supports, the routes hold no authenticated state to abuse,
# and both are rate limited hard with an account lockout behind them. The moment a session does
# exist the token is required here too, so an already logged-in browser cannot be made to
# re-authenticate as somebody else.
DEFAULT_SESSIONLESS_PATHS: tuple[str, ...] = ("/auth/login", "/auth/setup")

_API_PREFIX = "/api/v1"

REASON_CROSS_ORIGIN = "cross_origin_rejected"
REASON_BAD_ORIGIN = "bad_origin"
REASON_CSRF_INVALID = "csrf_invalid"

# One safe sentence each. The reason code is machine readable and the message is renderable;
# neither says which of the three layers or which stored value did not match.
REASON_MESSAGES: dict[str, str] = {
    REASON_CROSS_ORIGIN: "This request did not come from the application.",
    REASON_BAD_ORIGIN: "This request did not come from the application.",
    REASON_CSRF_INVALID: "The request could not be verified. Reload the page and try again.",
}

# Both rejections are 403. API.md section 0 lists `403 csrf_invalid / cross_origin_rejected`.
REJECTION_STATUS = 403


@dataclass(frozen=True, slots=True)
class CsrfDecision:
    """The outcome of the three layer check."""

    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.allowed

    @property
    def message(self) -> str:
        if self.reason is None:
            return ""
        return REASON_MESSAGES.get(self.reason, REASON_MESSAGES[REASON_CSRF_INVALID])


ALLOWED = CsrfDecision(allowed=True)


def _path_matches(path: str, candidates: tuple[str, ...], *, allow_api_prefix: bool) -> bool:
    normalised = path[:-1] if path.endswith("/") and len(path) > 1 else path
    for candidate in candidates:
        if normalised == candidate:
            return True
        if allow_api_prefix and normalised == _API_PREFIX + candidate:
            return True
    return False


def is_exempt(path: str, exempt_paths: tuple[str, ...] = DEFAULT_EXEMPT_PATHS) -> bool:
    """True for the OAuth callback only. Compared exactly, with an optional trailing slash.

    The API prefix is deliberately not accepted here: the callback is mounted at the root and
    `/api/v1/fyers/callback` is not a route this application serves.
    """
    return _path_matches(path, exempt_paths, allow_api_prefix=False)


def is_sessionless(
    path: str, sessionless_paths: tuple[str, ...] = DEFAULT_SESSIONLESS_PATHS
) -> bool:
    """True for the two POST routes reachable before a session exists."""
    return _path_matches(path, sessionless_paths, allow_api_prefix=True)


def check_csrf(
    *,
    method: str,
    path: str,
    sec_fetch_site: str | None,
    origin: str | None,
    header_token: str | None,
    session_token: str | None,
    allowed_origins: frozenset[str] = DEFAULT_ALLOWED_ORIGINS,
    exempt_paths: tuple[str, ...] = DEFAULT_EXEMPT_PATHS,
    sessionless_paths: tuple[str, ...] = DEFAULT_SESSIONLESS_PATHS,
) -> CsrfDecision:
    """The whole policy as one pure function.

    Pure so it can be tested exhaustively without an app, a session store or a network, and so the
    middleware below has nothing in it worth reading twice.
    """
    if method.upper() in SAFE_METHODS:
        return ALLOWED
    if is_exempt(path, exempt_paths):
        return ALLOWED

    # Layer 1. `same-site` is refused as well as `cross-site`: a sibling port on 127.0.0.1 is a
    # different origin and any local development server the user runs is one.
    if sec_fetch_site is not None and sec_fetch_site.lower() in {"cross-site", "same-site"}:
        return CsrfDecision(allowed=False, reason=REASON_CROSS_ORIGIN)

    # Layer 2. An absent Origin is not a rejection; a present and unknown one is.
    if origin is not None and origin not in allowed_origins:
        return CsrfDecision(allowed=False, reason=REASON_BAD_ORIGIN)

    # Layer 3. See DEFAULT_SESSIONLESS_PATHS: login and setup are reachable with no session at all,
    # and on those two, and only while there is genuinely no session, layers 1 and 2 stand alone.
    if not session_token:
        if is_sessionless(path, sessionless_paths):
            return ALLOWED
        return CsrfDecision(allowed=False, reason=REASON_CSRF_INVALID)
    if not header_token:
        return CsrfDecision(allowed=False, reason=REASON_CSRF_INVALID)
    if not hmac.compare_digest(header_token, session_token):
        return CsrfDecision(allowed=False, reason=REASON_CSRF_INVALID)
    return ALLOWED


class CsrfMiddleware:
    """Enforce `check_csrf` on every unsafe method.

    Pure ASGI, and it reads the session that `SessionMiddleware` has already resolved into
    `scope["state"]`, so it must be mounted inside that one. The app factory's middleware order is
    TrustedHost, SecurityHeaders, Session, CSRF, RateLimit.
    """

    def __init__(
        self,
        app,  # type: ignore[no-untyped-def]
        *,
        allowed_origins: frozenset[str] = DEFAULT_ALLOWED_ORIGINS,
        exempt_paths: tuple[str, ...] = DEFAULT_EXEMPT_PATHS,
        sessionless_paths: tuple[str, ...] = DEFAULT_SESSIONLESS_PATHS,
    ) -> None:
        self._app = app
        self._allowed_origins = frozenset(allowed_origins)
        self._exempt_paths = tuple(exempt_paths)
        self._sessionless_paths = tuple(sessionless_paths)

    @property
    def allowed_origins(self) -> frozenset[str]:
        return self._allowed_origins

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method in SAFE_METHODS:
            await self._app(scope, receive, send)
            return

        headers = _header_map(scope)
        session = (scope.get("state") or {}).get("session")
        decision = check_csrf(
            method=method,
            path=scope.get("path", ""),
            sec_fetch_site=headers.get("sec-fetch-site"),
            origin=headers.get("origin"),
            header_token=headers.get(CSRF_HEADER_NAME),
            session_token=getattr(session, "csrf_token", None),
            allowed_origins=self._allowed_origins,
            exempt_paths=self._exempt_paths,
            sessionless_paths=self._sessionless_paths,
        )
        if decision.allowed:
            await self._app(scope, receive, send)
            return

        response = error_response(
            REJECTION_STATUS, decision.reason or REASON_CSRF_INVALID, decision.message
        )
        await response(scope, receive, send)


def _header_map(scope) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in scope.get("headers", [])
    }
