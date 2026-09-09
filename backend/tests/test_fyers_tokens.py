"""The token broker: encrypted storage, the generation guard and the scheduled logout.

The SQLite tests run against a real temporary database and the real W04 key hierarchy, so the
round trip proves the ciphertext is actually decryptable and actually bound to its row. Every token
value here is synthetic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from expirymanager.brokers.fyers.throttle import (
    FyersGovernor,
    GovernorMode,
    InMemoryBudgetStore,
)
from expirymanager.brokers.fyers.tokens import (
    EXPIRY_MARGIN_SECONDS,
    FyersCredentials,
    InMemoryTokenStore,
    NoCredentialsError,
    SqliteCredentialStore,
    SqliteTokenStore,
    TokenBroker,
    TokenState,
    decode_jwt_expiry,
    token_fingerprint,
)
from expirymanager.db import migrate, sqlite
from expirymanager.security.crypto import DecryptionError
from expirymanager.security.keys import InMemoryCryptoKeyStore, KeyManager
from expirymanager.security.kek import KeyFileKekProvider

from tests.fyers_fake_transport import (
    FAKE_APP_ID,
    FAKE_APP_SECRET,
    make_access_token,
    token_valid_for,
)

CREDENTIAL_ID = "cred-0001"
REDIRECT_URI = "https://127.0.0.1:8000/fyers/callback"


class StubCredentials:
    def __init__(self, credentials: FyersCredentials | None) -> None:
        self.credentials = credentials

    def load_active(self) -> FyersCredentials | None:
        return self.credentials


def _credentials() -> FyersCredentials:
    return FyersCredentials(
        credential_id=CREDENTIAL_ID,
        app_id=FAKE_APP_ID,
        app_secret=FAKE_APP_SECRET,
        redirect_uri=REDIRECT_URI,
    )


def _broker(*, governor: FyersGovernor | None = None) -> TokenBroker:
    return TokenBroker(
        credentials=StubCredentials(_credentials()),
        tokens=InMemoryTokenStore(),
        governor=governor,
    )


# The JWT expiry decode


def test_the_jwt_exp_claim_is_decoded_locally() -> None:
    expires_at = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)
    decoded = decode_jwt_expiry(make_access_token(expires_at))
    assert decoded == expires_at


def test_a_token_that_is_not_a_jwt_answers_none_rather_than_raising() -> None:
    # An unknown expiry is not an expired one, and a crash on a malformed token would turn a
    # recoverable login into an outage.
    for value in ("", "not-a-jwt", "a.b", "a.b.c.d", "a.!!!!.c", "a.e30.c"):
        assert decode_jwt_expiry(value) is None


def test_the_fingerprint_is_a_full_sha256_and_is_not_the_token() -> None:
    token = token_valid_for()
    fingerprint = token_fingerprint(token)
    assert len(fingerprint) == 64
    assert token not in fingerprint


# Loading and storing


async def test_a_fresh_install_has_no_token_and_no_auth_context() -> None:
    broker = _broker()
    assert not broker.has_valid_token()
    assert await broker.auth_context() is None
    assert broker.record() is None
    assert not broker.auth_gate.is_set()


async def test_storing_a_login_opens_the_gate_and_bumps_the_generation() -> None:
    broker = _broker()
    token = token_valid_for()
    record = await broker.store_login(access_token=token)

    assert record.generation == 1
    assert broker.generation == 1
    assert record.state == TokenState.ACTIVE
    assert record.fingerprint == token_fingerprint(token)
    assert broker.has_valid_token()
    assert broker.auth_gate.is_set()

    second = await broker.store_login(access_token=token_valid_for(days=2))
    assert second.generation == 2
    assert broker.generation == 2


async def test_the_access_token_is_never_on_the_record() -> None:
    broker = _broker()
    token = token_valid_for()
    record = await broker.store_login(access_token=token)
    assert token not in repr(record)
    assert not any(token == getattr(record, name, None) for name in record.__slots__)


async def test_storing_a_login_without_credentials_is_refused() -> None:
    broker = TokenBroker(credentials=StubCredentials(None), tokens=InMemoryTokenStore())
    with pytest.raises(NoCredentialsError):
        await broker.store_login(access_token=token_valid_for())
    with pytest.raises(NoCredentialsError):
        broker.credentials()


async def test_an_expired_token_is_not_valid() -> None:
    broker = _broker()
    await broker.store_login(
        access_token=make_access_token(datetime.now(UTC) - timedelta(minutes=1))
    )
    assert not broker.has_valid_token()
    assert await broker.auth_context() is None


async def test_a_token_with_no_exp_claim_is_used_until_the_broker_rejects_it() -> None:
    # An unknown expiry is not an expired one. Guessing a lifetime would park a working session.
    broker = _broker()
    await broker.store_login(access_token="opaque-token-with-no-claims")
    assert broker.has_valid_token()
    assert broker.seconds_to_expiry() is None
    assert not broker.is_expiring()


async def test_the_proactive_parking_margin_is_visible_before_the_token_dies() -> None:
    broker = _broker()
    await broker.store_login(
        access_token=make_access_token(
            datetime.now(UTC) + timedelta(seconds=EXPIRY_MARGIN_SECONDS - 30)
        )
    )
    assert broker.has_valid_token()
    assert broker.is_expiring()
    assert 0 < broker.seconds_to_expiry() <= EXPIRY_MARGIN_SECONDS


# The generation guard


async def test_concurrent_auth_errors_produce_exactly_one_reaction() -> None:
    governor = FyersGovernor(budget_store=InMemoryBudgetStore())
    broker = _broker(governor=governor)
    await broker.store_login(access_token=token_valid_for())
    generation = broker.generation

    # Eight workers all see the same rejection within the same millisecond.
    results = await asyncio.gather(
        *[broker.on_auth_error(generation, reason="token expired") for _ in range(8)]
    )
    assert sum(1 for reacted in results if reacted) == 1
    assert not broker.auth_gate.is_set()
    assert governor.mode is GovernorMode.PAUSED_AUTH


async def test_a_stale_generation_is_ignored_after_a_newer_login() -> None:
    broker = _broker()
    await broker.store_login(access_token=token_valid_for())
    stale = broker.generation
    await broker.store_login(access_token=token_valid_for(days=2))

    # A worker that used the old token reports late. Parking on it would undo a good login.
    assert await broker.on_auth_error(stale) is False
    assert broker.auth_gate.is_set()
    assert broker.has_valid_token()


async def test_an_auth_error_marks_the_row_needs_reauth_and_drops_the_token() -> None:
    store = InMemoryTokenStore()
    broker = TokenBroker(credentials=StubCredentials(_credentials()), tokens=store)
    await broker.store_login(access_token=token_valid_for())

    await broker.on_auth_error(broker.generation, reason="invalid token")
    assert await broker.auth_context() is None
    assert not broker.has_valid_token()


async def test_a_login_after_an_auth_error_reopens_the_gate() -> None:
    governor = FyersGovernor(budget_store=InMemoryBudgetStore())
    broker = _broker(governor=governor)
    await broker.store_login(access_token=token_valid_for())
    await broker.on_auth_error(broker.generation, reason="token expired")
    assert not broker.auth_gate.is_set()

    record = await broker.store_login(access_token=token_valid_for(days=2))
    assert broker.auth_gate.is_set()
    assert record.generation == 2
    assert broker.has_valid_token()


# The scheduled logout


async def test_the_scheduled_logout_clears_the_token_and_parks_the_pipeline() -> None:
    governor = FyersGovernor(budget_store=InMemoryBudgetStore())
    broker = _broker(governor=governor)
    await broker.store_login(access_token=token_valid_for())

    await broker.scheduled_logout()

    assert not broker.has_valid_token()
    assert await broker.auth_context() is None
    assert not broker.auth_gate.is_set()
    assert governor.mode is GovernorMode.PAUSED_AUTH
    assert "03:00" in (governor.reason or "")


async def test_a_login_after_the_scheduled_logout_advances_the_generation() -> None:
    # Jobs parked by the logout resume at the exact task, so the generation must keep climbing
    # rather than restart and make a stale worker's report look current.
    broker = _broker()
    await broker.store_login(access_token=token_valid_for())
    await broker.scheduled_logout()
    record = await broker.store_login(access_token=token_valid_for(days=2))
    assert record.generation == 2


async def test_clearing_an_empty_broker_is_harmless() -> None:
    broker = _broker()
    await broker.clear(reason="disconnect")
    assert not broker.has_valid_token()


def test_there_is_no_refresh_entry_point() -> None:
    # SEBI discontinued the refresh token flow from 1 April 2026. A refresh method here would be
    # a path that can only ever fail, and a caller would build retry logic around it.
    assert not any("refresh" in name for name in dir(TokenBroker) if not name.startswith("_"))


# The real SQLite and encryption round trip


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / "expirymanager.sqlite3"
    engine = sqlite.create_engine(db_path)
    migrate.migrate(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def key_manager(tmp_path):
    provider = KeyFileKekProvider(tmp_path / "master.key")
    manager = KeyManager(InMemoryCryptoKeyStore(), provider)
    manager.ensure_dek()
    return manager


def _insert_credential(engine, key_manager) -> None:
    now = datetime.now(UTC).isoformat()
    secret_enc = key_manager.encrypt_field(
        FAKE_APP_SECRET,
        table="broker_credential",
        column="app_secret_enc",
        row_id=CREDENTIAL_ID,
    )
    with engine.begin() as connection:
        connection.execute(
            sa_text(
                "INSERT INTO broker_credential (credential_id, broker, label, app_id,"
                " app_secret_enc, redirect_uri, plan, key_ver, is_active, created_at, updated_at)"
                " VALUES (:cid, 'fyers', 'Test', :app_id, :secret, :redirect, 'standard',"
                " :key_ver, 1, :now, :now)"
            ),
            {
                "cid": CREDENTIAL_ID,
                "app_id": FAKE_APP_ID,
                "secret": secret_enc,
                "redirect": REDIRECT_URI,
                "key_ver": key_manager.active_version,
                "now": now,
            },
        )


async def test_a_token_round_trips_through_sqlite_encrypted(engine, key_manager) -> None:
    _insert_credential(engine, key_manager)
    broker = TokenBroker(
        credentials=SqliteCredentialStore(engine, key_manager),
        tokens=SqliteTokenStore(engine, key_manager),
    )
    assert broker.credentials().app_secret == FAKE_APP_SECRET

    token = token_valid_for()
    record = await broker.store_login(access_token=token, refresh_token="refresh.token.value")

    # The ciphertext on disk is not the token.
    with engine.connect() as connection:
        stored = connection.execute(
            sa_text("SELECT access_token_enc, state, generation FROM broker_token")
        ).fetchone()
    assert token.encode("utf-8") not in stored[0]
    assert stored[1] == TokenState.ACTIVE
    assert stored[2] == 1

    # A fresh broker over the same database reads the token back.
    reopened = TokenBroker(
        credentials=SqliteCredentialStore(engine, key_manager),
        tokens=SqliteTokenStore(engine, key_manager),
    )
    context = await reopened.auth_context()
    assert context is not None
    assert context.access_token == token
    assert context.app_id == FAKE_APP_ID
    assert context.generation == record.generation


async def test_the_ciphertext_cannot_be_decrypted_from_another_row(engine, key_manager) -> None:
    _insert_credential(engine, key_manager)
    store = SqliteTokenStore(engine, key_manager)
    record = store.save(
        credential_id=CREDENTIAL_ID,
        access_token=token_valid_for(),
        refresh_token=None,
        generation=1,
        access_expires_at=None,
        refresh_expires_at=None,
    )
    with engine.connect() as connection:
        blob = connection.execute(sa_text("SELECT access_token_enc FROM broker_token")).scalar()

    # The AAD binds the ciphertext to its table, column and row id, so a copied value is inert.
    with pytest.raises(DecryptionError):
        key_manager.decrypt_text(
            blob, table="broker_token", column="access_token_enc", row_id="another-row"
        )
    assert record.token_id


async def test_a_second_login_supersedes_the_first_row(engine, key_manager) -> None:
    _insert_credential(engine, key_manager)
    broker = TokenBroker(
        credentials=SqliteCredentialStore(engine, key_manager),
        tokens=SqliteTokenStore(engine, key_manager),
    )
    await broker.store_login(access_token=token_valid_for())
    await broker.store_login(access_token=token_valid_for(days=2))

    with engine.connect() as connection:
        rows = connection.execute(
            sa_text("SELECT state, generation FROM broker_token ORDER BY generation")
        ).fetchall()
    assert [row[0] for row in rows] == [TokenState.REVOKED, TokenState.ACTIVE]
    assert [row[1] for row in rows] == [1, 2]
    # There is never more than one usable token.
    assert sum(1 for row in rows if row[0] == TokenState.ACTIVE) == 1


async def test_the_scheduled_logout_destroys_the_ciphertext(engine, key_manager) -> None:
    _insert_credential(engine, key_manager)
    broker = TokenBroker(
        credentials=SqliteCredentialStore(engine, key_manager),
        tokens=SqliteTokenStore(engine, key_manager),
    )
    await broker.store_login(access_token=token_valid_for())
    await broker.scheduled_logout()

    with engine.connect() as connection:
        row = connection.execute(
            sa_text("SELECT access_token_enc, state, last_error FROM broker_token")
        ).fetchone()
    # A revoked row that still holds a usable bearer credential is a stored secret with no owner.
    assert row[0] == b""
    assert row[1] == TokenState.REVOKED
    assert "03:00" in row[2]

    reopened = TokenBroker(
        credentials=SqliteCredentialStore(engine, key_manager),
        tokens=SqliteTokenStore(engine, key_manager),
    )
    assert not reopened.has_valid_token()
    # The generation keeps climbing across the logout, so a parked worker's report stays stale.
    assert await reopened.store_login(access_token=token_valid_for()) is not None
    assert reopened.generation == 2
