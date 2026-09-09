"""The Fyers endpoint registry and the typed wrappers over it.

One table, one source of truth for three facts that are easy to get wrong in a call site: the path,
whether the call carries an Authorization header, and which of the two response envelopes it
answers with. The historical data endpoints do not use the `data` wrapper and carry no `code` or
`message` on success, so a single parser would either lose the candles or misread an error. The
envelope is a property of the endpoint, declared once here and never guessed at the call site.

Every wrapper is a thin coroutine over `FyersClient`. They exist so a handler names an operation
rather than assembling a path and a parameter dict, which is what keeps the percent encoding and
the envelope choice in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from expirymanager.brokers.fyers.client import CandleResponse, FyersClient, FyersResponse

__all__ = [
    "API_BASE_URL",
    "Envelope",
    "Endpoint",
    "AUTHORIZE",
    "VALIDATE_AUTHCODE",
    "PROFILE",
    "LOGOUT",
    "EXPIRY_DATES",
    "UNDERLYING_SYMBOLS",
    "EXPIRED_HISTORICAL_DATA",
    "HISTORY",
    "QUOTES",
    "OPTIONS_CHAIN",
    "FUTURES_CHAIN",
    "ENDPOINTS",
    "DATE_FORMAT_YMD",
    "DATE_FORMAT_EPOCH",
    "expiry_dates",
    "underlying_symbols",
    "expired_historical_data",
    "history",
    "quotes",
    "options_chain",
    "profile",
]

# The one host this product talks to. Not configurable: a second base URL is a way to send a live
# token somewhere it was not issued for.
API_BASE_URL = "https://api-t1.fyers.in"

# date_format flags, from section 24. Every call in this product sends yyyy-mm-dd, because an
# epoch boundary that silently lands in the wrong timezone is the classic off by one day bug.
DATE_FORMAT_YMD = "1"
DATE_FORMAT_EPOCH = "0"


class Envelope(StrEnum):
    """Which response shape an endpoint answers with."""

    # s, code, message and one payload key, usually `data`.
    STANDARD = "standard"
    # s, symbol, resolution, columns, candles, schema_version. No wrapper, no code, no message on
    # success. `s` can additionally be `no_data`, which is a success with zero rows.
    CANDLES = "candles"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One callable Fyers operation."""

    name: str
    method: str
    path: str
    envelope: Envelope
    authenticated: bool = True
    # The login exchange must not queue behind the outbound limiter. If it did, a pipeline parked
    # in paused_auth could never be unparked, because the limiter blocks while the pipeline is
    # paused and the only thing that can unpause it is a successful login.
    governed: bool = True
    # Whether the response is documented to carry a `columns` array. The live history endpoint is
    # not, so its column names come from the documented order instead.
    returns_columns: bool = False


AUTHORIZE = Endpoint(
    name="generate-authcode",
    method="GET",
    path="/api/v3/generate-authcode",
    envelope=Envelope.STANDARD,
    authenticated=False,
    governed=False,
)

VALIDATE_AUTHCODE = Endpoint(
    name="validate-authcode",
    method="POST",
    path="/api/v3/validate-authcode",
    envelope=Envelope.STANDARD,
    authenticated=False,
    governed=False,
)

PROFILE = Endpoint(
    name="profile",
    method="GET",
    path="/api/v3/profile",
    envelope=Envelope.STANDARD,
)

LOGOUT = Endpoint(
    name="logout",
    method="POST",
    path="/api/v3/logout",
    envelope=Envelope.STANDARD,
)

EXPIRY_DATES = Endpoint(
    name="expiry-dates",
    method="GET",
    path="/data/history/fno/expired/expiry-dates",
    envelope=Envelope.STANDARD,
)

UNDERLYING_SYMBOLS = Endpoint(
    name="underlying-symbols",
    method="GET",
    path="/data/history/fno/expired/underlying-symbols",
    envelope=Envelope.STANDARD,
)

EXPIRED_HISTORICAL_DATA = Endpoint(
    name="expired-historical-data",
    method="GET",
    path="/data/history/fno/expired/historical-data",
    envelope=Envelope.CANDLES,
    returns_columns=True,
)

