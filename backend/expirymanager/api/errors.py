"""The error envelope, the correlation id, and the handlers that keep upstream detail out.

Boundary B in ARCHITECTURE.md section 5 is the reason this file exists. A Fyers error body can
contain a token, and `raise HTTPException(500, str(exc))` is exactly how one reaches the browser
console. So there is one rule here and it has no exceptions: the client gets a stable code, a
message that was written in this repository, and a correlation id. Everything else goes to the
log, and to the log only after `redact_value`.

The envelope shape itself is defined in `security/headers.py` and reused rather than rebuilt,
because the CSRF and rate limit middleware reject before any handler in this file can run, and two
independently written envelopes drift the moment one of them gains a field.

The correlation id is bound at the very top of the middleware stack so that every log record
emitted while serving a request carries it, including the ones written by a middleware rejection
that never reaches a route.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Iterable

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from expirymanager.api.schemas.common import CursorError
from expirymanager.logging_setup import correlation_id_var
from expirymanager.security.headers import (
    BASE_SECURITY_HEADERS,
    NO_STORE,
    error_body,
    error_response,
)
from expirymanager.security.ratelimit import RateLimited, rate_limited_response
from expirymanager.security.redaction import redact_text, redact_value

__all__ = [
    "CORRELATION_HEADER",
    "CODE_NOT_AUTHENTICATED",
    "CODE_FORBIDDEN",
    "CODE_NOT_FOUND",
    "CODE_CONFLICT",
    "CODE_NEEDS_REAUTH",
    "CODE_VALIDATION_ERROR",
    "CODE_RATE_LIMITED",
    "CODE_PIPELINE_STOPPED",
    "CODE_SERVICE_UNAVAILABLE",
    "CODE_INTERNAL_ERROR",
    "CODE_INVALID_CURSOR",
    "STATUS_CODES",
    "ApiError",
    "not_authenticated",
    "not_found",
    "needs_reauth",
    "pipeline_stopped",
    "CorrelationIdMiddleware",
    "new_correlation_id",
    "install_error_handlers",
]

log = logging.getLogger(__name__)

CORRELATION_HEADER = "X-Correlation-Id"

# The standard cases from API.md section 0, so that no two route modules invent two spellings of
# the same condition. A route with a case of its own passes its own code to ApiError.
CODE_NOT_AUTHENTICATED = "not_authenticated"
CODE_FORBIDDEN = "forbidden"
CODE_NOT_FOUND = "not_found"
CODE_CONFLICT = "conflict"
CODE_NEEDS_REAUTH = "needs_reauth"
CODE_VALIDATION_ERROR = "validation_error"
CODE_RATE_LIMITED = "rate_limited"
CODE_PIPELINE_STOPPED = "pipeline_stopped"
CODE_SERVICE_UNAVAILABLE = "service_unavailable"
CODE_INTERNAL_ERROR = "internal_error"
CODE_INVALID_CURSOR = "invalid_cursor"

# The code a bare HTTPException carries when the route did not name one.
STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: CODE_NOT_AUTHENTICATED,
    403: CODE_FORBIDDEN,
    404: CODE_NOT_FOUND,
    405: "method_not_allowed",
    409: CODE_CONFLICT,
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: CODE_VALIDATION_ERROR,
    423: "account_locked",
    429: CODE_RATE_LIMITED,
    500: CODE_INTERNAL_ERROR,
    503: CODE_SERVICE_UNAVAILABLE,
}

INTERNAL_ERROR_MESSAGE = (
    "The server could not complete this request. The correlation id identifies the log entry."
)


class ApiError(Exception):
    """The exception every route raises for an expected failure.

    Carries the envelope directly, so that a route never has to think about how the body is
    shaped, and so that `message` is unambiguously a string this repository wrote.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status_code} {code}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        self.headers = headers

    def response(self) -> JSONResponse:
        return error_response(
            self.status_code,
            self.code,
            self.message,
            detail=self.detail,
            headers=self.headers,
        )


def not_authenticated(message: str = "Sign in to continue.") -> ApiError:
    return ApiError(401, CODE_NOT_AUTHENTICATED, message)


def not_found(message: str = "Not found.", *, code: str = CODE_NOT_FOUND) -> ApiError:
    return ApiError(404, code, message)


def needs_reauth(
    message: str = "The Fyers session has ended. Connect the broker again to continue.",
) -> ApiError:
    """409, the code the frontend turns into the persistent reconnect banner."""
    return ApiError(409, CODE_NEEDS_REAUTH, message)


def pipeline_stopped(message: str = "The download pipeline is not running.") -> ApiError:
    return ApiError(503, CODE_PIPELINE_STOPPED, message)


def new_correlation_id() -> str:
    """Sixteen hex characters. Long enough to be unique in a log file, short enough to read out."""
    return secrets.token_hex(8)


