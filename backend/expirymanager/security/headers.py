"""The response security headers, the CSP, and the shared error envelope.

The header values are built with `secure` 2.0.1 rather than written as string literals, and then
frozen into module constants at import. The builders give the library's escaping and ordering; the
constants give a value that cannot drift between one middleware and the next, and something a test
can compare against the text in SECURITY.md section 9 character for character.

The CSP is the part worth reading. Every directive below was checked against what the application
actually loads, not assumed:

- `script-src 'self'`. There is no `eval` and no `new Function` anywhere in `openalgo-charts`
  2.1.0's shipped `dist/`, checked by grep across every emitted module, and the built
  `frontend/dist/index.html` references only external `<script src=...>` files. Inline script is
  the XSS vector that matters here and it stays blocked.
- `style-src 'self' 'unsafe-inline'`. Required, and the reason is verified rather than assumed.
  `openalgo-charts.widget.mjs` does `document.createElement("style")`, assigns `textContent` and
  appends it to `head`, which CSP governs under `style-src`, and it writes the `--oac-tool-cursor`
  chrome token with `el.style.setProperty`, which is a style attribute write and needs the same
  keyword. A strict `style-src 'self'` renders the chart unstyled with no console error the user
  would recognise. React's own `style={...}` props across the SPA need it too.
- `img-src 'self' data:`. `openalgo-charts.draw.mjs` builds drawing-tool cursors as
  `cursor: url("data:image/svg+xml,...")`, and a CSS `url()` cursor is governed by `img-src`.
  Without `data:` the drawing tools fall back to the default pointer.
- `font-src 'self'`. The Geist variable faces are bundled into `frontend/dist/assets` by
  `@fontsource-variable/geist`, so nothing is fetched from a font CDN.
- `connect-src 'self'`. The API and the SSE stream at `/events/stream` are same origin in
  production. In development the SPA is served by Vite, which sends its own headers, so the Vite
  HMR websocket is not governed by this policy.
- `frame-ancestors 'none'`, `base-uri 'none'`, `object-src 'none'`, `form-action 'self'`. Nothing
  in the app frames anything, rewrites its base, embeds a plugin, or posts a form cross origin.

The chart's snapshot export creates a `blob:` object URL and clicks a synthetic `<a download>`.
That is a download rather than a fetch or a subresource load, and no shipped CSP directive governs
an anchor navigation, so it needs no keyword here. It is written down because the absence of a
directive looks like an oversight otherwise.
"""

from __future__ import annotations

from typing import Any

from secure import (
    CacheControl,
    ContentSecurityPolicy,
    CrossOriginOpenerPolicy,
    CrossOriginResourcePolicy,
    PermissionsPolicy,
    ReferrerPolicy,
    StrictTransportSecurity,
    XContentTypeOptions,
    XFrameOptions,
)
from starlette.responses import JSONResponse

__all__ = [
    "CONTENT_SECURITY_POLICY",
    "PERMISSIONS_POLICY",
    "STRICT_TRANSPORT_SECURITY",
    "HSTS_MAX_AGE",
    "NO_STORE",
    "BASE_SECURITY_HEADERS",
    "ENVIRONMENT_PRODUCTION",
    "ENVIRONMENT_DEVELOPMENT",
    "LOOPBACK_HOSTS",
    "SecurityHeadersMiddleware",
    "error_response",
    "error_body",
]

ENVIRONMENT_DEVELOPMENT = "development"
ENVIRONMENT_PRODUCTION = "production"

CONTENT_SECURITY_POLICY = (
    ContentSecurityPolicy()
    .default_src("'self'")
    .script_src("'self'")
    .style_src("'self'", "'unsafe-inline'")
    .img_src("'self'", "data:")
    .font_src("'self'")
    .connect_src("'self'")
    .frame_ancestors("'none'")
    .base_uri("'none'")
    .form_action("'self'")
    .object_src("'none'")
).header_value

PERMISSIONS_POLICY = (
    PermissionsPolicy().geolocation().camera().microphone().payment().usb()
).header_value

HSTS_MAX_AGE = 31536000
STRICT_TRANSPORT_SECURITY = StrictTransportSecurity().max_age(HSTS_MAX_AGE).header_value

NO_STORE = CacheControl().no_store().header_value

# Emitted on every response, whatever the route and whatever the status.
BASE_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Content-Type-Options", XContentTypeOptions().nosniff().header_value),
    # `no-referrer` is one of the three mandatory mitigations for the OAuth callback URL carrying
    # `auth_code` in its query string. The other two are the 303 to a clean URL and the access log
    # filter in security/redaction.py.
    ("Referrer-Policy", ReferrerPolicy().no_referrer().header_value),
    ("X-Frame-Options", XFrameOptions().deny().header_value),
    ("Permissions-Policy", PERMISSIONS_POLICY),
    ("Cross-Origin-Opener-Policy", CrossOriginOpenerPolicy().same_origin().header_value),
    ("Cross-Origin-Resource-Policy", CrossOriginResourcePolicy().same_origin().header_value),
)

