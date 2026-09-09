"""The only module that lets httpx touch Fyers.

Everything outbound goes through one shared `httpx.AsyncClient`: one connection pool, one place
that writes the Authorization header, one place that percent encodes a symbol, and one place that
decides which of the two response envelopes to parse. A second client, or a bare httpx call from a
handler, bypasses the outbound limiter, and bypassing the limiter is how an account gets blocked
for the rest of the day.

Two envelopes, chosen by the endpoint and never by inspection:

  standard   {"s": "ok", "code": 200, "message": "", "data": {...}}
  candles    {"s": "ok", "symbol": ..., "resolution": ..., "columns": [...],
              "candles": [[...]], "schema_version": 1}

The candle envelope has no `data` wrapper and carries no `code` or `message` on success, so a
standard parser reading `code` off a candle response would see None and a candle parser reading
`candles` off a standard response would see nothing. The choice is a declared property of the
endpoint, in endpoints.py.

Candle values are addressed by the returned `columns` array, never by position. The seventh
element exists only when `include_oi` is set and `include_greeks` will append more later, so a
fixed index is a bug waiting for the next schema version.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlencode

import httpx

from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers.errors import (
    STATUS_NO_DATA,
    STATUS_OK,
    Classification,
    RetryClass,
    classify,
    classify_exception,
)
from expirymanager.version import user_agent

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_CANDLE_COLUMNS",
    "OI_COLUMN",
    "AuthContext",
    "TokenSupplier",
    "RequestGovernor",
    "FyersClientError",
    "AuthTokenUnavailable",
    "MalformedResponse",
    "FyersResponse",
    "CandleResponse",
    "FyersClient",
    "encode_query",
    "authorization_header",
    "parse_standard_envelope",
    "parse_candle_envelope",
]

log = logging.getLogger(__name__)

# Generous enough for a 95 day one minute chunk, short enough that a hung socket does not hold a
# lease past its 120 second expiry.
DEFAULT_TIMEOUT_SECONDS = 30.0

# The documented order for endpoints that do not return a columns array. Used only for
# `/data/history`, and recorded as such on the response so a reader can tell the difference.
DEFAULT_CANDLE_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
OI_COLUMN = "open_interest"


@dataclass(frozen=True, slots=True)
class AuthContext:
    """What the Authorization header needs, plus the generation that produced it.

    The token is kept out of `repr` because dataclass reprs end up in tracebacks, and a traceback
    ends up in a log file.
    """

    app_id: str
    access_token: str = field(repr=False)
    generation: int = 1

    def header(self) -> str:
        return authorization_header(self.app_id, self.access_token)


@runtime_checkable
class TokenSupplier(Protocol):
    """What the client needs from the token broker. Deliberately one method wide."""

    async def auth_context(self) -> AuthContext | None: ...


@runtime_checkable
class RequestGovernor(Protocol):
    """What the client needs from the outbound limiter."""

    def slot(self, endpoint: str) -> Any:
        """An async context manager held for the duration of one request."""
        ...

    async def note_rate_limited(self, *, endpoint: str, http_status: int | None, code: int | None) -> None: ...


class FyersClientError(Exception):
    """Base class for failures raised by the client itself rather than reported by Fyers."""


class AuthTokenUnavailable(FyersClientError):
    """An authenticated endpoint was called with no usable token. The user must log in."""


class MalformedResponse(FyersClientError):
    """The body was not JSON, or not the shape the endpoint's envelope requires."""


def authorization_header(app_id: str, access_token: str) -> str:
    """The documented format: `app_id:access_token`, with no Bearer prefix.

    Adding Bearer is the single most common way to get -16 from this API.
    """
    return f"{app_id}:{access_token}"


def encode_query(params: Mapping[str, Any], *, safe: str = ":") -> str:
    """Percent encode a parameter mapping the way the docs' own cURL samples read.

    By default `:` is left literal so `NSE:SBIN-EQ` stays legible in a log line and in the access
    log, exactly as the documented cURL samples show it. Everything else is escaped, which is what
    turns `M&M` into `M%26M`. Getting that wrong is the documented cause of error -300, and the
    failure mode is a query string that silently truncates at the ampersand.

    Pass `safe=""` to escape everything, which is what the authorize URL does: its redirect_uri
    value must come out looking exactly like the documented sample.
    """
    pairs = [(key, str(value)) for key, value in params.items() if value is not None]
    return urlencode(pairs, quote_via=quote, safe=safe)


def _payload_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class FyersResponse:
    """One parsed standard envelope."""

    status: str
    code: int | None
    message: str
    payload: Mapping[str, Any]
    http_status: int
    response_bytes: int
    latency_ms: int
    payload_sha256: str
    endpoint: str

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def data(self) -> Any:
        """The `data` wrapper when there is one. Quotes answers under `d` instead."""
        if "data" in self.payload:
            return self.payload["data"]
        return self.payload.get("d")

    def classification(self) -> Classification | None:
        """The retry class for a failure, or None when the call succeeded."""
        if self.ok:
            return None
        return classify(
            status=self.status,
            code=self.code,
            message=self.message,
            http_status=self.http_status,
        )


