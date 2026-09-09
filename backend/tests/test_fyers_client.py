"""The HTTP client: headers, percent encoding and both response envelopes.

Every request in this file goes through httpx.MockTransport. Nothing here touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers.client import (
    AuthContext,
    AuthTokenUnavailable,
    FyersClient,
    MalformedResponse,
    authorization_header,
    encode_query,
    parse_candle_envelope,
    parse_standard_envelope,
)
from expirymanager.brokers.fyers.errors import RetryClass
from expirymanager.version import user_agent

from tests.fyers_fake_transport import (
    FAKE_ACCESS_TOKEN,
    FAKE_APP_ID,
    RecordingTransport,
    always,
    candle_body,
    json_response,
    standard_body,
)


class StubTokens:
    def __init__(self, context: AuthContext | None) -> None:
        self._context = context
        self.calls = 0

    async def auth_context(self) -> AuthContext | None:
        self.calls += 1
        return self._context


def _client(transport: httpx.MockTransport, *, tokens: object | None = None) -> FyersClient:
    return FyersClient(
        tokens=tokens if tokens is not None else StubTokens(
            AuthContext(app_id=FAKE_APP_ID, access_token=FAKE_ACCESS_TOKEN)
        ),
        transport=transport,
    )


# Header format


def test_authorization_header_is_app_id_colon_token_with_no_bearer_prefix() -> None:
    header = authorization_header("XC4XXXXM-100", "token-value")
    assert header == "XC4XXXXM-100:token-value"
    assert "Bearer" not in header


async def test_every_request_carries_the_documented_headers() -> None:
    transport = always(200, standard_body(data={"symbol": "SBIN"}))
    async with _client(transport) as client:
        await ep.expiry_dates(
            client, symbol="NSE:SBIN-EQ", range_from="2025-01-01", range_to="2025-03-31"
        )
    request = transport.last_request
    assert request.headers["Authorization"] == f"{FAKE_APP_ID}:{FAKE_ACCESS_TOKEN}"
    assert request.headers["User-Agent"] == user_agent()


async def test_an_unauthenticated_endpoint_sends_no_authorization_header() -> None:
    transport = always(200, {"s": "ok", "code": 200, "message": "", "access_token": "a.b.c"})
    tokens = StubTokens(None)
    async with _client(transport, tokens=tokens) as client:
        await client.request_standard(ep.VALIDATE_AUTHCODE, json_body={"grant_type": "x"})
    assert "Authorization" not in transport.last_request.headers
    # The token supplier is never even asked, so a login works with no token in hand.
    assert tokens.calls == 0


async def test_a_missing_token_refuses_before_the_request_leaves() -> None:
    transport = always(200, standard_body())
    async with _client(transport, tokens=StubTokens(None)) as client:
        with pytest.raises(AuthTokenUnavailable):
            await ep.profile(client)
    assert transport.requests == []


def test_the_access_token_stays_out_of_the_auth_context_repr() -> None:
    context = AuthContext(app_id=FAKE_APP_ID, access_token="super-secret-token")
    assert "super-secret-token" not in repr(context)


# Percent encoding


def test_an_ampersand_symbol_is_percent_encoded() -> None:
    # The documented cause of error -300 is a special character that was not encoded.
    assert encode_query({"symbol": "NSE:M&M-EQ"}) == "symbol=NSE:M%26M-EQ"


def test_the_exchange_colon_stays_readable() -> None:
    assert encode_query({"symbol": "NSE:SBIN-EQ"}) == "symbol=NSE:SBIN-EQ"


def test_a_none_value_is_dropped_rather_than_sent_as_the_string_none() -> None:
    assert encode_query({"symbol": "NSE:SBIN-EQ", "include_oi": None}) == "symbol=NSE:SBIN-EQ"


async def test_the_encoded_query_survives_onto_the_wire() -> None:
    transport = always(200, candle_body())
    async with _client(transport) as client:
        await ep.expired_historical_data(
            client,
            symbol="NSE:M&M25MARFUT",
            resolution="60",
            range_from="2025-03-26",
            range_to="2025-03-27",
        )
    url = str(transport.last_request.url)
    assert "M%26M25MARFUT" in url
    assert "include_oi=1" in url
    assert "date_format=1" in url


# The standard envelope


def test_the_standard_parser_reads_s_code_message_and_data() -> None:
    raw = json.dumps(
        {
            "code": 200,
            "data": {"symbol": "SBIN", "expiry_dates": {"futures": ["2025-01-30"]}},
            "message": "",
            "s": "ok",
        }
    ).encode()
    parsed = parse_standard_envelope(raw, http_status=200, endpoint="expiry-dates")
    assert parsed.ok
    assert parsed.code == 200
    assert parsed.data["expiry_dates"]["futures"] == ["2025-01-30"]
    assert parsed.classification() is None


def test_the_standard_parser_reads_the_quotes_payload_key() -> None:
    raw = json.dumps({"s": "ok", "code": 200, "d": [{"n": "NSE:SBIN-EQ"}]}).encode()
    parsed = parse_standard_envelope(raw, http_status=200, endpoint="quotes")
    assert parsed.data == [{"n": "NSE:SBIN-EQ"}]


def test_a_standard_error_carries_its_classification() -> None:
    raw = json.dumps({"s": "error", "code": -300, "message": "invalid symbol"}).encode()
    parsed = parse_standard_envelope(raw, http_status=400, endpoint="expiry-dates")
    classification = parsed.classification()
    assert classification is not None
    assert classification.retry_class is RetryClass.FATAL
    assert classification.check_symbol_encoding


def test_a_non_2xx_body_without_s_is_not_read_as_a_success() -> None:
    parsed = parse_standard_envelope(b"{}", http_status=500, endpoint="profile")
    assert not parsed.ok


def test_a_body_that_is_not_json_raises() -> None:
    with pytest.raises(MalformedResponse):
        parse_standard_envelope(b"<html>gateway timeout</html>", http_status=504, endpoint="x")


# The candle envelope


def test_the_candle_parser_reads_the_flat_envelope() -> None:
    raw = json.dumps(candle_body()).encode()
    parsed = parse_candle_envelope(raw, http_status=200, endpoint="expired-historical-data")
    assert parsed.ok
    assert parsed.symbol == "NSE:NIFTY25MAR23000CE"
    assert parsed.resolution == "60"
    assert parsed.schema_version == 1
    assert parsed.row_count == 2
    assert parsed.has_open_interest
    assert parsed.columns_from_response
    # No data wrapper and no code or message on success. The standard parser would find neither.
    assert parsed.code is None
    assert parsed.message == ""


def test_the_standard_parser_would_lose_the_candles() -> None:
    # The reason two parsers exist rather than one.
    raw = json.dumps(candle_body()).encode()
    standard = parse_standard_envelope(raw, http_status=200, endpoint="expired-historical-data")
    assert standard.data is None


def test_no_data_is_parsed_as_an_empty_success() -> None:
    raw = json.dumps(candle_body(status="no_data")).encode()
    parsed = parse_candle_envelope(raw, http_status=200, endpoint="expired-historical-data")
    assert parsed.is_no_data
    assert not parsed.ok
    assert parsed.row_count == 0
    classification = parsed.classification()
    assert classification is not None
    assert classification.retry_class is RetryClass.EMPTY
    assert classification.is_success


def test_columns_come_from_the_response_and_never_from_a_fixed_index() -> None:
    # A reordered columns array with open interest ahead of volume must still map correctly.
    reordered = ["timestamp", "open", "high", "low", "close", "open_interest", "volume"]
    body = candle_body(
        columns=reordered,
        candles=[[1742960700, 730.0, 750.0, 645.0, 706.0, 5131225, 359100]],
    )
    parsed = parse_candle_envelope(
        json.dumps(body).encode(), http_status=200, endpoint="expired-historical-data"
    )
    assert parsed.columns == tuple(reordered)
    row = parsed.candles[0]
    assert row[parsed.columns.index("volume")] == 359100
    assert row[parsed.columns.index("open_interest")] == 5131225


def test_a_future_column_is_carried_through_untouched() -> None:
    # include_greeks will append columns. Nothing here may care how many there are.
    body = candle_body(
        columns=["timestamp", "open", "high", "low", "close", "volume", "open_interest", "delta"],
        candles=[[1742960700, 730.0, 750.0, 645.0, 706.0, 359100, 5131225, 0.42]],
    )
    parsed = parse_candle_envelope(
        json.dumps(body).encode(), http_status=200, endpoint="expired-historical-data"
    )
    assert parsed.columns[-1] == "delta"
    assert parsed.candles[0][parsed.columns.index("delta")] == 0.42


def test_candles_without_a_columns_array_are_refused_where_one_is_documented() -> None:
    body = {"s": "ok", "symbol": "NSE:X", "resolution": "60", "candles": [[1, 2, 3, 4, 5, 6]]}
    with pytest.raises(MalformedResponse):
        parse_candle_envelope(
            json.dumps(body).encode(),
            http_status=200,
            endpoint="expired-historical-data",
            require_columns=True,
        )


def test_the_live_history_endpoint_falls_back_to_the_documented_order() -> None:
    # /data/history does not document a columns array, so the order is the documented one and the
    # response says so.
    body = {"s": "ok", "candles": [[1621814400, 417.0, 419.2, 405.3, 412.05, 142964052]]}
    parsed = parse_candle_envelope(
        json.dumps(body).encode(), http_status=200, endpoint="history", require_columns=False
    )
    assert parsed.columns == ("timestamp", "open", "high", "low", "close", "volume")
    assert not parsed.columns_from_response
    assert not parsed.has_open_interest


def test_the_live_history_fallback_detects_open_interest_by_row_width() -> None:
    body = {"s": "ok", "candles": [[1621814400, 417.0, 419.2, 405.3, 412.05, 142964052, 5131225]]}
    parsed = parse_candle_envelope(
        json.dumps(body).encode(), http_status=200, endpoint="history", require_columns=False
    )
    assert parsed.has_open_interest


def test_a_candle_error_classifies_from_its_code() -> None:
    body = {"s": "error", "code": -8, "message": "token expired"}
    parsed = parse_candle_envelope(
        json.dumps(body).encode(), http_status=401, endpoint="expired-historical-data"
    )
    classification = parsed.classification()
    assert classification is not None
    assert classification.is_auth_failure


def test_the_payload_hash_is_stable_and_the_byte_count_is_real() -> None:
    raw = json.dumps(candle_body()).encode()
    first = parse_candle_envelope(raw, http_status=200, endpoint="expired-historical-data")
    second = parse_candle_envelope(raw, http_status=200, endpoint="expired-historical-data")
    assert first.payload_sha256 == second.payload_sha256
    assert first.response_bytes == len(raw)


# Envelope routing


async def test_asking_for_the_wrong_envelope_is_refused() -> None:
    transport = always(200, standard_body())
    async with _client(transport) as client:
        with pytest.raises(ValueError):
            await client.request_candles(ep.EXPIRY_DATES)
        with pytest.raises(ValueError):
            await client.request_standard(ep.EXPIRED_HISTORICAL_DATA)


async def test_each_endpoint_is_routed_to_its_declared_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/historical-data"):
            return json_response(200, candle_body())
        return json_response(200, standard_body(data={}))

    transport = RecordingTransport(handler)
    async with _client(transport) as client:
        await ep.expiry_dates(
            client, symbol="NSE:SBIN-EQ", range_from="2025-01-01", range_to="2025-03-31"
        )
        await ep.underlying_symbols(client, symbol="NSE:SBIN-EQ", expiry_date="2025-03-27")
        await ep.expired_historical_data(
            client,
            symbol="NSE:SBIN25MAR320CE",
            resolution="60",
            range_from="2025-03-26",
            range_to="2025-03-27",
        )
        await ep.profile(client)
    paths = [request.url.path for request in transport.requests]
    assert paths == [
        "/data/history/fno/expired/expiry-dates",
        "/data/history/fno/expired/underlying-symbols",
        "/data/history/fno/expired/historical-data",
        "/api/v3/profile",
    ]


# Governor integration


class SpyGovernor:
    def __init__(self) -> None:
        self.slots: list[str] = []
        self.rate_limits: list[tuple[str, int | None, int | None]] = []

    def slot(self, endpoint: str):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _slot():
            self.slots.append(endpoint)
            yield

        return _slot()

    async def note_rate_limited(self, *, endpoint, http_status, code) -> None:
        self.rate_limits.append((endpoint, http_status, code))


async def test_a_governed_call_takes_a_slot_and_a_login_does_not() -> None:
    governor = SpyGovernor()
    transport = always(200, standard_body(data={}))
    client = FyersClient(
        tokens=StubTokens(AuthContext(app_id=FAKE_APP_ID, access_token=FAKE_ACCESS_TOKEN)),
        governor=governor,
        transport=transport,
    )
    async with client:
        await ep.profile(client)
        await client.request_standard(ep.VALIDATE_AUTHCODE, json_body={"grant_type": "x"})
    # The login exchange must bypass the limiter, or a pipeline parked in paused_auth could never
    # be unparked: the limiter blocks while paused and only a login can unpause it.
    assert governor.slots == ["profile"]


async def test_a_rate_limited_response_is_reported_to_the_governor() -> None:
    governor = SpyGovernor()
    transport = always(429, {"s": "error", "code": -429, "message": "rate limit"})
    client = FyersClient(
        tokens=StubTokens(AuthContext(app_id=FAKE_APP_ID, access_token=FAKE_ACCESS_TOKEN)),
        governor=governor,
        transport=transport,
    )
    async with client:
        response = await ep.profile(client)
    assert response.classification().retry_class is RetryClass.RATE_LIMITED
    assert governor.rate_limits == [("profile", 429, -429)]


async def test_an_ordinary_error_is_not_reported_as_a_rate_limit() -> None:
    governor = SpyGovernor()
    transport = always(400, {"s": "error", "code": -50, "message": "invalid params"})
    client = FyersClient(
        tokens=StubTokens(AuthContext(app_id=FAKE_APP_ID, access_token=FAKE_ACCESS_TOKEN)),
        governor=governor,
        transport=transport,
    )
    async with client:
        await ep.profile(client)
    assert governor.rate_limits == []


async def test_a_transport_failure_propagates_for_the_caller_to_classify() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(RecordingTransport(handler)) as client:
        with pytest.raises(httpx.ConnectError):
            await ep.profile(client)