class CorrelationIdMiddleware:
    """Bind a correlation id for the duration of one request and echo it on the response.

    Pure ASGI. `BaseHTTPMiddleware` would run the downstream app in a separate anyio task, and a
    ContextVar set in the middleware would then not be visible to the route, which is the whole
    point of setting it. It also mounts outermost, above TrustedHost, so that a request rejected
    by host validation still logs under an id.
    """

    def __init__(self, app, *, header: str = CORRELATION_HEADER) -> None:  # type: ignore[no-untyped-def]
        self._app = app
        self._header = header.encode("latin-1")

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = new_correlation_id()
        token = None
        if correlation_id_var is not None:
            token = correlation_id_var.set(correlation_id)

        # Also on the scope, because the ContextVar is reset on the way out of this middleware
        # while `ServerErrorMiddleware` sits above it and renders the 500 afterwards. Without the
        # scope copy, the one response that most needs a correlation id would be the one without.
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        encoded = correlation_id.encode("latin-1")

        async def send_wrapper(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((self._header.lower(), encoded))
                message["headers"] = headers
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            if token is not None and correlation_id_var is not None:
                correlation_id_var.reset(token)


def _security_headers() -> dict[str, str]:
    """The header set, for a response produced above the security header middleware.

    `ServerErrorMiddleware` sits outside every middleware the app factory installs, so the 500 it
    renders never passes through `SecurityHeadersMiddleware`'s send wrapper. Reusing W07's
    constants here is what keeps that one response from being the single uncovered case.
    """
    headers = {name: value for name, value in BASE_SECURITY_HEADERS}
    headers["Cache-Control"] = NO_STORE
    return headers


def _safe_validation_errors(errors: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip a Pydantic error list down to what is safe to return.

    Pydantic v2 puts the offending value in `input`. On `POST /auth/login` that value is the
    password, and on `POST /broker/fyers/credentials` it is the app secret. So `input` and `ctx`
    are dropped rather than redacted: neither adds anything the field location does not already
    say, and a field a future item names something unexpected must not be the thing that decides
    whether a secret is echoed.
    """
    safe: list[dict[str, Any]] = []
    for error in errors:
        location = [str(part) for part in error.get("loc", ())]
        safe.append(
            {
                "loc": location,
                "type": str(error.get("type", "")),
                "msg": redact_text(str(error.get("msg", ""))),
            }
        )
    return safe


def _http_exception_code(exc: StarletteHTTPException) -> tuple[str, str, Any]:
    """Read a code, a message and an optional detail out of an HTTPException.

    A route may raise `HTTPException(status_code=409, detail={"code": ..., "message": ...})` to
    name its own code without importing ApiError. Anything else falls back to the status code
    table, and the detail is redacted before it becomes the message, because that is the field a
    caller is most likely to have filled with an upstream string.
    """
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        message = str(detail.get("message", STATUS_CODES.get(exc.status_code, "Request failed.")))
        return (str(detail["code"]), message, detail.get("detail"))
    code = STATUS_CODES.get(exc.status_code, CODE_INTERNAL_ERROR)
    if isinstance(detail, str) and detail:
        return (code, redact_text(detail), None)
    return (code, code.replace("_", " ").capitalize() + ".", None)


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler. Called by the app factory.

    Ordering note: the handlers for ApiError, HTTPException, RequestValidationError and
    RateLimited run inside Starlette's ExceptionMiddleware, which sits below every middleware the
    factory installs, so their responses collect the security headers, the rate limit headers and
    the correlation id header on the way out. The catch-all runs in ServerErrorMiddleware, which
    does not, so it carries the headers itself.
    """

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request, exc: ApiError) -> Response:  # type: ignore[no-untyped-def]
        if exc.status_code >= 500:
            log.error(
                "api error",
                extra={"error_code": exc.code, "status_code": exc.status_code},
            )
        return exc.response()

    @app.exception_handler(RateLimited)
    async def _handle_rate_limited(_request, exc: RateLimited) -> Response:  # type: ignore[no-untyped-def]
        # Raised by the route-level limiter, which is the only place that has parsed a username
        # out of the body. Rendered with the same helper the middleware uses.
        return rate_limited_response(exc.decision)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_request, exc: StarletteHTTPException) -> Response:  # type: ignore[no-untyped-def]
        code, message, detail = _http_exception_code(exc)
        return error_response(
            exc.status_code,
            code,
            message,
            detail=detail,
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(CursorError)
    async def _handle_cursor_error(_request, exc: CursorError) -> Response:  # type: ignore[no-untyped-def]
        # A cursor is client input that becomes part of a WHERE clause. A paging route can call
        # decode_cursor without wrapping it, and a tampered token is a 400 rather than a 500.
        # The message comes from api/schemas/common.py, so it is text this repository wrote.
        return error_response(400, CODE_INVALID_CURSOR, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_request, exc: RequestValidationError) -> Response:  # type: ignore[no-untyped-def]
        return error_response(
            422,
            CODE_VALIDATION_ERROR,
            "The request body or query string is not valid.",
            detail={"errors": _safe_validation_errors(exc.errors())},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request, exc: Exception) -> Response:  # type: ignore[no-untyped-def]
        correlation_id = getattr(request.state, "correlation_id", None)
        if not correlation_id and correlation_id_var is not None:
            correlation_id = correlation_id_var.get()
        # The exception itself never reaches the client. It reaches the log, redacted, and
        # ServerErrorMiddleware re-raises afterwards so the traceback is written there too, where
        # the redaction filters on the root handlers scrub it.
        log.error(
            "unhandled exception",
            extra={
                "correlation_id": correlation_id,
                "path": redact_text(str(request.url.path)),
                "error_type": type(exc).__name__,
                "error_detail": redact_value(str(exc)),
            },
        )
        headers = _security_headers()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        return JSONResponse(
            status_code=500,
            content=error_body(
                CODE_INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE, correlation_id=correlation_id
            ),
            headers=headers,
        )
