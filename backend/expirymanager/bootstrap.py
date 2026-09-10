"""Idempotent first-run provisioning, and the three-state answer the SPA reads before it renders.

Two responsibilities that belong together because they are two views of the same question.

Provisioning is what has to be true before any route can work: the schema is migrated, and the key
hierarchy exists so that the very first credential the user saves can be encrypted. Both are safe
to repeat, so the lifespan runs them unconditionally on every start rather than testing for a
first run. A first run is then not a special case in the code, which is the only way it stays
correct: a conditional first-run path is exercised once per install and never again.

The status side answers `GET /api/v1/bootstrap`. Three states matter to the frontend:

  needs setup   the schema and keys are in place but no user account exists yet
  needs login   a user exists, so the browser needs a session
  ready         a user exists and, usually, a broker token as well

`provisioned` and `has_user` are separate fields because they answer different questions.
`provisioned` says the instance is capable of storing an encrypted secret, which is true from the
first successful startup onward. `has_user` says the setup wizard has been completed. The SPA
requires both before it will render the application.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from expirymanager import paths as paths_module
from expirymanager.brokers.fyers import tokens as tokens_module
from expirymanager.db import migrate as migrate_module
from expirymanager.security.kek import KeyFileKekProvider
from expirymanager.security.keys import KeyManager, SqliteCryptoKeyStore
from expirymanager.version import __version__

__all__ = [
    "TOKEN_STATE_NONE",
    "BootstrapStatus",
    "EngineDbapi",
    "apply_migrations",
    "build_key_manager",
    "ensure_key_hierarchy",
    "read_status",
]

log = logging.getLogger(__name__)

# What `token_state` reports when there is no token row at all. Every other value is the
# `broker_token.state` column verbatim, so the UI never has to guess which vocabulary it is in.
TOKEN_STATE_NONE = "none"


class EngineDbapi:
    """A DB-API-shaped facade over a SQLAlchemy engine, for `SqliteCryptoKeyStore`.

    `security/keys.py` takes a raw connection because the key hierarchy has to work before any
    mapper is configured. Handing it a checked-out pool connection for the life of the process
    would be wrong twice over: it would hold one of eight pooled connections forever, and
    `KeyManager.encrypt_field` bumps a use counter from whichever thread pool thread ran the
    encryption, so a single shared sqlite3 connection would be used concurrently.

    Each call here borrows a connection, runs inside one transaction and materialises the rows
    before giving it back. That keeps the per-connection PRAGMAs from `db/sqlite.py`, which is
    what `secure_delete` on wrapped key material depends on.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def execute(self, sql: str, params: Any = ()) -> "_MaterialisedRows":
        with self._engine.begin() as connection:
            result = connection.exec_driver_sql(sql, tuple(params))
            rows = [tuple(row) for row in result.fetchall()] if result.returns_rows else []
        return _MaterialisedRows(rows)

    def commit(self) -> None:
        # Each execute already committed. Present because the store calls it.
        return None


class _MaterialisedRows:
    """The subset of a DB-API cursor that `SqliteCryptoKeyStore` reads."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


def apply_migrations(engine: Engine) -> list[int]:
    """Bring the operational schema up to date. Returns the versions applied on this run."""
    applied = migrate_module.migrate(engine)
    if applied:
        log.info("schema migrated", extra={"migrations_applied": applied})
    return applied


def build_key_manager(engine: Engine, paths: paths_module.Paths) -> KeyManager:
    """The process-wide `KeyManager` over the keyfile KEK provider.

    The keyfile at `~/.expirymanager/master.key` is the primary provider from SECURITY.md section
    3. The keyring and passphrase providers exist in `security/kek.py` and are reachable through
    `KeyManager.rewrap`, which is how a later settings route switches provider without touching a
    single field ciphertext.
    """
    return KeyManager(SqliteCryptoKeyStore(EngineDbapi(engine)), KeyFileKekProvider(paths.master_key))


def ensure_key_hierarchy(key_manager: KeyManager) -> int:
    """Provision the KEK file and the first DEK if they are missing. Returns the active version."""
    version = key_manager.ensure_dek()
    log.info("key hierarchy ready", extra={"key_ver": version})
    return version


@dataclass(frozen=True, slots=True)
class BootstrapStatus:
    """The body of `GET /api/v1/bootstrap`."""

    provisioned: bool
    has_user: bool
    has_credentials: bool
    broker_connected: bool
    token_state: str
    token_expires_at: str | None
    needs_reauth: bool
    data_dir: str
    app_version: str
    duckdb_version: str


def _scalar(engine: Engine, sql: str) -> Any:
    with engine.connect() as connection:
        row = connection.execute(text(sql)).first()
    return None if row is None else row[0]


def _has_active_key(engine: Engine) -> bool:
    return bool(_scalar(engine, "SELECT count(*) FROM crypto_key WHERE state = 'active'"))


def read_status(
    engine: Engine,
    *,
    paths: paths_module.Paths,
    token_broker: Any = None,
    duckdb_version: str = "",
) -> BootstrapStatus:
    """Read the three-state answer. Synchronous SQLite, so call it through `run_in_threadpool`.

    `token_broker` is optional so the status is still readable during a degraded startup. Without
    it the broker fields fall back to what the tables say, which is the same answer one step less
    fresh.
    """
    has_user = bool(_scalar(engine, "SELECT count(*) FROM app_user"))
    has_credentials = bool(
        _scalar(engine, "SELECT count(*) FROM broker_credential WHERE is_active = 1")
    )
    provisioned = _has_active_key(engine)

    token_state = TOKEN_STATE_NONE
    token_expires_at: str | None = None
    broker_connected = False

    record = None
    if token_broker is not None:
        try:
            record = token_broker.record()
        except Exception:  # noqa: BLE001 - a broker read must never take down the public route
            log.warning("could not read the broker token state for bootstrap")
            record = None

    if record is not None:
        # Evaluated against the clock rather than read off the column. The column is only
        # rewritten by a login, a rejection or the 03:00 sweep, so reporting it verbatim made
        # bootstrap answer "active" for hours after the token had actually expired.
        token_state = record.effective_state()
        token_expires_at = record.access_expires_at
        broker_connected = bool(record.is_usable)
    elif has_credentials:
        row = _read_token_row(engine)
        if row is not None:
            stored_state, token_expires_at = row
            token_state = tokens_module.effective_token_state(stored_state, token_expires_at)

    if token_broker is not None and broker_connected:
        # A record can be marked active and still be past its JWT exp, which is what the broker
        # decodes locally. Trust the broker over the column when both are available.
        try:
            broker_connected = bool(token_broker.has_valid_token())
        except Exception:  # noqa: BLE001
            log.warning("could not validate the broker token for bootstrap")

    needs_reauth = has_credentials and not broker_connected

    return BootstrapStatus(
        provisioned=provisioned,
        has_user=has_user,
        has_credentials=has_credentials,
        broker_connected=broker_connected,
        token_state=token_state,
        token_expires_at=token_expires_at,
        needs_reauth=needs_reauth,
        data_dir=str(paths.root),
        app_version=__version__,
        duckdb_version=duckdb_version,
    )


def _read_token_row(engine: Engine) -> tuple[str, str | None] | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, access_expires_at FROM broker_token "
                "WHERE state <> 'revoked' ORDER BY generation DESC LIMIT 1"
            )
        ).first()
    if row is None:
        return None
    return (str(row[0]), None if row[1] is None else str(row[1]))