@dataclass(frozen=True, slots=True)
class CandleResponse:
    """One parsed candle envelope.

    `columns` is what the response declared. `columns_from_response` records whether the broker
    supplied it or whether it came from the documented order, so nothing downstream has to guess
    how much to trust the mapping.
    """

    status: str
    symbol: str | None
    resolution: str | None
    columns: tuple[str, ...]
    candles: tuple[Sequence[Any], ...]
    schema_version: int | None
    code: int | None
    message: str
    http_status: int
    response_bytes: int
    latency_ms: int
    payload_sha256: str
    endpoint: str
    columns_from_response: bool

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def is_no_data(self) -> bool:
        """A success with zero rows. The contract simply did not trade in this window."""
        return self.status == STATUS_NO_DATA

    @property
    def row_count(self) -> int:
        return len(self.candles)

    @property
    def has_open_interest(self) -> bool:
        return OI_COLUMN in self.columns

    def classification(self) -> Classification | None:
        if self.ok:
            return None
        return classify(
            status=self.status,
            code=self.code,
            message=self.message,
            http_status=self.http_status,
        )


def _load_json(raw: bytes, endpoint: str) -> Mapping[str, Any]:
    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise MalformedResponse(f"{endpoint} returned a body that is not json") from exc
    if not isinstance(body, Mapping):
        raise MalformedResponse(f"{endpoint} returned a json {type(body).__name__}, not an object")
    return body


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _status_of(body: Mapping[str, Any], http_status: int) -> str:
    raw = body.get("s")
    if isinstance(raw, str) and raw:
        return raw
    # Some failures answer with an HTTP status and a code but no `s`. Treating a missing `s` on a
    # non 2xx response as ok would hand a handler an empty success.
    return STATUS_OK if 200 <= http_status < 300 else "error"


def parse_standard_envelope(
    raw: bytes,
    *,
    http_status: int,
    endpoint: str,
    latency_ms: int = 0,
) -> FyersResponse:
    """Parse `s`, `code`, `message` and whatever payload key the endpoint uses."""
    body = _load_json(raw, endpoint)
    message = body.get("message")
    return FyersResponse(
        status=_status_of(body, http_status),
        code=_as_int(body.get("code")),
        message=message if isinstance(message, str) else "",
        payload=body,
        http_status=http_status,
        response_bytes=len(raw),
        latency_ms=latency_ms,
        payload_sha256=_payload_digest(raw),
        endpoint=endpoint,
    )


def parse_candle_envelope(
    raw: bytes,
    *,
    http_status: int,
    endpoint: str,
    latency_ms: int = 0,
    require_columns: bool = True,
) -> CandleResponse:
    """Parse the flat historical data envelope.

    `require_columns` is true for the expired contracts endpoint, which documents a `columns`
    array, and false for `/data/history`, which does not. When it is false and the array is
    missing, the documented column order is used and the response records that it was inferred.
    """
    body = _load_json(raw, endpoint)
    status = _status_of(body, http_status)
    raw_candles = body.get("candles")
    candles: tuple[Sequence[Any], ...]
    if raw_candles is None:
        candles = ()
    elif isinstance(raw_candles, list):
        candles = tuple(row for row in raw_candles)
    else:
        raise MalformedResponse(f"{endpoint} returned candles as a {type(raw_candles).__name__}")

    raw_columns = body.get("columns")
    columns_from_response = isinstance(raw_columns, list) and bool(raw_columns)
    if columns_from_response:
        columns = tuple(str(name) for name in raw_columns)
    elif status == STATUS_OK and candles and require_columns:
        raise MalformedResponse(
            f"{endpoint} returned candles with no columns array, so no field mapping is possible"
        )
    else:
        columns = _documented_columns(candles)

    if status == STATUS_OK and candles and len(columns) < len(DEFAULT_CANDLE_COLUMNS):
        raise MalformedResponse(
            f"{endpoint} declared {len(columns)} columns, fewer than the documented six"
        )

    message = body.get("message")
    symbol = body.get("symbol")
    resolution = body.get("resolution")
    return CandleResponse(
        status=status,
        symbol=symbol if isinstance(symbol, str) else None,
        resolution=str(resolution) if resolution is not None else None,
        columns=columns,
        candles=candles,
        schema_version=_as_int(body.get("schema_version")),
        code=_as_int(body.get("code")),
        message=message if isinstance(message, str) else "",
        http_status=http_status,
        response_bytes=len(raw),
        latency_ms=latency_ms,
        payload_sha256=_payload_digest(raw),
        endpoint=endpoint,
        columns_from_response=columns_from_response,
    )