# Hosts on which HSTS is never sent whatever the environment. SECURITY.md section 3a: HSTS on
# 127.0.0.1 poisons that origin in the browser for a year, for every other local development server
# the user runs, and a self-signed certificate makes recovery worse. This app binds loopback only,
# so in practice the gate below never opens. It is still written as a gate rather than as a hard
# `return`, because the day someone terminates TLS in front of this on a real host, the correct
# behaviour must already be in the code rather than in a comment.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Paths whose responses are never cached. `/api` because API.md requires it. The OAuth callback
# because its URL carries `auth_code` and a cached 303 in the back/forward cache would replay it.
_NO_STORE_PREFIXES: tuple[str, ...] = ("/api", "/fyers/callback")


def error_body(
    code: str, message: str, *, correlation_id: str | None = None, detail: Any = None
) -> dict[str, Any]:
    """The one error envelope from API.md section 0.

    Defined here so the security middleware, which rejects before any route or exception handler
    runs, produces the same shape the rest of the API does. `api/errors.py` reuses it.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if correlation_id is not None:
        error["correlation_id"] = correlation_id
    if detail is not None:
        error["detail"] = detail
    return {"error": error}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    detail: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """A JSON error carrying the current correlation id, if one is bound."""
    correlation_id = _current_correlation_id()
    return JSONResponse(
        status_code=status_code,
        content=error_body(code, message, correlation_id=correlation_id, detail=detail),
        headers=headers,
    )


def _current_correlation_id() -> str | None:
    # Imported lazily: logging_setup imports security.redaction, and a module level import here
    # would make the security package depend on logging configuration order.
    try:
        from expirymanager.logging_setup import correlation_id_var
    except ImportError:  # pragma: no cover - logging_setup is always importable
        return None
    if correlation_id_var is None:  # pragma: no cover - defensive
        return None
    try:
        return correlation_id_var.get()
    except LookupError:  # pragma: no cover - the ContextVar has a default
        return None


class SecurityHeadersMiddleware:
    """Add the security header set to every response.

    Pure ASGI rather than `BaseHTTPMiddleware`, because `BaseHTTPMiddleware` buffers through an
    anyio task group and that turns the SSE stream at `GET /events/stream` into a response that
    only arrives when the stream ends.

    Headers a route set for itself are not overwritten, so a download can still name its own
    `Content-Disposition` and `Cache-Control`; the security headers themselves are always asserted,
    because a route that could weaken its own CSP is a route that eventually does.
    """

    def __init__(
        self,
        app,  # type: ignore[no-untyped-def]
        *,
        environment: str = ENVIRONMENT_DEVELOPMENT,
        no_store_prefixes: tuple[str, ...] = _NO_STORE_PREFIXES,
        hsts_max_age: int = HSTS_MAX_AGE,
    ) -> None:
        self._app = app
        self._environment = environment
        self._no_store_prefixes = no_store_prefixes
        self._hsts = StrictTransportSecurity().max_age(hsts_max_age).header_value

    @property
    def environment(self) -> str:
        return self._environment

    def _wants_hsts(self, scope) -> bool:  # type: ignore[no-untyped-def]
        """Production, https, and not a loopback host. All three, or nothing is sent."""
        if self._environment != ENVIRONMENT_PRODUCTION:
            return False
        if scope.get("scheme") != "https":
            return False
        return _host_of(scope) not in LOOPBACK_HOSTS

    def _wants_no_store(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self._no_store_prefixes)

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        add_hsts = self._wants_hsts(scope)
        add_no_store = self._wants_no_store(scope.get("path", ""))

        async def send_wrapper(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] != "http.response.start":
                await send(message)
                return

            raw = list(message.get("headers", []))
            present = {name.lower() for name, _value in raw}
            for name, value in BASE_SECURITY_HEADERS:
                key = name.lower().encode("latin-1")
                raw = [(n, v) for n, v in raw if n.lower() != key]
                raw.append((key, value.encode("latin-1")))
            if add_hsts:
                raw.append((b"strict-transport-security", self._hsts.encode("latin-1")))
            if add_no_store and b"cache-control" not in present:
                raw.append((b"cache-control", NO_STORE.encode("latin-1")))
            message["headers"] = raw
            await send(message)

        await self._app(scope, receive, send_wrapper)


def _host_of(scope) -> str:  # type: ignore[no-untyped-def]
    for name, value in scope.get("headers", []):
        if name.lower() == b"host":
            host = value.decode("latin-1")
            # Strip the port. An IPv6 literal keeps its brackets, which is why the bracketed form
            # is in LOOPBACK_HOSTS as well.
            if host.startswith("["):
                return host.split("]")[0] + "]"
            return host.split(":")[0]
    return ""