HISTORY = Endpoint(
    name="history",
    method="GET",
    path="/data/history",
    envelope=Envelope.CANDLES,
    returns_columns=False,
)

QUOTES = Endpoint(
    name="quotes",
    method="GET",
    path="/data/quotes",
    envelope=Envelope.STANDARD,
)

OPTIONS_CHAIN = Endpoint(
    name="options-chain-v3",
    method="GET",
    path="/data/options-chain-v3",
    envelope=Envelope.STANDARD,
)

FUTURES_CHAIN = Endpoint(
    name="futures-chain",
    method="GET",
    path="/data/futures-chain",
    envelope=Envelope.STANDARD,
)

ENDPOINTS: dict[str, Endpoint] = {
    endpoint.name: endpoint
    for endpoint in (
        AUTHORIZE,
        VALIDATE_AUTHCODE,
        PROFILE,
        LOGOUT,
        EXPIRY_DATES,
        UNDERLYING_SYMBOLS,
        EXPIRED_HISTORICAL_DATA,
        HISTORY,
        QUOTES,
        OPTIONS_CHAIN,
        FUTURES_CHAIN,
    )
}


def _ymd(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


async def expiry_dates(
    client: FyersClient,
    *,
    symbol: str,
    range_from: date | str,
    range_to: date | str,
) -> FyersResponse:
    """Available futures and options expiries for an underlying, up to 366 days per call."""
    return await client.request_standard(
        EXPIRY_DATES,
        params={
            "symbol": symbol,
            "range_from": _ymd(range_from),
            "range_to": _ymd(range_to),
            "date_format": DATE_FORMAT_YMD,
        },
    )


async def underlying_symbols(
    client: FyersClient,
    *,
    symbol: str,
    expiry_date: date | str,
) -> FyersResponse:
    """Every expired contract symbol for one underlying and one expiry."""
    return await client.request_standard(
        UNDERLYING_SYMBOLS,
        params={"symbol": symbol, "expiry_date": _ymd(expiry_date)},
    )


async def expired_historical_data(
    client: FyersClient,
    *,
    symbol: str,
    resolution: str,
    range_from: date | str,
    range_to: date | str,
    include_oi: bool = True,
) -> CandleResponse:
    """Candles for one expired contract. `range_to` is inclusive of that date."""
    params: dict[str, Any] = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": DATE_FORMAT_YMD,
        "range_from": _ymd(range_from),
        "range_to": _ymd(range_to),
    }
    if include_oi:
        params["include_oi"] = "1"
    return await client.request_candles(EXPIRED_HISTORICAL_DATA, params=params)


async def history(
    client: FyersClient,
    *,
    symbol: str,
    resolution: str,
    range_from: date | str,
    range_to: date | str,
    include_oi: bool = False,
    cont_flag: bool = False,
) -> CandleResponse:
    """Candles for a live symbol. This is the spot series, so it has no expired counterpart."""
    params: dict[str, Any] = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": DATE_FORMAT_YMD,
        "range_from": _ymd(range_from),
        "range_to": _ymd(range_to),
    }
    if include_oi:
        # Spelled oi_flag on this endpoint and include_oi on the expired one. The inconsistency is
        # the broker's; hiding it here is the point of the wrapper.
        params["oi_flag"] = "1"
    if cont_flag:
        params["cont_flag"] = "1"
    return await client.request_candles(HISTORY, params=params)


async def quotes(client: FyersClient, *, symbols: list[str] | str) -> FyersResponse:
    """Live quotes for up to the documented batch size, comma separated."""
    joined = symbols if isinstance(symbols, str) else ",".join(symbols)
    return await client.request_standard(QUOTES, params={"symbols": joined})


async def options_chain(
    client: FyersClient,
    *,
    symbol: str,
    strike_count: int,
    greeks: bool = False,
) -> FyersResponse:
    """The live option chain snapshot for one underlying."""
    params: dict[str, Any] = {"symbol": symbol, "strikecount": str(strike_count)}
    if greeks:
        params["greeks"] = "1"
    return await client.request_standard(OPTIONS_CHAIN, params=params)


async def profile(client: FyersClient) -> FyersResponse:
    """The cheapest authenticated call. Used to prove a stored token still works."""
    return await client.request_standard(PROFILE)
