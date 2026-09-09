"""W16: the local passcode routes.

Every test here runs a real application over a temporary data directory: real SQLite, real
migrations, a real key hierarchy, real Argon2id and the real middleware stack. Nothing is stubbed
out of the authentication path, because the properties being asserted (one failure body, one
failure cost, a replaced session id, cookie flags) are exactly the ones a stub would satisfy for
free.

Every credential in this file is synthetic.
"""

from __future__ import annotations

from expirymanager import runtime_scheme

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from expirymanager.api.v1.auth import (
    CODE_ACCOUNT_LOCKED,
    CODE_ALREADY_PROVISIONED,
    CODE_INVALID_CREDENTIALS,
)
from expirymanager.app import create_app
from expirymanager.db import sqlite as sqlite_module
from expirymanager.security import passwords
from expirymanager.security.sessions import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

BASE_URL = "https://127.0.0.1:8000"

USERNAME = "operator"
PASSCODE = "correct-horse-battery-staple"
OTHER_PASSCODE = "another-passcode-entirely"


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


def csrf_headers(client: TestClient) -> dict[str, str]:
    """The header the fetch wrapper sends, read from the cookie the way the browser would."""
    token = client.cookies.get(CSRF_COOKIE_NAME)
    return {"X-CSRF-Token": token} if token else {}


def do_setup(client: TestClient, *, username: str = USERNAME, password: str = PASSCODE):
    return client.post(
        "/api/v1/auth/setup",
        json={"username": username, "password": password},
        headers=csrf_headers(client),
    )


def do_login(client: TestClient, *, username: str = USERNAME, password: str = PASSCODE):
    """Always carries the CSRF header.

    Login is reachable with no session at all, and only then do layers 1 and 2 stand alone. The
    moment a session exists the synchroniser token is required here too, so a browser that is
    already signed in cannot be made to re-authenticate as somebody else.
    """
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers=csrf_headers(client),
    )


@pytest.fixture
def signed_in(client):
    """A client that has completed setup and holds a session."""
    response = do_setup(client)
    assert response.status_code == 200
    return client


class TestSetup:
    def test_it_creates_the_account_and_signs_the_browser_in(self, client, app):
        response = do_setup(client)

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"]
        assert SESSION_COOKIE_NAME in response.cookies
        assert CSRF_COOKIE_NAME in response.cookies

        with app.state.services.engine.connect() as connection:
            row = connection.execute(
                text("SELECT username, password_phc FROM app_user")
            ).first()
        assert row[0] == USERNAME
        assert row[1].startswith("$argon2id$")

    def test_the_passcode_is_never_stored_in_plaintext(self, client, app):
        do_setup(client)
        with app.state.services.engine.connect() as connection:
            row = connection.execute(text("SELECT password_phc FROM app_user")).first()
        assert PASSCODE not in row[0]

    def test_a_second_setup_is_rejected_and_creates_nothing(self, client, app):
        do_setup(client)

        response = client.post(
            "/api/v1/auth/setup",
            json={"username": "intruder", "password": "a-second-passcode-value"},
            headers=csrf_headers(client),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == CODE_ALREADY_PROVISIONED
        with app.state.services.engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM app_user")).scalar()
        assert count == 1

    def test_a_short_passcode_is_refused_with_the_policy_message(self, client):
        response = client.post(
            "/api/v1/auth/setup", json={"username": USERNAME, "password": "short"}
        )

        assert response.status_code == 422
        assert str(passwords.MIN_PASSWORD_LENGTH) in response.json()["error"]["message"]

    def test_the_submitted_passcode_is_never_echoed_in_a_validation_error(self, client):
        response = client.post(
            "/api/v1/auth/setup", json={"username": "ab", "password": PASSCODE}
        )

        assert response.status_code == 422
        assert PASSCODE not in response.text

    def test_leading_and_trailing_spaces_in_a_passcode_are_preserved(self, client):
        spaced = "  " + PASSCODE + "  "
        assert do_setup(client, password=spaced).status_code == 200

        client.cookies.clear()
        assert do_login(client, password=spaced).status_code == 200
        assert do_login(client, password=PASSCODE).status_code == 401


class TestLogin:
    def test_it_returns_the_user_and_rotates_the_session_id(self, client):
        do_setup(client)
        first = client.cookies.get(SESSION_COOKIE_NAME)

        response = do_login(client)

        assert response.status_code == 200
        assert response.json() == {"user_id": response.json()["user_id"], "username": USERNAME}
        second = client.cookies.get(SESSION_COOKIE_NAME)
        assert second and second != first

    def test_the_pre_login_session_row_is_deleted_not_merely_replaced(self, client, app):
        do_setup(client)
        do_login(client)

        with app.state.services.engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM session")).scalar()
        assert count == 1

    def test_an_unknown_user_and_a_wrong_passcode_are_indistinguishable(self, client):
        do_setup(client)
        client.cookies.clear()

        unknown = do_login(client, username="nobody", password=OTHER_PASSCODE)
        wrong = do_login(client, password=OTHER_PASSCODE)

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["error"]["code"] == CODE_INVALID_CREDENTIALS
        assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]

    def test_an_unknown_user_costs_a_real_verification(self, client):
        """The unknown branch must not be the cheap branch, or it is a username oracle."""
        do_setup(client)
        client.cookies.clear()

        started = time.perf_counter()
        do_login(client, username="nobody-at-all", password=OTHER_PASSCODE)
        unknown_seconds = time.perf_counter() - started

        started = time.perf_counter()
        do_login(client, password=OTHER_PASSCODE)
        wrong_seconds = time.perf_counter() - started

        # Argon2id at these parameters costs tens of milliseconds. A branch that skipped it would
        # come back in microseconds, which is what this catches; the bound is loose on purpose so
        # a loaded machine does not fail the run.
        assert unknown_seconds > wrong_seconds / 10

    def test_no_session_is_issued_for_a_failed_login(self, client, app):
        do_setup(client)
        client.cookies.clear()

        do_login(client, password=OTHER_PASSCODE)

        assert client.cookies.get(SESSION_COOKIE_NAME) is None
        with app.state.services.engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM session")).scalar()
        assert count == 1  # the setup session, untouched

    def test_the_failed_attempt_counter_climbs_and_resets(self, client, app):
        do_setup(client)
        client.cookies.clear()
        do_login(client, password=OTHER_PASSCODE)
        do_login(client, password=OTHER_PASSCODE)

        with app.state.services.engine.connect() as connection:
            assert (
                connection.execute(text("SELECT failed_attempts FROM app_user")).scalar() == 2
            )

        assert do_login(client).status_code == 200
        with app.state.services.engine.connect() as connection:
            row = connection.execute(
                text("SELECT failed_attempts, locked_until, last_login_at FROM app_user")
            ).first()
        assert row[0] == 0
        assert row[1] is None
        assert row[2] is not None


