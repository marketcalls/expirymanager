"""W16: the broker credential routes and the root OAuth callback.

The whole cold-start-to-logged-in path runs here against a real application: real SQLite, real
migrations, a real key hierarchy doing real AES-256-GCM, the real middleware stack, and a fake
HTTP transport standing in for Fyers. The only thing that is not real is the network.

Every credential in this file is synthetic. The developer's live credentials live outside the
repository and are never read by application code or by a test.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from expirymanager.api import oauth_callback
from expirymanager.api.v1 import broker as broker_routes
from expirymanager.app import create_app
from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers.auth import DEFAULT_REDIRECT_URI, app_id_hash, state_digest
from expirymanager.brokers.fyers.client import FyersClient
from expirymanager.db import sqlite as sqlite_module
from expirymanager.security.csrf import DEFAULT_EXEMPT_PATHS
from expirymanager.security.redaction import CallbackQueryFilter, RedactionFilter
from expirymanager.security.sessions import CSRF_COOKIE_NAME

from tests.fyers_fake_transport import (
    FAKE_APP_ID,
    FAKE_APP_SECRET,
    RecordingTransport,
    json_response,
    token_valid_for,
)

BASE_URL = "https://127.0.0.1:8000"

USERNAME = "operator"
PASSCODE = "correct-horse-battery-staple"

SYNTHETIC_AUTH_CODE = "synthetic-auth-code-not-a-real-value"
SYNTHETIC_LABEL = "Primary"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "expirymanager-home"
    monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
    yield root
    sqlite_module.dispose_engine()


@pytest.fixture
def app(data_dir):
    application = create_app(root=data_dir, serve_static=False)
    yield application
    sqlite_module.dispose_engine()


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    return {"X-CSRF-Token": token} if token else {}


def second_browser(app) -> TestClient:
    """A second browser against the SAME running application.

    Deliberately not used as a context manager: entering a TestClient runs the lifespan, and
    leaving it runs the shutdown, which would tear down the engine the first client is still
    using. The app is already started by the fixture, so a bare client shares its state.
    """
    return TestClient(app, base_url=BASE_URL)


def sign_in(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/setup", json={"username": USERNAME, "password": PASSCODE}
    )
    assert response.status_code == 200


def save_credentials(client: TestClient, **overrides):
    body = {
        "label": SYNTHETIC_LABEL,
        "app_id": FAKE_APP_ID,
        "app_secret": FAKE_APP_SECRET,
        "redirect_uri": DEFAULT_REDIRECT_URI,
        "plan": "standard",
    }
    body.update(overrides)
    return client.post("/api/v1/broker/fyers/credentials", json=body, headers=csrf(client))


@pytest.fixture
def connected_client(client):
    """Signed in, with credentials stored. One step short of a broker login."""
    sign_in(client)
    assert save_credentials(client).status_code == 200
    return client


class FakeFyers:
    """A transport that answers validate-authcode and expiry-dates, and remembers the requests."""

    def __init__(self, app, *, access_token: str | None = None, exchange_response=None) -> None:
        self.access_token = access_token or token_valid_for(days=1)
        self.exchange_response = exchange_response
        self.transport = RecordingTransport(self._handle)
        services = app.state.services
        self.previous = services.fyers_client
        services.fyers_client = FyersClient(
            tokens=services.token_broker,
            governor=services.governor,
            transport=self.transport,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == ep.VALIDATE_AUTHCODE.path:
            if self.exchange_response is not None:
                return self.exchange_response
            return json_response(
                200,
                {
                    "s": "ok",
                    "code": 200,
                    "message": "",
                    "access_token": self.access_token,
                },
            )
        if request.url.path == ep.EXPIRY_DATES.path:
            return json_response(
                200,
                {"s": "ok", "code": 200, "message": "", "data": {"expiryData": []}},
            )
        return json_response(404, {"s": "error", "code": -99, "message": "no such endpoint"})

    @property
    def exchange_requests(self) -> list[httpx.Request]:
        return [r for r in self.transport.requests if r.url.path == ep.VALIDATE_AUTHCODE.path]


@pytest.fixture
def fyers(app):
    return FakeFyers(app)


def start_login(client: TestClient) -> str:
    """Run `POST /broker/fyers/connect` and return the raw state it minted."""
    response = client.post("/api/v1/broker/fyers/connect", headers=csrf(client))
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["authorize_url"]).query)
    return query["state"][0]


def callback(client: TestClient, *, state: str | None, auth_code: str | None = None, **extra):
    params: dict[str, str] = {"s": "ok", "code": "200"}
    if auth_code is not None:
        params["auth_code"] = auth_code
    if state is not None:
        params["state"] = state
    params.update(extra)
    return client.get("/fyers/callback", params=params, follow_redirects=False)


# The test client is itself an httpx client, and httpx logs the request line it just made with
# the URL as an httpx.URL object rather than as a string. Those records are the test harness
# describing its own call, not this application logging anything, so they are excluded here. The
# equivalent production record is the uvicorn access line, which is formatted with string
# arguments and is asserted separately in TestCallbackLogging.
_HARNESS_LOGGERS = ("httpx", "httpcore")


def filtered_log(records) -> str:
    """Every record this application emitted, put through the two filters it installs.

    `caplog` attaches its own handler, so the filters that `configure_logging` puts on the real
    root handlers never run against it. Applying them by hand here is what makes the assertion
    mean what it says rather than testing an unfiltered stream.
    """
    scrubbers = (RedactionFilter(), CallbackQueryFilter())
    rendered: list[str] = []
    for record in records:
        if record.name.split(".")[0] in _HARNESS_LOGGERS:
            continue
        for scrubber in scrubbers:
            scrubber.filter(record)
        rendered.append(record.getMessage() + str(record.__dict__))
    return "\n".join(rendered)


def token_rows(app):
    with app.state.services.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT token_id, access_token_enc, generation, state, token_fingerprint "
                "FROM broker_token ORDER BY generation"
            )
        ).fetchall()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_status_before_anything_is_saved(self, client):
        sign_in(client)

        body = client.get("/api/v1/broker/fyers").json()

        assert body["credential_id"] is None
        assert body["app_secret_configured"] is False
        assert body["connected"] is False
        assert body["token_state"] == "none"
        assert body["redirect_uri"] == DEFAULT_REDIRECT_URI

    def test_saving_credentials_reports_configured_and_returns_no_secret(self, client):
        sign_in(client)

        response = save_credentials(client)

        assert response.status_code == 200
        body = response.json()
        assert body["app_secret_configured"] is True
        assert body["app_id"] == FAKE_APP_ID
        assert body["label"] == SYNTHETIC_LABEL
        assert FAKE_APP_SECRET not in response.text
        # Not even a mask. A mask is an oracle and the frontend would round-trip it back on save.
        assert "app_secret" not in body

    def test_no_route_ever_returns_the_secret(self, client):
        sign_in(client)
        save_credentials(client)

        for path in ("/api/v1/broker/fyers", "/api/v1/bootstrap"):
            assert FAKE_APP_SECRET not in client.get(path).text

    def test_the_secret_is_encrypted_at_rest_and_decrypts_back(self, client, app):
        sign_in(client)
        save_credentials(client)

        with app.state.services.engine.connect() as connection:
            row = connection.execute(
                text("SELECT credential_id, app_secret_enc, key_ver FROM broker_credential")
            ).first()
        assert FAKE_APP_SECRET.encode() not in bytes(row[1])
        assert len(bytes(row[1])) > len(FAKE_APP_SECRET)
        assert int(row[2]) >= 1

        credentials = app.state.services.token_broker.credentials()
        assert credentials.app_secret == FAKE_APP_SECRET

    def test_there_is_no_pin_field_anywhere_in_the_surface(self, client):
        sign_in(client)
        save_credentials(client)

        body = client.get("/api/v1/broker/fyers").json()
        assert "pin" not in json.dumps(body)

        rejected = client.post(
            "/api/v1/broker/fyers/credentials",
            json={
                "label": SYNTHETIC_LABEL,
                "app_id": FAKE_APP_ID,
                "app_secret": FAKE_APP_SECRET,
                "redirect_uri": DEFAULT_REDIRECT_URI,
                "plan": "standard",
                "pin": "1234",
            },
            headers=csrf(client),
        )
        assert rejected.status_code == 422

    def test_a_non_https_redirect_uri_is_refused(self, client):
        sign_in(client)

        response = save_credentials(client, redirect_uri="http://127.0.0.1:8000/fyers/callback")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == broker_routes.CODE_INVALID_REDIRECT_URI
        assert DEFAULT_REDIRECT_URI in response.json()["error"]["message"]

    def test_an_app_id_that_carries_the_secret_is_refused(self, client):
        sign_in(client)

        response = save_credentials(client, app_id=f"{FAKE_APP_ID}:{FAKE_APP_SECRET}")

        assert response.status_code == 422
        assert FAKE_APP_SECRET not in response.text

    def test_saving_again_updates_one_row_and_revokes_the_existing_token(
        self, connected_client, app, fyers
    ):
        state = start_login(connected_client)
        assert callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)
        assert app.state.services.token_broker.has_valid_token()

        assert save_credentials(connected_client, label="Replaced").status_code == 200

        with app.state.services.engine.connect() as connection:
            rows = connection.execute(text("SELECT label FROM broker_credential")).fetchall()
        assert [row[0] for row in rows] == ["Replaced"]
        assert app.state.services.token_broker.has_valid_token() is False
        assert all(bytes(row[1]) == b"" for row in token_rows(app))

    def test_the_plan_reaches_the_governor_settings(self, client, app):
        sign_in(client)

        save_credentials(client, plan="prime")

        assert app.state.services.settings.get_str("plan_tier") == "prime"

    def test_credentials_require_a_session(self, client):
        sign_in(client)
        headers = csrf(client)
        client.cookies.clear()

        assert save_credentials(client).status_code in (401, 403)
        assert client.get("/api/v1/broker/fyers").status_code == 401
        assert headers  # the token existed; it is the session that is missing


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


class TestConnect:
    def test_it_builds_the_documented_authorize_url(self, connected_client):
        response = connected_client.post(
            "/api/v1/broker/fyers/connect", headers=csrf(connected_client)
        )

        assert response.status_code == 200
        url = response.json()["authorize_url"]
        assert url.startswith(f"{ep.API_BASE_URL}{ep.AUTHORIZE.path}?")
        query = parse_qs(urlsplit(url).query)
        assert query["client_id"] == [FAKE_APP_ID]
        assert query["redirect_uri"] == [DEFAULT_REDIRECT_URI]
        assert query["response_type"] == ["code"]
        assert len(query["state"][0]) >= 43
        assert response.json()["state_expires_at"]

    def test_only_the_digest_of_the_state_is_stored(self, connected_client, app):
        state = start_login(connected_client)

        with app.state.services.engine.connect() as connection:
            rows = connection.execute(text("SELECT * FROM oauth_state")).fetchall()
        assert len(rows) == 1
        for value in rows[0]:
            assert state not in str(value)
        with app.state.services.engine.connect() as connection:
            stored = connection.execute(text("SELECT state_hash FROM oauth_state")).scalar()
        assert bytes(stored) == state_digest(state)

    def test_the_state_is_bound_to_this_session(self, connected_client, app):
        start_login(connected_client)

        with app.state.services.engine.connect() as connection:
            row = connection.execute(
                text("SELECT session_id_hash, used_at FROM oauth_state")
            ).first()
        assert row[1] is None
        assert len(bytes(row[0])) == 32

    def test_connect_without_credentials_is_a_named_400(self, client):
        sign_in(client)

        response = client.post("/api/v1/broker/fyers/connect", headers=csrf(client))

        assert response.status_code == 400
        assert response.json()["error"]["code"] == broker_routes.CODE_NO_CREDENTIALS

    def test_a_second_connect_prunes_the_consumed_row(self, connected_client, app, fyers):
        first = start_login(connected_client)
        callback(connected_client, state=first, auth_code=SYNTHETIC_AUTH_CODE)

        start_login(connected_client)

        with app.state.services.engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM oauth_state")).scalar()
        assert count == 1


# ---------------------------------------------------------------------------
# The callback, mounted at the root
# ---------------------------------------------------------------------------


class TestCallbackMounting:
    def test_it_is_served_at_the_registered_path_and_not_under_the_api_prefix(
        self, connected_client, fyers
    ):
        assert oauth_callback.CALLBACK_PATH == "/fyers/callback"
        assert DEFAULT_REDIRECT_URI == f"{BASE_URL}{oauth_callback.CALLBACK_PATH}"

        assert connected_client.get("/api/v1/fyers/callback").status_code == 404

    def test_it_is_the_one_csrf_exempt_path(self):
        assert DEFAULT_EXEMPT_PATHS == (oauth_callback.CALLBACK_PATH,)

    def test_a_cross_site_navigation_reaches_it(self, connected_client, fyers):
        """SameSite=Lax is what carries the cookie on this navigation. Strict would not."""
        state = start_login(connected_client)

        response = connected_client.get(
            "/fyers/callback",
            params={"s": "ok", "code": "200", "auth_code": SYNTHETIC_AUTH_CODE, "state": state},
            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == oauth_callback.SUCCESS_REDIRECT

    def test_the_response_is_not_cacheable(self, connected_client, fyers):
        state = start_login(connected_client)

        response = callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        assert "no-store" in response.headers["cache-control"]
        assert response.headers["referrer-policy"] == "no-referrer"


class TestCallbackHappyPath:
    def test_it_exchanges_the_code_and_redirects_to_a_clean_url(
        self, connected_client, app, fyers
    ):
        state = start_login(connected_client)

        response = callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        assert response.status_code == 303
        location = response.headers["location"]
        assert location == "/settings?broker=connected"
        # The whole point of the 303: the auth code must not survive into browser history.
        assert SYNTHETIC_AUTH_CODE not in location
        assert SYNTHETIC_AUTH_CODE not in response.text

    def test_the_exchange_sends_exactly_the_three_documented_fields(
        self, connected_client, fyers
    ):
        state = start_login(connected_client)

        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        assert len(fyers.exchange_requests) == 1
        body = json.loads(fyers.exchange_requests[0].content)
        assert body == {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash(FAKE_APP_ID, FAKE_APP_SECRET),
            "code": SYNTHETIC_AUTH_CODE,
        }
        assert "pin" not in body

    def test_the_token_is_stored_encrypted_and_the_gate_opens(
        self, connected_client, app, fyers
    ):
        state = start_login(connected_client)

        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        broker = app.state.services.token_broker
        assert broker.has_valid_token() is True
        assert broker.auth_gate.is_set() is True
        rows = token_rows(app)
        assert len(rows) == 1
        assert fyers.access_token.encode() not in bytes(rows[0][1])
        assert rows[0][3] == "active"
        assert rows[0][2] == 1

    def test_the_status_route_reports_the_connection(self, connected_client, fyers):
        state = start_login(connected_client)
        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        body = connected_client.get("/api/v1/broker/fyers").json()

        assert body["connected"] is True
        assert body["token_state"] == "active"
        assert body["token_expires_at"]
        assert len(body["token_fingerprint"]) == 8
        assert fyers.access_token not in json.dumps(body)

    def test_bootstrap_flips_to_connected(self, connected_client, fyers):
        state = start_login(connected_client)
        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        body = connected_client.get("/api/v1/bootstrap").json()

        assert body["broker_connected"] is True
        assert body["needs_reauth"] is False

    def test_the_state_row_is_marked_used(self, connected_client, app, fyers):
        state = start_login(connected_client)

        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        with app.state.services.engine.connect() as connection:
            used_at = connection.execute(text("SELECT used_at FROM oauth_state")).scalar()
        assert used_at is not None

    def test_parked_jobs_move_back_to_queued(self, connected_client, app, fyers):
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job (job_id, kind, status, params_json, created_at, "
                    "block_reason) VALUES ('job-1', 'candle_backfill', 'blocked_auth', '{}', "
                    "'2026-01-01T00:00:00+00:00', 'awaiting authentication')"
                )
            )
        state = start_login(connected_client)

        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        with app.state.services.engine.connect() as connection:
            row = connection.execute(
                text("SELECT status, block_reason FROM job WHERE job_id = 'job-1'")
            ).first()
        assert row[0] == "queued"
        assert row[1] is None


class TestCallbackRejection:
    def _failed(self, response, reason: str) -> None:
        assert response.status_code == 303
        assert response.headers["location"] == oauth_callback.failure_url(reason)

    def test_a_replayed_state_is_rejected_and_stores_no_second_token(
        self, connected_client, app, fyers
    ):
        state = start_login(connected_client)
        assert callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        replay = callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        self._failed(replay, oauth_callback.REASON_STATE_INVALID)
        assert len(token_rows(app)) == 1
        assert len(fyers.exchange_requests) == 1

    def test_a_state_from_another_session_is_rejected(self, connected_client, app, fyers):
        state = start_login(connected_client)

        other = second_browser(app)
        assert other.post(
            "/api/v1/auth/login", json={"username": USERNAME, "password": PASSCODE}
        ).status_code == 200
        attempt = callback(other, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        self._failed(attempt, oauth_callback.REASON_STATE_INVALID)
        assert token_rows(app) == []
        assert fyers.exchange_requests == []

    def test_the_state_survives_a_rejection_by_another_session(
        self, connected_client, app, fyers
    ):
        """A failed attempt by somebody else must not burn the legitimate user's state."""
        state = start_login(connected_client)
        other = second_browser(app)
        other.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSCODE})
        callback(other, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        response = callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        assert response.headers["location"] == oauth_callback.SUCCESS_REDIRECT

    def test_no_session_at_all_is_the_same_generic_failure(self, connected_client, fyers):
        state = start_login(connected_client)
        connected_client.cookies.clear()

        response = callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        self._failed(response, oauth_callback.REASON_STATE_INVALID)

    def test_a_missing_state_is_the_same_generic_failure(self, connected_client, fyers):
        start_login(connected_client)

        self._failed(
            callback(connected_client, state=None, auth_code=SYNTHETIC_AUTH_CODE),
            oauth_callback.REASON_STATE_INVALID,
        )

    def test_an_unknown_state_is_the_same_generic_failure(self, connected_client, fyers):
        start_login(connected_client)

        self._failed(
            callback(connected_client, state="not-a-state-this-app-minted", auth_code="x"),
            oauth_callback.REASON_STATE_INVALID,
        )

    def test_an_expired_state_is_the_same_generic_failure(self, connected_client, app, fyers):
        state = start_login(connected_client)
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text("UPDATE oauth_state SET expires_at = '2020-01-01T00:00:00+00:00'")
            )

        self._failed(
            callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE),
            oauth_callback.REASON_STATE_INVALID,
        )

    def test_every_state_failure_returns_the_identical_location(self, connected_client, fyers):
        start_login(connected_client)
        locations = {
            callback(connected_client, state=value, auth_code="x").headers["location"]
            for value in (None, "unknown-state", "another-unknown-state")
        }
        assert len(locations) == 1

    def test_a_missing_auth_code_consumes_the_state_and_reports_a_login_failure(
        self, connected_client, app, fyers
    ):
        state = start_login(connected_client)

        response = connected_client.get(
            "/fyers/callback",
            params={"s": "error", "code": "-99", "message": "user declined", "state": state},
            follow_redirects=False,
        )

        self._failed(response, oauth_callback.REASON_LOGIN_FAILED)
        assert fyers.exchange_requests == []
        with app.state.services.engine.connect() as connection:
            assert connection.execute(text("SELECT used_at FROM oauth_state")).scalar()

    def test_an_upstream_error_response_stores_no_token(self, connected_client, app):
        fyers = FakeFyers(
            connected_client.app,
            exchange_response=json_response(
                400,
                {"s": "error", "code": -352, "message": "invalid app id or app secret"},
            ),
        )
        state = start_login(connected_client)

        response = callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        self._failed(response, oauth_callback.REASON_EXCHANGE_FAILED)
        assert token_rows(app) == []
        assert app.state.services.token_broker.has_valid_token() is False
        assert len(fyers.exchange_requests) == 1

    def test_an_upstream_body_never_reaches_the_browser(self, connected_client):
        FakeFyers(
            connected_client.app,
            exchange_response=json_response(
                400,
                {
                    "s": "error",
                    "code": -352,
                    "message": f"secret {FAKE_APP_SECRET} was rejected",
                },
            ),
        )
        state = start_login(connected_client)

        response = callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        assert FAKE_APP_SECRET not in response.text
        assert FAKE_APP_SECRET not in json.dumps(dict(response.headers))

    def test_a_success_envelope_with_no_token_is_still_a_failure(self, connected_client, app):
        FakeFyers(
            connected_client.app,
            exchange_response=json_response(200, {"s": "ok", "code": 200, "message": ""}),
        )
        state = start_login(connected_client)

        response = callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        self._failed(response, oauth_callback.REASON_EXCHANGE_FAILED)
        assert token_rows(app) == []


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class TestCallbackLogging:
    def test_neither_the_auth_code_nor_the_token_reaches_a_log_record(
        self, connected_client, fyers, caplog
    ):
        with caplog.at_level("DEBUG"):
            state = start_login(connected_client)
            response = callback(
                connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE
            )
        assert response.status_code == 303

        written = filtered_log(caplog.records)
        assert SYNTHETIC_AUTH_CODE not in written
        assert fyers.access_token not in written
        assert state not in written
        assert FAKE_APP_SECRET not in written

    def test_a_uvicorn_access_record_for_the_callback_is_scrubbed(
        self, connected_client, fyers
    ):
        """The one place the raw callback URL genuinely appears in production.

        uvicorn formats the access line positionally, with the request line as a string argument,
        which is the shape CallbackQueryFilter is written for.
        """
        import logging

        state = start_login(connected_client)
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=(
                "127.0.0.1:54321",
                "GET",
                f"/fyers/callback?s=ok&code=200&auth_code={SYNTHETIC_AUTH_CODE}&state={state}",
                "1.1",
                303,
            ),
            exc_info=None,
        )

        CallbackQueryFilter().filter(record)
        RedactionFilter().filter(record)

        assert SYNTHETIC_AUTH_CODE not in record.getMessage()
        assert state not in record.getMessage()
        assert "/fyers/callback" in record.getMessage()
        assert "303" in record.getMessage()

    def test_the_redaction_filter_scrubs_a_callback_line(self, connected_client, fyers):
        from expirymanager.security.redaction import redact_text

        state = start_login(connected_client)
        line = (
            f'GET /fyers/callback?s=ok&code=200&auth_code={SYNTHETIC_AUTH_CODE}'
            f'&state={state} HTTP/1.1" 303'
        )

        scrubbed = redact_text(line)

        assert SYNTHETIC_AUTH_CODE not in scrubbed
        assert state not in scrubbed
        assert "303" in scrubbed


