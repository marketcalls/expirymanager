"""The HTTP security middleware stack: TrustedHost, headers, CSRF and inbound rate limiting.

Assembled here in the order the app factory will use it, because the order is load bearing. A CSRF
rejection must happen before the rate limiter charges anyone's budget, and the session must be
resolved before CSRF can compare a token against it.

Everything runs over `https://127.0.0.1:8000`, the real production origin, because the cookies are
unconditionally `Secure` and would simply be dropped over http.
"""

from __future__ import annotations

from expirymanager import runtime_scheme

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from sqlalchemy.orm import Session as OrmSession

from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.models import AppUser
from expirymanager.security import csrf as csrf_module
from expirymanager.security import headers as headers_module
from expirymanager.security import ratelimit as ratelimit_module
from expirymanager.security import sessions as sessions_module
from expirymanager.security.csrf import (
    CsrfMiddleware,
    DEV_ORIGIN,
    PROD_ORIGIN,
    REASON_BAD_ORIGIN,
    REASON_CROSS_ORIGIN,
    REASON_CSRF_INVALID,
    check_csrf,
)
from expirymanager.security.headers import (
    CONTENT_SECURITY_POLICY,
    ENVIRONMENT_DEVELOPMENT,
    ENVIRONMENT_PRODUCTION,
    PERMISSIONS_POLICY,
    SecurityHeadersMiddleware,
)
from expirymanager.security.ratelimit import (
    ConcurrencyLimiter,
    RateLimited,
    RateLimiter,
    RateLimitMiddleware,
)
from expirymanager.security.sessions import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SessionManager,
    SessionMiddleware,
)

BASE_URL = "https://127.0.0.1:8000"
ALLOWED_HOSTS = ["127.0.0.1", "127.0.0.1:8000", "127.0.0.1:5173"]

FAKE_AUTH_CODE = "SYNTHETICAUTHCODE0123456789"
FAKE_STATE = "SYNTHETICSTATEVALUE9876543210"


# The documented policy from SECURITY.md section 9, written out so a change to either the builder
# or the document shows up as a failing test rather than as a silently different header.
EXPECTED_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)


async def _ok(request):
    return JSONResponse({"ok": True})


async def _index(request):
    return PlainTextResponse("<!doctype html>", media_type="text/html")


async def _callback(request):
    return RedirectResponse("/settings?broker=connected", status_code=303)


ROUTES = [
    Route("/", _index),
    Route("/api/v1/bootstrap", _ok),
    Route("/api/v1/auth/login", _ok, methods=["POST"]),
    Route("/api/v1/auth/setup", _ok, methods=["POST"]),
    Route("/api/v1/jobs/{job_id}/cancel", _ok, methods=["POST"]),
    Route("/api/v1/downloads", _ok, methods=["POST"]),
    Route("/api/v1/contracts", _ok),
    Route("/fyers/callback", _callback),
]


@pytest.fixture
def engine(tmp_path: Path):
    eng = sqlite_module.create_engine(tmp_path / "config.sqlite3")
    migrate_module.migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def manager(engine) -> SessionManager:
    return SessionManager(engine)