class TestLockout:
    def _fail_once(self, client, app):
        """One failed attempt, with the route level limiter reset so the lock is what fires."""
        app.state.services.rate_limiter.reset()
        return do_login(client, password=OTHER_PASSCODE)

    def test_ten_failures_lock_the_account_and_the_next_attempt_is_423(self, client, app):
        do_setup(client)
        client.cookies.clear()

        for _ in range(passwords.LOCKOUT_THRESHOLD):
            assert self._fail_once(client, app).status_code == 401

        app.state.services.rate_limiter.reset()
        locked = do_login(client)

        assert locked.status_code == 423
        assert locked.json()["error"]["code"] == CODE_ACCOUNT_LOCKED
        retry_after = int(locked.headers["Retry-After"])
        assert 0 < retry_after <= int(passwords.LOCKOUT_DURATION.total_seconds())

    def test_the_correct_passcode_does_not_open_a_locked_account(self, client, app):
        do_setup(client)
        client.cookies.clear()
        for _ in range(passwords.LOCKOUT_THRESHOLD):
            self._fail_once(client, app)

        app.state.services.rate_limiter.reset()
        assert do_login(client).status_code == 423

    def test_the_attempt_that_trips_the_lock_still_answers_401(self, client, app):
        """423 on that attempt would confirm the username at the worst possible moment."""
        do_setup(client)
        client.cookies.clear()
        for _ in range(passwords.LOCKOUT_THRESHOLD - 1):
            self._fail_once(client, app)

        tripping = self._fail_once(client, app)

        assert tripping.status_code == 401


class TestLoginRateLimit:
    def test_the_documented_per_username_rule_fires_before_the_per_ip_one(self, client):
        do_setup(client)
        client.cookies.clear()

        statuses = [do_login(client, password=OTHER_PASSCODE).status_code for _ in range(8)]

        assert statuses.count(401) == 5
        assert statuses[-1] == 429

    def test_a_rate_limited_login_carries_retry_after_and_the_budget_headers(self, client):
        do_setup(client)
        client.cookies.clear()
        for _ in range(6):
            response = do_login(client, password=OTHER_PASSCODE)

        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
        assert response.headers["RateLimit-Limit"]
        assert "RateLimit-Remaining" in response.headers

    def test_rotating_the_username_still_hits_the_per_ip_ceiling(self, client):
        do_setup(client)
        client.cookies.clear()

        statuses = [
            do_login(client, username=f"user{index}", password=OTHER_PASSCODE).status_code
            for index in range(25)
        ]

        assert statuses.count(429) > 0


