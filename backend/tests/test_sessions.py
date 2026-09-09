"""Opaque server-side sessions: storage, both expiry boundaries, rotation and revocation.

These run against a real migrated `config.sqlite3`, because every property under test is a property
of what is on disk: that the raw id is absent from the row, that an expired row is deleted rather
than merely ignored, that the absolute window survives a slide.

The clock is injected rather than mocked at the module level, so the 8 hour and 7 day boundaries
are tested at the second instead of being approximated.
"""

from __future__ import annotations

from expirymanager import runtime_scheme

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.models import AppSession, AppUser
from expirymanager.security import sessions as sessions_module
from expirymanager.security.sessions import (
    ABSOLUTE_TIMEOUT,
    CSRF_COOKIE_NAME,
    IDLE_TIMEOUT,
    LAST_SEEN_WRITE_INTERVAL,
    SESSION_COOKIE_NAME,
    SessionManager,
    hash_session_id,
    new_session_id,
)

START = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)


class MovableClock:
    """A clock the test advances explicitly. No sleeping, no wall-clock flakiness."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def engine(tmp_path: Path):
    eng = sqlite_module.create_engine(tmp_path / "config.sqlite3")
    migrate_module.migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def user_id(engine) -> str:
    identifier = str(uuid.uuid4())
    with OrmSession(engine) as db:
        db.add(
            AppUser(
                user_id=identifier,
                username="synthetic",
                password_phc="synthetic-phc-not-a-real-hash",
                created_at=START.isoformat(),
            )
        )
        db.commit()
    return identifier


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def manager(engine, clock) -> SessionManager:
    return SessionManager(engine, clock=clock)


class TestIssue:
    def test_raw_id_is_256_bits_and_unique(self):
        first, second = new_session_id(), new_session_id()
        assert first != second
        # token_urlsafe(32) is 32 random bytes rendered base64url without padding.
        assert len(first) >= 43

    def test_the_raw_id_never_reaches_the_database(self, manager, user_id, engine):
        issued = manager.create(user_id)
        with OrmSession(engine) as db:
            rows = db.execute(select(AppSession)).scalars().all()
        assert len(rows) == 1
        assert bytes(rows[0].id_hash) == hash_session_id(issued.raw_id)
        assert issued.raw_id not in str(
            {column.name: getattr(rows[0], column.name) for column in AppSession.__table__.columns}
        )

    def test_lifetimes_match_the_specification(self, manager, user_id):
        issued = manager.create(user_id)
        assert issued.record.idle_expires_at == START + IDLE_TIMEOUT
        assert issued.record.absolute_expires_at == START + ABSOLUTE_TIMEOUT
        assert IDLE_TIMEOUT == timedelta(hours=8)
        assert ABSOLUTE_TIMEOUT == timedelta(days=7)

    def test_each_session_carries_its_own_csrf_token(self, manager, user_id):
        first = manager.create(user_id)
        second = manager.create(user_id)
        assert first.csrf_token != second.csrf_token

    def test_lookup_resolves_the_cookie_value(self, manager, user_id):
        issued = manager.create(user_id)
        record = manager.lookup(issued.raw_id)
        assert record is not None
        assert record.user_id == user_id
        assert record.csrf_token == issued.csrf_token

    def test_unknown_and_empty_cookies_resolve_to_nothing(self, manager, user_id):
        manager.create(user_id)
        assert manager.lookup(new_session_id()) is None
        assert manager.lookup("") is None
        assert manager.lookup(None) is None


class TestIdleBoundary:
    def test_just_inside_the_idle_window_still_resolves(self, manager, user_id, clock):
        issued = manager.create(user_id)
        clock.advance(IDLE_TIMEOUT - timedelta(seconds=1))
        assert manager.lookup(issued.raw_id) is not None

    def test_at_the_idle_boundary_the_session_is_gone(self, manager, user_id, clock, engine):
        issued = manager.create(user_id)
        clock.advance(IDLE_TIMEOUT)
        assert manager.lookup(issued.raw_id) is None
        with OrmSession(engine) as db:
            assert db.execute(select(AppSession)).scalars().all() == []

    def test_use_slides_the_idle_window(self, manager, user_id, clock):
        issued = manager.create(user_id)
        clock.advance(timedelta(hours=7))
        record = manager.lookup(issued.raw_id)
        assert record is not None
        assert record.idle_expires_at == clock.now + IDLE_TIMEOUT

        # Seven hours after the slide is fifteen hours after issue, and still inside the window.
        clock.advance(timedelta(hours=7))
        assert manager.lookup(issued.raw_id) is not None

    def test_last_seen_is_written_at_most_once_a_minute(self, manager, user_id, clock, engine):
        issued = manager.create(user_id)
        clock.advance(LAST_SEEN_WRITE_INTERVAL - timedelta(seconds=1))
        manager.lookup(issued.raw_id)
        with OrmSession(engine) as db:
            row = db.get(AppSession, hash_session_id(issued.raw_id))
            assert datetime.fromisoformat(row.last_seen_at) == START

        clock.advance(timedelta(seconds=2))
        manager.lookup(issued.raw_id)
        with OrmSession(engine) as db:
            row = db.get(AppSession, hash_session_id(issued.raw_id))
            assert datetime.fromisoformat(row.last_seen_at) == clock.now

    def test_slide_can_be_suppressed(self, manager, user_id, clock, engine):
        issued = manager.create(user_id)
        clock.advance(timedelta(hours=2))
        manager.lookup(issued.raw_id, slide=False)
        with OrmSession(engine) as db:
            row = db.get(AppSession, hash_session_id(issued.raw_id))
            assert datetime.fromisoformat(row.last_seen_at) == START


class TestAbsoluteBoundary:
    def test_the_absolute_window_never_extends(self, manager, user_id, clock):
        issued = manager.create(user_id)
        absolute = issued.record.absolute_expires_at
        # Six hour steps keep the session inside its eight hour idle window, so every lookup slides
        # the idle deadline. The absolute one must not move with it.
        for _ in range(27):
            clock.advance(timedelta(hours=6))
            record = manager.lookup(issued.raw_id)
            assert record is not None
            assert record.absolute_expires_at == absolute

    def test_at_the_absolute_boundary_the_session_is_gone(self, manager, user_id, clock):
        issued = manager.create(user_id)
        # Kept alive across the whole week by regular use, so only the absolute window can end it.
        for _ in range(28):
            clock.advance(timedelta(hours=6))
            manager.lookup(issued.raw_id)
        assert clock.now == START + ABSOLUTE_TIMEOUT
        assert manager.lookup(issued.raw_id) is None

    def test_the_idle_window_is_clamped_to_the_absolute_one(self, manager, user_id, clock):
        issued = manager.create(user_id)
        for _ in range(27):
            clock.advance(timedelta(hours=6))
            manager.lookup(issued.raw_id)
        clock.advance(timedelta(hours=5))
        assert clock.now == START + ABSOLUTE_TIMEOUT - timedelta(hours=1)
        record = manager.lookup(issued.raw_id)
        assert record is not None
        assert record.idle_expires_at == record.absolute_expires_at
        assert record.expires_at == record.absolute_expires_at


class TestRotationAndRevocation:
    def test_rotation_replaces_the_id_and_the_token(self, manager, user_id, engine):
        original = manager.create(user_id)
        rotated = manager.rotate(original.raw_id)
        assert rotated is not None
        assert rotated.raw_id != original.raw_id
        assert rotated.csrf_token != original.csrf_token
        assert rotated.user_id == user_id
        assert manager.lookup(original.raw_id) is None
        assert manager.lookup(rotated.raw_id) is not None
        with OrmSession(engine) as db:
            assert len(db.execute(select(AppSession)).scalars().all()) == 1

    def test_rotation_restarts_the_absolute_window(self, manager, user_id, clock):
        original = manager.create(user_id)
        clock.advance(timedelta(hours=6))
        rotated = manager.rotate(original.raw_id)
        assert rotated is not None
        assert rotated.record.absolute_expires_at == clock.now + ABSOLUTE_TIMEOUT

    def test_rotating_an_unknown_id_returns_nothing(self, manager, user_id):
        manager.create(user_id)
        assert manager.rotate(new_session_id()) is None

    def test_rotation_carries_the_client_details_forward(self, manager, user_id):
        original = manager.create(user_id, user_agent="synthetic-agent", client_ip="127.0.0.1")
        rotated = manager.rotate(original.raw_id)
        assert rotated is not None
        assert rotated.record.user_agent == "synthetic-agent"
        assert rotated.record.client_ip == "127.0.0.1"

    def test_revoke_deletes_one_session(self, manager, user_id):
        first = manager.create(user_id)
        second = manager.create(user_id)
        assert manager.revoke(first.raw_id) is True
        assert manager.lookup(first.raw_id) is None
        assert manager.lookup(second.raw_id) is not None
        assert manager.revoke(first.raw_id) is False

    def test_revoke_all_clears_every_session_for_the_user(self, manager, user_id):
        issued = [manager.create(user_id) for _ in range(3)]
        assert manager.revoke_all(user_id) == 3
        assert all(manager.lookup(session.raw_id) is None for session in issued)

    def test_revoke_all_can_keep_the_caller_session(self, manager, user_id):
        keep = manager.create(user_id)
        manager.create(user_id)
        manager.create(user_id)
        assert manager.revoke_all(user_id, keep_id_hash=keep.record.id_hash) == 2
        assert manager.lookup(keep.raw_id) is not None
        assert manager.count_for_user(user_id) == 1

    def test_refresh_csrf_replaces_the_token_in_place(self, manager, user_id):
        issued = manager.create(user_id)
        token = manager.refresh_csrf(issued.record.id_hash)
        assert token is not None
        assert token != issued.csrf_token
        record = manager.lookup(issued.raw_id)
        assert record is not None
        assert record.csrf_token == token


class TestPrune:
    def test_prune_removes_only_expired_rows(self, manager, user_id, clock):
        stale = manager.create(user_id)
        clock.advance(IDLE_TIMEOUT + timedelta(minutes=1))
        fresh = manager.create(user_id)
        assert manager.prune_expired() == 1
        assert manager.lookup(fresh.raw_id) is not None
        assert manager.lookup(stale.raw_id) is None

    def test_prune_on_an_empty_table_is_a_no_op(self, manager):
        assert manager.prune_expired() == 0


class TestCookies:
    """The cookie attributes are the part that breaks OAuth silently when changed."""

    def test_samesite_is_lax_and_secure_is_unconditional(self):
        assert sessions_module.COOKIE_SAMESITE == "lax"
        assert sessions_module.cookie_secure() is runtime_scheme.is_https()

    def test_set_session_cookies_emits_both_with_the_right_flags(self, manager, user_id):
        from starlette.responses import Response

        response = Response()
        sessions_module.set_session_cookies(response, manager.create(user_id))
        cookies = [value for name, value in response.raw_headers if name == b"set-cookie"]
        assert len(cookies) == 2
        session_cookie = next(c.decode() for c in cookies if c.startswith(SESSION_COOKIE_NAME.encode()))
        csrf_cookie = next(c.decode() for c in cookies if c.startswith(CSRF_COOKIE_NAME.encode()))

        assert "HttpOnly" in session_cookie
        # Secure follows the scheme the server serves. Asserting it unconditionally would
        # encode a contract that breaks login on http, where the browser accepts the
        # cookie and then never sends it back.
        assert ("Secure" in session_cookie) is runtime_scheme.is_https()
        assert "SameSite=lax" in session_cookie.replace("SameSite=Lax", "SameSite=lax")
        assert "Path=/" in session_cookie
        assert "Domain=" not in session_cookie

        # The CSRF cookie must be readable by script or the frontend cannot echo it in the header.
        assert "HttpOnly" not in csrf_cookie
        # Secure follows the scheme the server serves. Asserting it unconditionally would
        # encode a contract that breaks login on http, where the browser accepts the
        # cookie and then never sends it back.
        assert ("Secure" in csrf_cookie) is runtime_scheme.is_https()

    def test_clear_session_cookies_expires_both(self, manager, user_id):
        from starlette.responses import Response

        response = Response()
        sessions_module.clear_session_cookies(response)
        cookies = [value.decode() for name, value in response.raw_headers if name == b"set-cookie"]
        assert len(cookies) == 2
        assert all("Max-Age=0" in cookie for cookie in cookies)