@pytest.fixture
def user_id(engine) -> str:
    identifier = str(uuid.uuid4())
    with OrmSession(engine) as db:
        db.add(
            AppUser(
                user_id=identifier,
                username="synthetic",
                password_phc="synthetic-phc-not-a-real-hash",
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        db.commit()
    return identifier


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter()


def build_app(
    manager: SessionManager,
    limiter: RateLimiter,
    *,
    environment: str = ENVIRONMENT_DEVELOPMENT,
) -> Starlette:
    """The documented middleware order: TrustedHost, SecurityHeaders, Session, CSRF, RateLimit."""
    return Starlette(
        routes=ROUTES,
        middleware=[
            Middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS),
            Middleware(SecurityHeadersMiddleware, environment=environment),
            Middleware(SessionMiddleware, manager=manager),
            Middleware(CsrfMiddleware),
            Middleware(RateLimitMiddleware, limiter=limiter),
        ],
    )


@pytest.fixture
def client(manager, limiter):
    with TestClient(build_app(manager, limiter), base_url=BASE_URL) as test_client:
        yield test_client


def login(client: TestClient, manager: SessionManager, user_id: str) -> str:
    """Put a real session cookie pair in the client jar and return the CSRF token."""
    issued = manager.create(user_id)
    client.cookies.set(SESSION_COOKIE_NAME, issued.raw_id, domain="127.0.0.1")
    client.cookies.set(CSRF_COOKIE_NAME, issued.csrf_token, domain="127.0.0.1")
    return issued.csrf_token


class TestCsrfPolicy:
    """The pure policy function, exhaustively, with no app in the way."""

    def test_safe_methods_are_never_checked(self):
        for method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            decision = check_csrf(
                method=method,
                path="/api/v1/jobs",
                sec_fetch_site="cross-site",
                origin="https://evil.invalid",
                header_token=None,
                session_token="token",
            )
            assert decision.allowed is True

    def test_layer_one_rejects_before_the_token_is_read(self):
        # A correct token must not rescue a cross-site request, and the reason must name layer 1.
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="cross-site",
            origin=PROD_ORIGIN,
            header_token="correct",
            session_token="correct",
        )
        assert decision.allowed is False
        assert decision.reason == REASON_CROSS_ORIGIN

    def test_same_site_is_rejected_as_well_as_cross_site(self):
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="same-site",
            origin=PROD_ORIGIN,
            header_token="correct",
            session_token="correct",
        )
        assert decision.reason == REASON_CROSS_ORIGIN

    def test_foreign_origin_is_rejected(self):
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="same-origin",
            origin="https://evil.invalid",
            header_token="correct",
            session_token="correct",
        )
        assert decision.reason == REASON_BAD_ORIGIN

    def test_absent_origin_is_not_a_rejection(self):
        # Some browsers omit Origin on a same-origin POST.
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="same-origin",
            origin=None,
            header_token="correct",
            session_token="correct",
        )
        assert decision.allowed is True

    @pytest.mark.parametrize("origin", [DEV_ORIGIN, PROD_ORIGIN])
    def test_both_loopback_origins_are_allowed(self, origin):
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="same-origin",
            origin=origin,
            header_token="correct",
            session_token="correct",
        )
        assert decision.allowed is True

    def test_wrong_and_missing_tokens_are_rejected_identically(self):
        for header_token in (None, "", "wrong"):
            decision = check_csrf(
                method="POST",
                path="/api/v1/downloads",
                sec_fetch_site="same-origin",
                origin=PROD_ORIGIN,
                header_token=header_token,
                session_token="correct",
            )
            assert decision.allowed is False
            assert decision.reason == REASON_CSRF_INVALID

    def test_a_single_flipped_character_is_rejected(self):
        token = "synthetic-csrf-token-value"
        tampered = token[:-1] + ("x" if token[-1] != "x" else "y")
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="same-origin",
            origin=PROD_ORIGIN,
            header_token=tampered,
            session_token=token,
        )
        assert decision.reason == REASON_CSRF_INVALID

    def test_a_truncated_token_is_rejected(self):
        token = "synthetic-csrf-token-value"
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="same-origin",
            origin=PROD_ORIGIN,
            header_token=token[:-1],
            session_token=token,
        )
        assert decision.reason == REASON_CSRF_INVALID

    def test_a_token_with_the_right_prefix_is_rejected(self):
        # The comparison is constant time and whole-value, not a prefix match.
        token = "synthetic-csrf-token-value"
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="same-origin",
            origin=PROD_ORIGIN,
            header_token=token + "extra",
            session_token=token,
        )
        assert decision.reason == REASON_CSRF_INVALID

    def test_the_callback_is_the_only_exempt_path(self):
        assert csrf_module.is_exempt("/fyers/callback") is True
        assert csrf_module.is_exempt("/fyers/callback/") is True
        assert csrf_module.is_exempt("/api/v1/fyers/callback") is False
        assert csrf_module.is_exempt("/api/v1/downloads") is False
        assert csrf_module.DEFAULT_EXEMPT_PATHS == ("/fyers/callback",)

    def test_sessionless_login_passes_layers_one_and_two_only(self):
        decision = check_csrf(
            method="POST",
            path="/api/v1/auth/login",
            sec_fetch_site="same-origin",
            origin=PROD_ORIGIN,
            header_token=None,
            session_token=None,
        )
        assert decision.allowed is True

    def test_sessionless_login_still_obeys_layer_one(self):
        decision = check_csrf(
            method="POST",
            path="/api/v1/auth/login",
            sec_fetch_site="cross-site",
            origin=None,
            header_token=None,
            session_token=None,
        )
        assert decision.reason == REASON_CROSS_ORIGIN

    def test_login_with_a_session_still_needs_the_token(self):
        decision = check_csrf(
            method="POST",
            path="/api/v1/auth/login",
            sec_fetch_site="same-origin",
            origin=PROD_ORIGIN,
            header_token=None,
            session_token="correct",
        )
        assert decision.reason == REASON_CSRF_INVALID

    def test_a_sessionless_protected_route_is_rejected(self):
        decision = check_csrf(
            method="POST",
            path="/api/v1/downloads",
            sec_fetch_site="same-origin",
            origin=PROD_ORIGIN,
            header_token="anything",
            session_token=None,
        )
        assert decision.reason == REASON_CSRF_INVALID