# ---------------------------------------------------------------------------
# The manual paste fallback
# ---------------------------------------------------------------------------


class TestManualCallback:
    def _paste(self, client, url: str):
        return client.post(
            "/api/v1/broker/fyers/callback/manual",
            json={"redirected_url": url},
            headers=csrf(client),
        )

    def _redirected_url(self, state: str) -> str:
        return (
            f"{DEFAULT_REDIRECT_URI}?s=ok&code=200"
            f"&auth_code={SYNTHETIC_AUTH_CODE}&state={state}"
        )

    def test_pasting_the_url_completes_the_same_login(self, connected_client, app, fyers):
        state = start_login(connected_client)

        response = self._paste(connected_client, self._redirected_url(state))

        assert response.status_code == 200
        assert response.json()["connected"] is True
        assert app.state.services.token_broker.has_valid_token() is True
        assert len(fyers.exchange_requests) == 1

    def test_it_runs_the_identical_state_verification(self, connected_client, fyers):
        state = start_login(connected_client)
        self._paste(connected_client, self._redirected_url(state))

        replay = self._paste(connected_client, self._redirected_url(state))

        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == broker_routes.CODE_OAUTH_STATE_INVALID

    def test_a_state_from_another_session_is_refused(self, connected_client, app, fyers):
        state = start_login(connected_client)

        other = second_browser(app)
        other.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSCODE})
        response = self._paste(other, self._redirected_url(state))

        assert response.status_code == 400
        assert response.json()["error"]["code"] == broker_routes.CODE_OAUTH_STATE_INVALID

    def test_a_url_with_no_query_string_is_a_generic_400(self, connected_client, fyers):
        start_login(connected_client)

        response = self._paste(connected_client, DEFAULT_REDIRECT_URI)

        assert response.status_code == 400
        assert response.json()["error"]["message"] == broker_routes.GENERIC_STATE_MESSAGE

    def test_the_pasted_url_never_comes_back_in_the_response(self, connected_client, fyers):
        start_login(connected_client)

        response = self._paste(connected_client, self._redirected_url("wrong-state"))

        assert SYNTHETIC_AUTH_CODE not in response.text

    def test_the_pasted_url_never_reaches_a_log_record(self, connected_client, fyers, caplog):
        state = start_login(connected_client)
        with caplog.at_level("DEBUG"):
            self._paste(connected_client, self._redirected_url(state))

        assert SYNTHETIC_AUTH_CODE not in filtered_log(caplog.records)