class TestCookies:
    def test_the_session_cookie_carries_the_documented_flags(self, client):
        response = do_setup(client)

        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE_NAME}="))
        csrf_cookie = next(c for c in cookies if c.startswith(f"{CSRF_COOKIE_NAME}="))

        assert "HttpOnly" in session_cookie
        # Secure follows the scheme the server serves. Asserting it unconditionally would
        # encode a contract that breaks login on http, where the browser accepts the
        # cookie and then never sends it back.
        assert ("Secure" in session_cookie) is runtime_scheme.is_https()
        assert "samesite=lax" in session_cookie.lower()
        assert "Path=/" in session_cookie
        assert "Domain=" not in session_cookie
        # The frontend has to read this one to echo it in X-CSRF-Token.
        assert "HttpOnly" not in csrf_cookie
        # Secure follows the scheme the server serves. Asserting it unconditionally would
        # encode a contract that breaks login on http, where the browser accepts the
        # cookie and then never sends it back.
        assert ("Secure" in csrf_cookie) is runtime_scheme.is_https()

    def test_only_the_hash_of_the_session_id_is_stored(self, client, app):
        do_setup(client)
        raw = client.cookies.get(SESSION_COOKIE_NAME)

        with app.state.services.engine.connect() as connection:
            rows = connection.execute(text("SELECT * FROM session")).fetchall()
        for row in rows:
            for value in row:
                assert raw not in str(value)


class TestMeAndLogout:
    def test_me_reports_the_user_and_the_session_deadline(self, signed_in):
        response = signed_in.get("/api/v1/auth/me")

        assert response.status_code == 200
        body = response.json()
        assert body["username"] == USERNAME
        assert body["user_id"]
        assert body["session_expires_at"]

    def test_me_is_401_with_no_session(self, client):
        do_setup(client)
        client.cookies.clear()

        response = client.get("/api/v1/auth/me")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"

    def test_logout_deletes_the_row_and_clears_both_cookies(self, signed_in, app):
        response = signed_in.post("/api/v1/auth/logout", headers=csrf_headers(signed_in))

        assert response.status_code == 204
        with app.state.services.engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM session")).scalar()
        assert count == 0
        assert signed_in.get("/api/v1/auth/me").status_code == 401

    def test_logout_with_no_session_is_refused_by_the_csrf_layer(self, client):
        """Not 204.

        A POST with neither a session nor a token is exactly the shape of a cross-site logout,
        and there is nothing to log out of: the row is already gone. The browser that reaches
        this state has a stale cookie and its next authenticated request is a 401 anyway.
        """
        do_setup(client)
        client.cookies.clear()

        assert client.post("/api/v1/auth/logout").status_code == 403

    def test_an_unsafe_request_without_the_csrf_header_is_rejected(self, signed_in):
        response = signed_in.post("/api/v1/auth/logout")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_invalid"


class TestPasswordChange:
    def test_it_replaces_the_hash_and_reissues_one_session(self, signed_in, app):
        before = signed_in.cookies.get(SESSION_COOKIE_NAME)

        response = signed_in.post(
            "/api/v1/auth/password",
            json={"current_password": PASSCODE, "new_password": OTHER_PASSCODE},
            headers=csrf_headers(signed_in),
        )

        assert response.status_code == 204
        assert signed_in.cookies.get(SESSION_COOKIE_NAME) != before
        assert signed_in.get("/api/v1/auth/me").status_code == 200
        with app.state.services.engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM session")).scalar()
        assert count == 1

    def test_every_other_session_is_deleted(self, signed_in, app, client):
        with TestClient(app, base_url=BASE_URL) as second:
            assert do_login(second).status_code == 200
            with app.state.services.engine.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM session")).scalar() == 2

            signed_in.post(
                "/api/v1/auth/password",
                json={"current_password": PASSCODE, "new_password": OTHER_PASSCODE},
                headers=csrf_headers(signed_in),
            )

            assert second.get("/api/v1/auth/me").status_code == 401

    def test_the_new_passcode_works_and_the_old_one_does_not(self, signed_in):
        signed_in.post(
            "/api/v1/auth/password",
            json={"current_password": PASSCODE, "new_password": OTHER_PASSCODE},
            headers=csrf_headers(signed_in),
        )
        signed_in.cookies.clear()

        assert do_login(signed_in, password=OTHER_PASSCODE).status_code == 200
        signed_in.cookies.clear()
        assert do_login(signed_in, password=PASSCODE).status_code == 401

    def test_a_wrong_current_passcode_changes_nothing(self, signed_in):
        response = signed_in.post(
            "/api/v1/auth/password",
            json={"current_password": OTHER_PASSCODE, "new_password": "yet-another-passcode"},
            headers=csrf_headers(signed_in),
        )

        assert response.status_code == 401
        signed_in.cookies.clear()
        assert do_login(signed_in).status_code == 200

    def test_a_weak_new_passcode_is_refused(self, signed_in):
        response = signed_in.post(
            "/api/v1/auth/password",
            json={"current_password": PASSCODE, "new_password": "tiny"},
            headers=csrf_headers(signed_in),
        )

        assert response.status_code == 422


class TestLogging:
    def test_no_passcode_reaches_a_log_record(self, client, caplog):
        with caplog.at_level("DEBUG"):
            do_setup(client)
            client.cookies.clear()
            do_login(client, password=OTHER_PASSCODE)
            do_login(client)

        written = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
        assert PASSCODE not in written
        assert OTHER_PASSCODE not in written
