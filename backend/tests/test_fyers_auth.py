"""The login flow: appIdHash, the authorize URL, the state round trip and the code exchange.

Every credential in this file is synthetic. The developer's live credentials live outside the
repository and are never read here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers.auth import (
    DEFAULT_REDIRECT_URI,
    AuthCodeExchangeFailed,
    AuthError,
    STATE_TTL_SECONDS,
    app_id_hash,
    build_authorize_url,
    complete_login,
    exchange_auth_code,
    new_state,
    parse_callback_query,
    parse_redirected_url,
    start_login,
    state_digest,
    state_matches,
)
from expirymanager.brokers.fyers.client import FyersClient
from expirymanager.brokers.fyers.errors import RetryClass
from expirymanager.brokers.fyers.tokens import (
    FyersCredentials,
    InMemoryTokenStore,
    TokenBroker,
)

from tests.fyers_fake_transport import (
    FAKE_APP_ID,
    FAKE_APP_SECRET,
    RecordingTransport,
    json_response,
    token_valid_for,
)

# A JWT shaped access token whose payload decodes to exp 2 January 2026 00:00:00 UTC. The
# signature segment is the literal word signature: nothing verifies it and nothing should.
SYNTHETIC_ACCESS_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9"
    ".eyJpc3MiOiJhcGkuZnllcnMuaW4iLCJleHAiOjE3NjczMTIwMDB9"
    ".signature"
)


class StubCredentials:
    def __init__(self, credentials: FyersCredentials | None) -> None:
        self._credentials = credentials

    def load_active(self) -> FyersCredentials | None:
        return self._credentials


def _credentials() -> FyersCredentials:
    return FyersCredentials(
        credential_id="cred-0001",
        app_id=FAKE_APP_ID,
        app_secret=FAKE_APP_SECRET,
        redirect_uri=DEFAULT_REDIRECT_URI,
    )


# appIdHash


def test_app_id_hash_matches_the_worked_example_in_the_docs() -> None:
    # Section 6 of the reference states that the SHA-256 of app_id:app_secret is this digest. It
    # is the one published vector for this value, and it pins the exact string that is hashed:
    # the two fields joined by a colon, with no separator invented and nothing else appended.
    assert app_id_hash("app_id", "app_secret") == (
        "7c7120d2b5004f8de22d8dc2da0453b4d7e6211e37a4108b8371266ecff00498"
    )


def test_app_id_hash_matches_an_independent_digest_of_a_synthetic_pair() -> None:
    expected = hashlib.sha256(f"{FAKE_APP_ID}:{FAKE_APP_SECRET}".encode("utf-8")).hexdigest()
    assert app_id_hash(FAKE_APP_ID, FAKE_APP_SECRET) == expected
    assert len(expected) == 64
    assert expected == expected.lower()


def test_app_id_hash_is_not_a_bare_concatenation() -> None:
    # Dropping the colon produces a different digest and a -352 that reads like a wrong app id.
    concatenated = hashlib.sha256(f"{FAKE_APP_ID}{FAKE_APP_SECRET}".encode("utf-8")).hexdigest()
    assert app_id_hash(FAKE_APP_ID, FAKE_APP_SECRET) != concatenated


def test_app_id_hash_requires_both_halves() -> None:
    with pytest.raises(AuthError):
        app_id_hash(FAKE_APP_ID, "")
    with pytest.raises(AuthError):
        app_id_hash("", FAKE_APP_SECRET)


# The authorize URL


def test_the_authorize_url_carries_the_four_documented_parameters() -> None:
    url = build_authorize_url(
        client_id=FAKE_APP_ID, redirect_uri=DEFAULT_REDIRECT_URI, state="sample_state"
    )
    split = urlsplit(url)
    assert f"{split.scheme}://{split.netloc}{split.path}" == (
        "https://api-t1.fyers.in/api/v3/generate-authcode"
    )
    query = parse_qs(split.query)
    assert query["client_id"] == [FAKE_APP_ID]
    assert query["redirect_uri"] == [DEFAULT_REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["sample_state"]


def test_the_redirect_uri_is_percent_encoded_in_the_query() -> None:
    url = build_authorize_url(
        client_id=FAKE_APP_ID, redirect_uri=DEFAULT_REDIRECT_URI, state="s"
    )
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Ffyers%2Fcallback" in url


def test_the_registered_redirect_uri_is_the_exact_documented_string() -> None:
    # Fyers matches this character for character. Scheme, host, port and root path all matter.
    # The scheme follows what is registered on the Fyers dashboard, which is http today.
    assert DEFAULT_REDIRECT_URI == "http://127.0.0.1:8000/fyers/callback"


def test_the_authorize_url_refuses_missing_parts() -> None:
    with pytest.raises(AuthError):
        build_authorize_url(client_id="", redirect_uri=DEFAULT_REDIRECT_URI, state="s")
    with pytest.raises(AuthError):
        build_authorize_url(client_id=FAKE_APP_ID, redirect_uri="", state="s")


# The state round trip


def test_the_state_is_unguessable_and_never_repeats() -> None:
    values = {new_state() for _ in range(200)}
    assert len(values) == 200
    assert all(len(value) >= 40 for value in values)


def test_the_state_round_trips_through_the_authorize_url_and_the_callback() -> None:
    request = start_login(_credentials())
    query = parse_qs(urlsplit(request.authorize_url).query)
    returned = query["state"][0]

    # Fyers echoes the state back on the redirect, exactly as it was sent.
    callback = parse_callback_query(f"s=ok&code=200&auth_code=synthetic-code&state={returned}")
    assert callback.state == returned
    assert state_matches(request.state_hash, callback.state)


def test_only_the_state_digest_is_stored() -> None:
    request = start_login(_credentials())
    assert request.state_hash == hashlib.sha256(request.state.encode("utf-8")).digest()
    assert request.state not in repr(request)


def test_a_wrong_state_does_not_match() -> None:
    request = start_login(_credentials())
    assert not state_matches(request.state_hash, "not-the-state")
    assert not state_matches(state_digest("a"), "b")


def test_the_state_expires_ten_minutes_after_it_is_minted() -> None:
    request = start_login(_credentials())
    minted_at = datetime.now(UTC)
    assert 0 < (request.expires_at - minted_at).total_seconds() <= STATE_TTL_SECONDS
    assert STATE_TTL_SECONDS == 600


# The callback


def test_the_redirected_url_is_parsed_for_the_manual_fallback() -> None:
    landed = (
        "https://127.0.0.1:8000/fyers/callback"
        "?s=ok&code=200&message=&auth_code=synthetic-auth-code&state=synthetic-state"
    )
    params = parse_redirected_url(landed)
    assert params.auth_code == "synthetic-auth-code"
    assert params.state == "synthetic-state"
    assert params.status == "ok"
    assert params.code == 200


def test_the_auth_code_stays_out_of_the_callback_repr() -> None:
    params = parse_callback_query("s=ok&auth_code=synthetic-auth-code&state=x")
    assert "synthetic-auth-code" not in repr(params)


def test_a_url_with_no_query_is_refused() -> None:
    with pytest.raises(AuthError):
        parse_redirected_url("https://127.0.0.1:8000/fyers/callback")


def test_a_callback_error_is_parsed_without_an_auth_code() -> None:
    params = parse_callback_query("s=error&code=-352&message=invalid+app+id")
    assert params.auth_code is None
    assert params.code == -352
    assert params.message == "invalid app id"


# The code exchange


def _exchange_transport(response: httpx.Response) -> RecordingTransport:
    return RecordingTransport(lambda _request: response)


async def test_the_exchange_posts_the_three_documented_fields_and_no_pin() -> None:
    transport = _exchange_transport(
        json_response(
            200,
            {
                "s": "ok",
                "code": 200,
                "message": "",
                "access_token": SYNTHETIC_ACCESS_TOKEN,
                "refresh_token": "refresh.token.value",
            },
        )
    )
    async with FyersClient(transport=transport) as client:
        result = await exchange_auth_code(
            client,
            app_id=FAKE_APP_ID,
            app_secret=FAKE_APP_SECRET,
            auth_code="synthetic-auth-code",
        )

    request = transport.last_request
    assert request.method == "POST"
    assert request.url.path == ep.VALIDATE_AUTHCODE.path
    body = json.loads(request.content)
    assert body == {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash(FAKE_APP_ID, FAKE_APP_SECRET),
        "code": "synthetic-auth-code",
    }
    # SEBI discontinued the refresh token flow from 1 April 2026, so there is no pin anywhere.
    assert "pin" not in body
    assert result.access_token == SYNTHETIC_ACCESS_TOKEN
    assert result.refresh_token == "refresh.token.value"


async def test_the_exchange_decodes_the_jwt_expiry_locally() -> None:
    transport = _exchange_transport(
        json_response(
            200,
            {"s": "ok", "code": 200, "message": "", "access_token": SYNTHETIC_ACCESS_TOKEN},
        )
    )
    async with FyersClient(transport=transport) as client:
        result = await exchange_auth_code(
            client, app_id=FAKE_APP_ID, app_secret=FAKE_APP_SECRET, auth_code="code"
        )
    assert result.expires_at is not None
    assert result.expires_at.isoformat() == "2026-01-02T00:00:00+00:00"


async def test_the_exchange_result_keeps_the_token_out_of_its_repr() -> None:
    transport = _exchange_transport(
        json_response(
            200,
            {"s": "ok", "code": 200, "message": "", "access_token": SYNTHETIC_ACCESS_TOKEN},
        )
    )
    async with FyersClient(transport=transport) as client:
        result = await exchange_auth_code(
            client, app_id=FAKE_APP_ID, app_secret=FAKE_APP_SECRET, auth_code="code"
        )
    assert SYNTHETIC_ACCESS_TOKEN not in repr(result)
    assert len(result.fingerprint) == 64


async def test_an_exchange_error_raises_with_its_classification() -> None:
    transport = _exchange_transport(
        json_response(400, {"s": "error", "code": -352, "message": "invalid app id"})
    )
    async with FyersClient(transport=transport) as client:
        with pytest.raises(AuthCodeExchangeFailed) as caught:
            await exchange_auth_code(
                client, app_id=FAKE_APP_ID, app_secret=FAKE_APP_SECRET, auth_code="code"
            )
    assert caught.value.classification.retry_class is RetryClass.FATAL
    # The broker's own message never reaches the caller: it would be an oracle.
    assert "invalid app id" not in str(caught.value)


async def test_an_ok_response_with_no_access_token_is_still_a_failure() -> None:
    transport = _exchange_transport(json_response(200, {"s": "ok", "code": 200, "message": ""}))
    async with FyersClient(transport=transport) as client:
        with pytest.raises(AuthCodeExchangeFailed):
            await exchange_auth_code(
                client, app_id=FAKE_APP_ID, app_secret=FAKE_APP_SECRET, auth_code="code"
            )


async def test_an_empty_auth_code_never_reaches_the_broker() -> None:
    transport = _exchange_transport(json_response(200, {"s": "ok"}))
    async with FyersClient(transport=transport) as client:
        with pytest.raises(AuthError):
            await exchange_auth_code(
                client, app_id=FAKE_APP_ID, app_secret=FAKE_APP_SECRET, auth_code=""
            )
    assert transport.requests == []


# The whole flow


async def test_complete_login_persists_the_token_and_opens_the_auth_gate() -> None:
    credentials = _credentials()
    broker = TokenBroker(
        credentials=StubCredentials(credentials), tokens=InMemoryTokenStore()
    )
    assert not broker.has_valid_token()
    assert not broker.auth_gate.is_set()

    live_token = token_valid_for(days=1)
    transport = _exchange_transport(
        json_response(
            200, {"s": "ok", "code": 200, "message": "", "access_token": live_token}
        )
    )
    async with FyersClient(transport=transport) as client:
        record = await complete_login(client, broker, auth_code="synthetic-auth-code")

    assert record.generation == 1
    assert record.state == "active"
    assert broker.auth_gate.is_set()
    # The record carries a fingerprint and an expiry, and deliberately not the token.
    assert live_token not in repr(record)
    context = await broker.auth_context()
    assert context is not None
    assert context.header() == f"{FAKE_APP_ID}:{live_token}"