# ---------------------------------------------------------------------------
# Test, disconnect and the scheduled logout
# ---------------------------------------------------------------------------


class TestConnectionTest:
    def test_it_spends_exactly_one_governed_request(self, connected_client, app, fyers):
        state = start_login(connected_client)
        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)
        before = app.state.services.governor.snapshot().requests_used

        response = connected_client.post(
            "/api/v1/broker/fyers/test", headers=csrf(connected_client)
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["endpoint"] == "expiry-dates"
        assert body["requests_used_today"] == before + 1
        assert body["latency_ms"] >= 0

    def test_the_probe_asks_for_the_documented_symbol(self, connected_client, fyers):
        state = start_login(connected_client)
        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        connected_client.post("/api/v1/broker/fyers/test", headers=csrf(connected_client))

        probe = [r for r in fyers.transport.requests if r.url.path == ep.EXPIRY_DATES.path][-1]
        assert probe.url.params["symbol"] == broker_routes.TEST_SYMBOL

    def test_without_a_token_it_is_the_documented_409(self, connected_client, fyers):
        response = connected_client.post(
            "/api/v1/broker/fyers/test", headers=csrf(connected_client)
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "needs_reauth"


class TestDisconnect:
    def test_it_destroys_the_ciphertext_and_keeps_the_credentials(
        self, connected_client, app, fyers
    ):
        state = start_login(connected_client)
        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        response = connected_client.post(
            "/api/v1/broker/fyers/disconnect", headers=csrf(connected_client)
        )

        assert response.status_code == 204
        rows = token_rows(app)
        assert rows[0][3] == "revoked"
        assert bytes(rows[0][1]) == b""
        assert app.state.services.token_broker.has_valid_token() is False
        assert connected_client.get("/api/v1/broker/fyers").json()["app_secret_configured"]


class TestScheduledLogout:
    async def _run(self, app):
        await broker_routes.scheduled_logout(app.state.services)

    def test_it_clears_the_token_parks_the_jobs_and_revokes_the_sessions(
        self, connected_client, app, fyers
    ):
        state = start_login(connected_client)
        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job (job_id, kind, status, params_json, created_at) VALUES "
                    "('job-1', 'candle_backfill', 'running', '{}', '2026-01-01T00:00:00+00:00')"
                )
            )

        connected_client.portal.call(broker_routes.scheduled_logout, app.state.services)

        broker = app.state.services.token_broker
        assert broker.has_valid_token() is False
        assert broker.auth_gate.is_set() is False
        with app.state.services.engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT status FROM job WHERE job_id = 'job-1'")
                ).scalar()
                == "blocked_auth"
            )
            assert connection.execute(text("SELECT count(*) FROM session")).scalar() == 0
        assert connected_client.get("/api/v1/auth/me").status_code == 401

    def test_the_parked_job_resumes_after_the_next_login(self, connected_client, app, fyers):
        state = start_login(connected_client)
        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job (job_id, kind, status, params_json, created_at) VALUES "
                    "('job-1', 'candle_backfill', 'queued', '{}', '2026-01-01T00:00:00+00:00')"
                )
            )
        connected_client.portal.call(broker_routes.scheduled_logout, app.state.services)

        connected_client.post(
            "/api/v1/auth/login", json={"username": USERNAME, "password": PASSCODE}
        )
        new_state = start_login(connected_client)
        callback(connected_client, state=new_state, auth_code=SYNTHETIC_AUTH_CODE)

        with app.state.services.engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM job WHERE job_id = 'job-1'")
            ).scalar()
        assert status == "queued"
        assert app.state.services.token_broker.has_valid_token() is True


class TestStatusResilience:
    def test_an_unreadable_token_reads_as_needing_reauth_rather_than_a_500(
        self, connected_client, app, fyers, monkeypatch
    ):
        """The screen that offers the reconnect button must not be the screen that breaks."""
        state = start_login(connected_client)
        callback(connected_client, state=state, auth_code=SYNTHETIC_AUTH_CODE)

        def explode():
            raise ValueError("the ciphertext does not decrypt under any loaded key")

        monkeypatch.setattr(app.state.services.token_broker, "record", explode)

        response = connected_client.get("/api/v1/broker/fyers")

        assert response.status_code == 200
        body = response.json()
        assert body["connected"] is False
        assert body["token_state"] == "needs_reauth"
        assert body["app_secret_configured"] is True
