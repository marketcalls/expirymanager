"""A fake Fyers transport for the W08 tests. Nothing here touches the network.

Every credential in this module is synthetic. Real credentials live outside the repository and are
never read by application code or by a test.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

# Synthetic throughout. The app id shape matches the documented one so the encoder is exercised.
FAKE_APP_ID = "TESTAPP01-100"
FAKE_APP_SECRET = "synthetic-secret-not-a-real-value"
FAKE_ACCESS_TOKEN = "header.payload.signature"
FAKE_REDIRECT_URI = "https://127.0.0.1:8000/fyers/callback"


def make_access_token(expires_at: datetime) -> str:
    """A JWT shaped synthetic access token carrying one exp claim.

    Not signed by anything and not verifiable: the product only ever reads the exp claim locally,
    so a signature would test nothing. Nothing in this string is or resembles a real credential.
    """
    header = _b64({"typ": "JWT", "alg": "HS256"})
    payload = _b64({"iss": "api.fyers.in", "exp": int(expires_at.timestamp())})
    return f"{header}.{payload}.signature"


def token_valid_for(days: int = 1) -> str:
    return make_access_token(datetime.now(UTC) + timedelta(days=days))


def _b64(claims: dict[str, Any]) -> str:
    raw = json.dumps(claims, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class RecordingTransport(httpx.MockTransport):
    """A MockTransport that keeps every request it was handed."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(wrapped)

    @property
    def last_request(self) -> httpx.Request:
        return self.requests[-1]


def json_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def always(status_code: int, body: dict[str, Any]) -> RecordingTransport:
    """A transport that answers every request with the same envelope."""
    return RecordingTransport(lambda _request: json_response(status_code, body))


def candle_body(
    *,
    status: str = "ok",
    columns: list[str] | None = None,
    candles: list[list[Any]] | None = None,
    symbol: str = "NSE:NIFTY25MAR23000CE",
    resolution: str = "60",
) -> dict[str, Any]:
    """The historical data envelope: no data wrapper, no code or message on success."""
    body: dict[str, Any] = {
        "s": status,
        "symbol": symbol,
        "resolution": resolution,
        "schema_version": 1,
    }
    if status == "ok":
        body["columns"] = columns if columns is not None else [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]
        body["candles"] = candles if candles is not None else [
            [1742960700, 730.00, 750.00, 645.00, 706.00, 359100, 5131225],
            [1742964300, 706.00, 760.00, 651.30, 680.50, 246225, 5019500],
        ]
    return body


def standard_body(
    *,
    status: str = "ok",
    code: int = 200,
    message: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The standard envelope: s, code, message and a data wrapper."""
    body: dict[str, Any] = {"s": status, "code": code, "message": message}
    if data is not None:
        body["data"] = data
    return body