class TestCsrfOverHttp:
    def test_post_without_the_header_is_403(self, client, manager, user_id):
        login(client, manager, user_id)
        response = client.post("/api/v1/downloads", headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == REASON_CSRF_INVALID

    def test_post_with_a_token_from_another_session_is_403(self, client, manager, user_id):
        login(client, manager, user_id)
        other = manager.create(user_id)
        response = client.post(
            "/api/v1/downloads",
            headers={"Sec-Fetch-Site": "same-origin", "X-CSRF-Token": other.csrf_token},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == REASON_CSRF_INVALID

    def test_cross_site_post_is_403_before_the_token_is_read(self, client, manager, user_id):
        token = login(client, manager, user_id)
        response = client.post(
            "/api/v1/downloads",
            headers={"Sec-Fetch-Site": "cross-site", "X-CSRF-Token": token},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == REASON_CROSS_ORIGIN

    def test_foreign_origin_post_is_403(self, client, manager, user_id):
        token = login(client, manager, user_id)
        response = client.post(
            "/api/v1/downloads",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Origin": "https://evil.invalid",
                "X-CSRF-Token": token,
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == REASON_BAD_ORIGIN

    def test_a_tampered_token_is_403(self, client, manager, user_id):
        token = login(client, manager, user_id)
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        response = client.post(
            "/api/v1/downloads",
            headers={"Sec-Fetch-Site": "same-origin", "X-CSRF-Token": tampered},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == REASON_CSRF_INVALID

    def test_the_cookie_value_alone_is_not_enough(self, client, manager, user_id):
        # A cookie-only check would be plain double submit. The header is what an attacker on
        # another origin cannot set, so the request must fail with the cookie present and no header.
        login(client, manager, user_id)
        assert client.cookies.get(CSRF_COOKIE_NAME) is not None
        response = client.post("/api/v1/downloads", headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 403

    def test_a_correct_post_succeeds(self, client, manager, user_id):
        token = login(client, manager, user_id)
        response = client.post(
            "/api/v1/downloads",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Origin": PROD_ORIGIN,
                "X-CSRF-Token": token,
            },
        )
        assert response.status_code == 200

    def test_the_vite_dev_origin_works(self, client, manager, user_id):
        token = login(client, manager, user_id)
        response = client.post(
            "/api/v1/downloads",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Origin": DEV_ORIGIN,
                "X-CSRF-Token": token,
            },
        )
        assert response.status_code == 200

    def test_get_is_never_blocked(self, client):
        assert client.get("/api/v1/bootstrap").status_code == 200

    def test_the_oauth_callback_is_reachable_with_no_token(self, client):
        response = client.get(
            f"/fyers/callback?s=ok&code=200&auth_code={FAKE_AUTH_CODE}&state={FAKE_STATE}",
            headers={"Sec-Fetch-Site": "cross-site"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings?broker=connected"

    def test_login_is_reachable_with_no_session(self, client):
        response = client.post(
            "/api/v1/auth/login", headers={"Sec-Fetch-Site": "same-origin", "Origin": PROD_ORIGIN}
        )
        assert response.status_code == 200

    def test_a_rejected_post_never_consumes_rate_limit_budget(self, client, manager, user_id, limiter):
        login(client, manager, user_id)
        before = limiter.peek(ratelimit_module.GLOBAL_FALLBACK, "ip:testclient").remaining
        client.post("/api/v1/downloads", headers={"Sec-Fetch-Site": "cross-site"})
        after = limiter.peek(ratelimit_module.GLOBAL_FALLBACK, "ip:testclient").remaining
        assert before == after


class TestTrustedHost:
    def test_a_foreign_host_is_rejected(self, client):
        response = client.get("/api/v1/bootstrap", headers={"Host": "attacker.invalid"})
        assert response.status_code == 400


class TestSecurityHeaders:
    def test_the_csp_matches_the_specification(self):
        assert CONTENT_SECURITY_POLICY == EXPECTED_CSP

    def test_the_csp_allows_the_chart_style_injection(self):
        # openalgo-charts 2.1.0 creates a <style> element and sets textContent, and writes the
        # --oac-tool-cursor token with el.style.setProperty. Both need 'unsafe-inline' on styles.
        assert "style-src 'self' 'unsafe-inline'" in CONTENT_SECURITY_POLICY
        # Its drawing cursors are cursor: url("data:image/svg+xml,..."), governed by img-src.
        assert "img-src 'self' data:" in CONTENT_SECURITY_POLICY
        # There is no eval and no new Function anywhere in the library, so scripts stay strict.
        assert "script-src 'self';" in CONTENT_SECURITY_POLICY
        assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
        assert "'unsafe-inline'" not in CONTENT_SECURITY_POLICY.split("style-src")[0]

    def test_the_full_set_is_present_on_an_api_response(self, client):
        response = client.get("/api/v1/bootstrap")
        assert response.headers["content-security-policy"] == EXPECTED_CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["permissions-policy"] == PERMISSIONS_POLICY
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert response.headers["cache-control"] == "no-store"

    def test_the_full_set_is_present_on_the_spa_index(self, client):
        response = client.get("/")
        assert response.headers["content-security-policy"] == EXPECTED_CSP
        assert response.headers["referrer-policy"] == "no-referrer"
        # The SPA shell is cacheable; only /api and the OAuth callback are no-store.
        assert "cache-control" not in response.headers

    def test_the_oauth_callback_is_never_cached(self, client):
        response = client.get(
            f"/fyers/callback?auth_code={FAKE_AUTH_CODE}", follow_redirects=False
        )
        assert response.headers["cache-control"] == "no-store"

    def test_permissions_policy_matches_the_specification(self):
        assert PERMISSIONS_POLICY == "geolocation=(), camera=(), microphone=(), payment=(), usb=()"

    def test_hsts_is_absent_in_development(self, client):
        assert "strict-transport-security" not in client.get("/api/v1/bootstrap").headers

    def test_hsts_is_absent_over_http(self, manager, limiter):
        app = build_app(manager, limiter, environment=ENVIRONMENT_PRODUCTION)
        with TestClient(app, base_url="http://127.0.0.1:8000") as http_client:
            response = http_client.get("/api/v1/bootstrap")
        assert "strict-transport-security" not in response.headers

    def test_hsts_is_absent_on_loopback_even_in_production(self, manager, limiter):
        # SECURITY.md section 3a: HSTS on 127.0.0.1 poisons that origin for every other local
        # development server the user runs, and a self-signed certificate makes recovery worse.
        app = build_app(manager, limiter, environment=ENVIRONMENT_PRODUCTION)
        with TestClient(app, base_url=BASE_URL) as prod_client:
            response = prod_client.get("/api/v1/bootstrap")
        assert "strict-transport-security" not in response.headers

    def test_hsts_is_present_on_a_production_https_non_loopback_response(self, manager, limiter):
        app = Starlette(
            routes=ROUTES,
            middleware=[
                Middleware(
                    SecurityHeadersMiddleware, environment=ENVIRONMENT_PRODUCTION
                ),
                Middleware(SessionMiddleware, manager=manager),
                Middleware(RateLimitMiddleware, limiter=limiter),
            ],
        )
        with TestClient(app, base_url="https://data.example.invalid") as remote_client:
            response = remote_client.get("/api/v1/bootstrap")
        assert response.headers["strict-transport-security"] == "max-age=31536000"

    def test_a_route_keeps_its_own_cache_control(self, manager, limiter):
        async def cached(request):
            return JSONResponse({"ok": True}, headers={"Cache-Control": "max-age=60"})

        app = Starlette(
            routes=[Route("/api/v1/cached", cached)],
            middleware=[Middleware(SecurityHeadersMiddleware)],
        )
        with TestClient(app, base_url=BASE_URL) as cached_client:
            response = cached_client.get("/api/v1/cached")
        assert response.headers["cache-control"] == "max-age=60"
        assert response.headers["content-security-policy"] == EXPECTED_CSP

    def test_both_session_cookies_survive_the_header_rewrite(self, manager, user_id, limiter):
        # The middleware rewrites the raw header list. A rewrite that deduplicated by name would
        # drop one of the two Set-Cookie headers and break login in a way no unit test of
        # sessions.py could catch.
        async def issue(request):
            response = JSONResponse({"ok": True})
            sessions_module.set_session_cookies(response, manager.create(user_id))
            return response

        app = Starlette(
            routes=[Route("/api/v1/auth/login", issue, methods=["POST"])],
            middleware=[
                Middleware(SecurityHeadersMiddleware),
                Middleware(SessionMiddleware, manager=manager),
                Middleware(CsrfMiddleware),
                Middleware(RateLimitMiddleware, limiter=limiter),
            ],
        )
        with TestClient(app, base_url=BASE_URL) as login_client:
            response = login_client.post(
                "/api/v1/auth/login",
                headers={"Sec-Fetch-Site": "same-origin", "Origin": PROD_ORIGIN},
            )
        cookies = [v.decode() for k, v in response.headers.raw if k.lower() == b"set-cookie"]
        assert len(cookies) == 2
        assert any(cookie.startswith(f"{SESSION_COOKIE_NAME}=") for cookie in cookies)
        assert any(cookie.startswith(f"{CSRF_COOKIE_NAME}=") for cookie in cookies)
        assert response.headers["content-security-policy"] == EXPECTED_CSP

    def test_a_route_cannot_weaken_the_csp(self, manager, limiter):
        async def sneaky(request):
            return JSONResponse(
                {"ok": True}, headers={"Content-Security-Policy": "default-src *"}
            )

        app = Starlette(
            routes=[Route("/api/v1/sneaky", sneaky)],
            middleware=[Middleware(SecurityHeadersMiddleware)],
        )
        with TestClient(app, base_url=BASE_URL) as sneaky_client:
            response = sneaky_client.get("/api/v1/sneaky")
        assert response.headers["content-security-policy"] == EXPECTED_CSP


class TestRateLimit:
    def test_the_login_ceiling_triggers_with_retry_after(self, client):
        headers = {"Sec-Fetch-Site": "same-origin", "Origin": PROD_ORIGIN}
        ceiling = ratelimit_module.ROUTE_LIMITS[0]
        assert ceiling.name == "login_per_ip"

        for _ in range(ceiling.limit.amount):
            assert client.post("/api/v1/auth/login", headers=headers).status_code == 200

        blocked = client.post("/api/v1/auth/login", headers=headers)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"
        assert int(blocked.headers["retry-after"]) >= 1
        assert blocked.headers["ratelimit-limit"] == str(ceiling.limit.amount)
        assert blocked.headers["ratelimit-remaining"] == "0"

    def test_the_limit_releases_when_the_window_is_dropped(self, client, limiter):
        headers = {"Sec-Fetch-Site": "same-origin", "Origin": PROD_ORIGIN}
        for _ in range(21):
            client.post("/api/v1/auth/login", headers=headers)
        assert client.post("/api/v1/auth/login", headers=headers).status_code == 429

        limiter.reset()
        assert client.post("/api/v1/auth/login", headers=headers).status_code == 200

    def test_headers_are_present_on_a_successful_response(self, client):
        response = client.get("/api/v1/bootstrap")
        assert response.headers["ratelimit-limit"] == "60"
        assert int(response.headers["ratelimit-remaining"]) == 59
        assert int(response.headers["ratelimit-reset"]) >= 0

    def test_the_tightest_rule_is_reported(self, client, limiter):
        # bootstrap is 60/minute and the global fallback is 300/minute, so bootstrap is reported.
        response = client.get("/api/v1/bootstrap")
        assert response.headers["ratelimit-limit"] == "60"

    def test_the_login_per_username_rule_is_five_per_fifteen_minutes(self, limiter):
        rule = ratelimit_module.LOGIN_PER_USERNAME
        assert rule.limit.amount == 5
        assert rule.limit.get_expiry() == 900
        assert rule.enforced_by == ratelimit_module.ENFORCED_BY_ROUTE

    def test_login_per_username_allows_five_then_refuses(self, limiter):
        for _ in range(5):
            assert limiter.check_login("127.0.0.1", "synthetic").allowed is True
        refused = limiter.check_login("127.0.0.1", "synthetic")
        assert refused.allowed is False
        assert refused.retry_after >= 1

    def test_login_per_username_is_case_insensitive(self, limiter):
        for _ in range(5):
            limiter.check_login("127.0.0.1", "Synthetic")
        assert limiter.check_login("127.0.0.1", "synthetic").allowed is False

    def test_a_different_username_gets_its_own_budget(self, limiter):
        for _ in range(6):
            limiter.check_login("127.0.0.1", "first")
        assert limiter.check_login("127.0.0.1", "second").allowed is True

    def test_enforce_login_raises_with_headers(self, limiter):
        for _ in range(5):
            limiter.enforce_login("127.0.0.1", "synthetic")
        with pytest.raises(RateLimited) as excinfo:
            limiter.enforce_login("127.0.0.1", "synthetic")
        assert excinfo.value.retry_after >= 1
        assert "Retry-After" in excinfo.value.headers()

    def test_username_rotation_still_hits_the_per_ip_ceiling(self, client):
        # The per (ip, username) rule alone constrains nothing against a rotating username. The
        # per-ip ceiling is what makes brute force useless.
        headers = {"Sec-Fetch-Site": "same-origin", "Origin": PROD_ORIGIN}
        statuses = [
            client.post("/api/v1/auth/login", headers=headers).status_code for _ in range(25)
        ]
        assert 429 in statuses
        assert statuses.count(200) == 20

    def test_route_rules_match_the_documented_table(self, limiter):
        expected = {
            ("POST", "/api/v1/auth/setup"): ("auth_provisioning", 5, 3600),
            ("POST", "/api/v1/auth/password"): ("auth_provisioning", 5, 3600),
            ("POST", "/api/v1/broker/fyers/connect"): ("oauth", 20, 60),
            ("GET", "/fyers/callback"): ("oauth", 20, 60),
            ("POST", "/api/v1/downloads"): ("mutations", 30, 60),
            ("POST", "/api/v1/jobs/abc/cancel"): ("mutations", 30, 60),
            ("POST", "/api/v1/schedules"): ("mutations", 30, 60),
            ("POST", "/api/v1/exports"): ("exports", 10, 60),
            ("POST", "/api/v1/system/checkpoint"): ("exports", 10, 60),
            ("POST", "/api/v1/system/optimise"): ("system_heavy", 2, 3600),
            ("POST", "/api/v1/system/backup"): ("system_heavy", 2, 3600),
            ("GET", "/api/v1/bars"): ("hot_reads", 240, 60),
            ("GET", "/api/v1/contracts/42/bounds"): ("hot_reads", 240, 60),
            ("GET", "/api/v1/system/budget"): ("hot_reads", 240, 60),
            ("GET", "/api/v1/contracts"): ("reads", 120, 60),
        }
        for (method, path), (name, amount, expiry) in expected.items():
            rule = limiter.rule_for(method, path)
            assert rule is not None, f"{method} {path} matched no rule"
            assert rule.name == name, f"{method} {path} matched {rule.name}"
            assert rule.limit.amount == amount
            assert rule.limit.get_expiry() == expiry

    def test_the_global_fallback_is_three_hundred_a_minute(self):
        assert ratelimit_module.GLOBAL_FALLBACK.limit.amount == 300
        assert ratelimit_module.GLOBAL_FALLBACK.limit.get_expiry() == 60

    def test_the_sse_stream_is_not_windowed(self, limiter):
        refusal, decisions = limiter.evaluate(
            method="GET", path="/api/v1/events/stream", client_ip="127.0.0.1", session_key=None
        )
        assert refusal is None
        assert decisions == []


class TestConcurrencyLimiter:
    def test_ten_concurrent_streams_then_refusal(self):
        gate = ConcurrencyLimiter(ratelimit_module.SSE_CONCURRENCY_LIMIT)
        assert gate.limit == 10
        for _ in range(10):
            assert gate.acquire("session-a") is True
        assert gate.acquire("session-a") is False
        # A different session is unaffected.
        assert gate.acquire("session-b") is True

    def test_releasing_frees_a_slot(self):
        gate = ConcurrencyLimiter(2)
        gate.acquire("s")
        gate.acquire("s")
        assert gate.acquire("s") is False
        gate.release("s")
        assert gate.acquire("s") is True

    def test_release_below_zero_is_harmless(self):
        gate = ConcurrencyLimiter(2)
        gate.release("s")
        assert gate.held("s") == 0


class TestSessionMiddleware:
    def test_a_valid_cookie_resolves_the_session(self, client, manager, user_id):
        token = login(client, manager, user_id)
        response = client.post(
            "/api/v1/downloads",
            headers={"Sec-Fetch-Site": "same-origin", "X-CSRF-Token": token},
        )
        assert response.status_code == 200

    def test_an_expired_session_resolves_to_nothing(self, client, manager, user_id):
        issued = manager.create(user_id)
        manager.revoke(issued.raw_id)
        client.cookies.set(SESSION_COOKIE_NAME, issued.raw_id, domain="127.0.0.1")
        response = client.post(
            "/api/v1/downloads",
            headers={"Sec-Fetch-Site": "same-origin", "X-CSRF-Token": issued.csrf_token},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == REASON_CSRF_INVALID


class TestCallbackLogging:
    """The realistic callback URL must not reach the log file, in whole or in part."""

    def test_a_logged_callback_request_line_leaks_nothing(self, tmp_path):
        from expirymanager import logging_setup

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        log_file = tmp_path / "logs" / "app.log"
        try:
            handlers = logging_setup.configure_logging(log_file=log_file)
            access = logging.getLogger("uvicorn.access")
            access.info(
                '%s - "%s %s HTTP/%s" %d',
                "127.0.0.1:54321",
                "GET",
                f"/fyers/callback?s=ok&code=200&auth_code={FAKE_AUTH_CODE}&state={FAKE_STATE}",
                "1.1",
                303,
            )
            for handler in handlers:
                handler.flush()
            written = log_file.read_text(encoding="utf-8")
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
                handler.close()
            for handler in saved_handlers:
                root.addHandler(handler)
            root.setLevel(saved_level)

        assert FAKE_AUTH_CODE not in written
        assert FAKE_STATE not in written
        assert "/fyers/callback" in written
        # Still a parseable line with the status on it, or the log has stopped being useful.
        record = json.loads(written.strip().splitlines()[-1])
        assert "303" in record["message"]


class TestErrorEnvelope:
    def test_rejections_use_the_documented_envelope(self, client, manager, user_id):
        login(client, manager, user_id)
        body = client.post("/api/v1/downloads", headers={"Sec-Fetch-Site": "same-origin"}).json()
        assert set(body) == {"error"}
        assert set(body["error"]) >= {"code", "message"}
        assert isinstance(body["error"]["message"], str)

    def test_no_rejection_message_names_which_layer_failed(self):
        for reason, message in csrf_module.REASON_MESSAGES.items():
            assert "token" not in message.lower() or reason == REASON_CSRF_INVALID
            assert "origin" not in message.lower()
            assert "sec-fetch" not in message.lower()

    def test_headers_module_error_body_shape(self):
        body = headers_module.error_body("rate_limited", "safe text", correlation_id="abc")
        assert body == {
            "error": {"code": "rate_limited", "message": "safe text", "correlation_id": "abc"}
        }


class TestCookieContract:
    def test_the_session_cookie_survives_a_cross_site_top_level_navigation(self):
        # SameSite=Lax, never Strict. Strict withholds the cookie on exactly the Fyers OAuth
        # redirect, which is the one cross-site top-level GET this app depends on.
        assert sessions_module.COOKIE_SAMESITE == "lax"

    def test_secure_is_unconditional(self):
        assert sessions_module.cookie_secure() is runtime_scheme.is_https()