def _documented_columns(candles: Sequence[Sequence[Any]]) -> tuple[str, ...]:
    """The column order for an endpoint that does not return one.

    Only the row width decides whether open interest is present, because the flag that asked for
    it is on the request and the response says nothing about it.
    """
    if not candles:
        return DEFAULT_CANDLE_COLUMNS
    width = len(candles[0])
    if width > len(DEFAULT_CANDLE_COLUMNS):
        return DEFAULT_CANDLE_COLUMNS + (OI_COLUMN,)
    return DEFAULT_CANDLE_COLUMNS


@asynccontextmanager
async def _no_governor(_endpoint: str):
    """Used when no governor is injected, which is only ever the case in a unit test."""
    yield


class FyersClient:
    """One shared async HTTP client against the Fyers API.

    Construct exactly one in the lifespan and inject it. The client owns no token: it asks the
    token supplier for one per request, so a login that lands mid job is picked up by the next
    request without anybody rebuilding the client.
    """

    def __init__(
        self,
        *,
        tokens: TokenSupplier | None = None,
        governor: RequestGovernor | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = ep.API_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._tokens = tokens
        self._governor = governor
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=timeout,
            headers={
                "User-Agent": user_agent(),
                "Accept": "application/json",
            },
            follow_redirects=False,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> FyersClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _slot(self, endpoint: ep.Endpoint):
        if endpoint.governed and self._governor is not None:
            return self._governor.slot(endpoint.name)
        return _no_governor(endpoint.name)

    async def _headers_for(self, endpoint: ep.Endpoint) -> dict[str, str]:
        headers: dict[str, str] = {}
        if not endpoint.authenticated:
            return headers
        if self._tokens is None:
            raise AuthTokenUnavailable(
                f"{endpoint.name} needs an access token and no token supplier is configured"
            )
        context = await self._tokens.auth_context()
        if context is None:
            raise AuthTokenUnavailable(f"{endpoint.name} needs an access token, log in first")
        headers["Authorization"] = context.header()
        return headers

    async def _send(
        self,
        endpoint: ep.Endpoint,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> tuple[bytes, int, int]:
        """Send one request and return its body, HTTP status and latency in milliseconds."""
        url = endpoint.path
        if params:
            url = f"{endpoint.path}?{encode_query(params)}"
        headers = await self._headers_for(endpoint)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        started = time.perf_counter()
        async with self._slot(endpoint):
            try:
                response = await self._http.request(
                    endpoint.method,
                    url,
                    headers=headers,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                classification = classify_exception(exc)
                # The exception type is logged, never the URL: the query string of an auth call
                # carries the auth code.
                log.warning(
                    "fyers request failed",
                    extra={
                        "endpoint": endpoint.name,
                        "retry_class": str(classification.retry_class),
                        "latency_ms": latency_ms,
                    },
                )
                raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        return response.content, response.status_code, latency_ms

    async def _note_rate_limit(self, endpoint: ep.Endpoint, classification: Classification | None) -> None:
        if classification is None or self._governor is None:
            return
        if classification.retry_class is not RetryClass.RATE_LIMITED:
            return
        # The first rate limit response stops the pipeline. It is not retried and not backed off,
        # because the account is blocked for the rest of the day on the fourth violation.
        await self._governor.note_rate_limited(
            endpoint=endpoint.name,
            http_status=classification.http_status,
            code=classification.code,
        )

    async def request_standard(
        self,
        endpoint: ep.Endpoint,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> FyersResponse:
        """Send one request and parse the standard `s`, `code`, `message`, `data` envelope."""
        if endpoint.envelope is not ep.Envelope.STANDARD:
            raise ValueError(f"{endpoint.name} answers with the candle envelope")
        raw, http_status, latency_ms = await self._send(
            endpoint, params=params, json_body=json_body
        )
        parsed = parse_standard_envelope(
            raw, http_status=http_status, endpoint=endpoint.name, latency_ms=latency_ms
        )
        await self._note_rate_limit(endpoint, parsed.classification())
        return parsed

    async def request_candles(
        self,
        endpoint: ep.Endpoint,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> CandleResponse:
        """Send one request and parse the flat historical data envelope."""
        if endpoint.envelope is not ep.Envelope.CANDLES:
            raise ValueError(f"{endpoint.name} answers with the standard envelope")
        raw, http_status, latency_ms = await self._send(endpoint, params=params)
        parsed = parse_candle_envelope(
            raw,
            http_status=http_status,
            endpoint=endpoint.name,
            latency_ms=latency_ms,
            require_columns=endpoint.returns_columns,
        )
        await self._note_rate_limit(endpoint, parsed.classification())
        return parsed
