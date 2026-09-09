"""The data encryption key: generate, wrap, cache, rewrap and rotate.

Two levels, as SECURITY.md section 2.4 sets out:

    KEK   from a pluggable provider, never in SQLite
       wraps
    DEK   random, generated once at first run, held in crypto_key.wrapped_dek
       encrypts
    field ciphertexts

Switching provider rewraps 32 bytes and leaves every field ciphertext byte for byte identical.
Rotating the DEK is the expensive path and is driven by the ``key_ver`` byte in the envelope: both
keys stay loaded while the re-encryption sweep runs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable, Protocol

from expirymanager.security.crypto import (
    CryptoError,
    MAX_KEY_VERSION,
    UnknownKeyVersionError,
    decrypt_field,
    decrypt_wrapped_dek,
    encrypt_field,
    encrypt_wrapped_dek,
    generate_dek,
)
from expirymanager.security.kek import KekProvider

STATE_ACTIVE = "active"
STATE_RETIRING = "retiring"
STATE_RETIRED = "retired"

# A retired key is excluded from the decrypt lookup, so it may only be retired once every field
# encrypted under it has been rewritten.
USABLE_STATES = (STATE_ACTIVE, STATE_RETIRING)


class KeyManagerError(Exception):
    """Base class for key management failures."""


class NoActiveKeyError(KeyManagerError):
    """The crypto_key table has no active row. Run ensure_dek() first."""


class KeyRotationError(KeyManagerError):
    """The key version space is exhausted or the requested rotation is not possible."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(slots=True)
class CryptoKeyRecord:
    """One row of the crypto_key table."""

    version: int
    wrapped_dek: bytes
    kek_provider: str
    kdf_params: str | None
    state: str
    created_at: str
    retired_at: str | None = None
    use_count: int = 0


class CryptoKeyStore(Protocol):
    """The persistence surface a KeyManager needs. W02 owns the schema, not this module."""

    def list_keys(self) -> list[CryptoKeyRecord]: ...

    def insert_key(self, record: CryptoKeyRecord) -> None: ...

    def update_wrapping(
        self, version: int, wrapped_dek: bytes, kek_provider: str, kdf_params: str | None
    ) -> None: ...

    def set_state(self, version: int, state: str, retired_at: str | None) -> None: ...

    def increment_use_count(self, version: int) -> None: ...


class InMemoryCryptoKeyStore:
    """A store with no database behind it. Used by tests and by first-run dry runs."""

    def __init__(self) -> None:
        self._rows: dict[int, CryptoKeyRecord] = {}

    def list_keys(self) -> list[CryptoKeyRecord]:
        # Copies, so a caller mutating a record cannot edit the store behind its own back.
        return [replace(row) for row in sorted(self._rows.values(), key=lambda r: r.version)]

    def insert_key(self, record: CryptoKeyRecord) -> None:
        if record.version in self._rows:
            raise KeyManagerError(f"crypto_key version {record.version} already exists")
        self._rows[record.version] = replace(record)

    def update_wrapping(
        self, version: int, wrapped_dek: bytes, kek_provider: str, kdf_params: str | None
    ) -> None:
        row = self._rows[version]
        row.wrapped_dek = wrapped_dek
        row.kek_provider = kek_provider
        row.kdf_params = kdf_params

    def set_state(self, version: int, state: str, retired_at: str | None) -> None:
        row = self._rows[version]
        row.state = state
        row.retired_at = retired_at

    def increment_use_count(self, version: int) -> None:
        self._rows[version].use_count += 1


class SqliteCryptoKeyStore:
    """crypto_key access over a DB-API connection.

    Deliberately raw SQL rather than an ORM model: the key hierarchy has to work during first-run
    bootstrap, before any mapper is configured, and it must never depend on a session factory that
    itself needs decrypted credentials.
    """

    TABLE = "crypto_key"

    def __init__(self, connection) -> None:
        self._conn = connection

    def _commit(self) -> None:
        commit = getattr(self._conn, "commit", None)
        if callable(commit):
            commit()

    def list_keys(self) -> list[CryptoKeyRecord]:
        cursor = self._conn.execute(
            f"SELECT version, wrapped_dek, kek_provider, kdf_params, state, created_at, "
            f"retired_at, use_count FROM {self.TABLE} ORDER BY version"
        )
        return [
            CryptoKeyRecord(
                version=int(row[0]),
                wrapped_dek=bytes(row[1]),
                kek_provider=row[2],
                kdf_params=row[3],
                state=row[4],
                created_at=row[5],
                retired_at=row[6],
                use_count=int(row[7]),
            )
            for row in cursor.fetchall()
        ]

    def insert_key(self, record: CryptoKeyRecord) -> None:
        self._conn.execute(
            f"INSERT INTO {self.TABLE} (version, wrapped_dek, kek_provider, kdf_params, state, "
            f"created_at, retired_at, use_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.version,
                record.wrapped_dek,
                record.kek_provider,
                record.kdf_params,
                record.state,
                record.created_at,
                record.retired_at,
                record.use_count,
            ),
        )
        self._commit()

    def update_wrapping(
        self, version: int, wrapped_dek: bytes, kek_provider: str, kdf_params: str | None
    ) -> None:
        self._conn.execute(
            f"UPDATE {self.TABLE} SET wrapped_dek = ?, kek_provider = ?, kdf_params = ? "
            f"WHERE version = ?",
            (wrapped_dek, kek_provider, kdf_params, version),
        )
        self._commit()

    def set_state(self, version: int, state: str, retired_at: str | None) -> None:
        self._conn.execute(
            f"UPDATE {self.TABLE} SET state = ?, retired_at = ? WHERE version = ?",
            (state, retired_at, version),
        )
        self._commit()

    def increment_use_count(self, version: int) -> None:
        self._conn.execute(
            f"UPDATE {self.TABLE} SET use_count = use_count + 1 WHERE version = ?", (version,)
        )
        self._commit()


class KeyManager:
    """Loads the DEKs once, then encrypts and decrypts fields with location-bound AAD."""

    def __init__(self, store: CryptoKeyStore, provider: KekProvider) -> None:
        self._store = store
        self._provider = provider
        self._lock = threading.RLock()
        self._deks: dict[int, bytes] = {}
        self._active_version: int | None = None
        self._loaded = False

    @property
    def provider(self) -> KekProvider:
        return self._provider

    def clear_cache(self) -> None:
        """Drop every unwrapped DEK. Called on provider switch and on shutdown."""
        with self._lock:
            self._deks.clear()
            self._active_version = None
            self._loaded = False

    def ensure_dek(self) -> int:
        """Provision the KEK and the first DEK if this is a first run. Idempotent."""
        with self._lock:
            self._provider.provision()
            rows = self._store.list_keys()
            active = [row for row in rows if row.state == STATE_ACTIVE]
            if active:
                self.clear_cache()
                return self._load()
            kek = self._provider.kek()
            dek = generate_dek()
            version = max((row.version for row in rows), default=0) + 1
            if version > MAX_KEY_VERSION:
                raise KeyRotationError("the one byte key version space is exhausted")
            self._store.insert_key(
                CryptoKeyRecord(
                    version=version,
                    wrapped_dek=encrypt_wrapped_dek(dek, kek=kek, key_ver=version),
                    kek_provider=self._provider.name,
                    kdf_params=self._provider.kdf_params(),
                    state=STATE_ACTIVE,
                    created_at=_now(),
                )
            )
            self.clear_cache()
            return self._load()

    def _load(self) -> int:
        if self._loaded and self._active_version is not None:
            return self._active_version
        rows = self._store.list_keys()
        usable = [row for row in rows if row.state in USABLE_STATES]
        if not usable:
            raise NoActiveKeyError("crypto_key holds no active or retiring key")
        kek = self._provider.kek()
        deks: dict[int, bytes] = {}
        for row in usable:
            deks[row.version] = decrypt_wrapped_dek(row.wrapped_dek, kek=kek)
        active = [row for row in usable if row.state == STATE_ACTIVE]
        if len(active) != 1:
            raise NoActiveKeyError(
                f"expected exactly one active crypto_key row, found {len(active)}"
            )
        self._deks = deks
        self._active_version = active[0].version
        self._loaded = True
        return self._active_version

    @property
    def active_version(self) -> int:
        with self._lock:
            return self._load()

    def dek(self, version: int | None = None) -> bytes:
        """The unwrapped DEK for a version, or the active one."""
        with self._lock:
            active = self._load()
            wanted = active if version is None else version
            try:
                return self._deks[wanted]
            except KeyError as exc:
                raise UnknownKeyVersionError(f"no DEK loaded for version {wanted}") from exc

    def loaded_versions(self) -> tuple[int, ...]:
        with self._lock:
            self._load()
            return tuple(sorted(self._deks))

    def encrypt_field(self, plaintext: bytes | str, *, table: str, column: str, row_id: str) -> bytes:
        """Encrypt one field under the active DEK, bound to its location."""
        with self._lock:
            version = self._load()
            key = self._deks[version]
        blob = encrypt_field(
            plaintext, key=key, key_ver=version, table=table, column=column, row_id=row_id
        )
        self._store.increment_use_count(version)
        return blob

    def decrypt_field(self, blob: bytes, *, table: str, column: str, row_id: str) -> bytes:
        """Decrypt one field. Dispatches on the key version carried in the envelope."""
        with self._lock:
            self._load()
            keys = dict(self._deks)
        return decrypt_field(blob, keys=keys, table=table, column=column, row_id=row_id)

    def decrypt_text(self, blob: bytes, *, table: str, column: str, row_id: str) -> str:
        return self.decrypt_field(blob, table=table, column=column, row_id=row_id).decode("utf-8")

    def rewrap(self, new_provider: KekProvider, *, destroy_old: bool = False) -> None:
        """Move every wrapped DEK onto another KEK provider.

        No field ciphertext is touched, which is the whole point of the two level hierarchy. The
        old material is only destroyed once every row has been rewrapped successfully.
        """
        with self._lock:
            old_provider = self._provider
            old_kek = old_provider.kek()
            new_provider.provision()
            new_kek = new_provider.kek()
            rows = self._store.list_keys()
            rewrapped: list[tuple[int, bytes]] = []
            for row in rows:
                if row.state == STATE_RETIRED:
                    continue
                dek = decrypt_wrapped_dek(row.wrapped_dek, kek=old_kek)
                rewrapped.append(
                    (row.version, encrypt_wrapped_dek(dek, kek=new_kek, key_ver=row.version))
                )
            if not rewrapped:
                raise NoActiveKeyError("there is no wrapped DEK to rewrap")
            params = new_provider.kdf_params()
            for version, wrapped in rewrapped:
                self._store.update_wrapping(version, wrapped, new_provider.name, params)
            self._provider = new_provider
            self.clear_cache()
            self._load()
            if destroy_old and old_provider is not new_provider:
                old_provider.destroy()

    def rotate_dek(self) -> int:
        """Start a DEK rotation: mint a new active version and mark the previous one retiring.

        The caller then re-encrypts every secret field and calls retire_version() on the old one.
        Both keys stay loaded in between, so nothing is unreadable during the sweep.
        """
        with self._lock:
            current = self._load()
            rows = self._store.list_keys()
            version = max(row.version for row in rows) + 1
            if version > MAX_KEY_VERSION:
                raise KeyRotationError("the one byte key version space is exhausted")
            kek = self._provider.kek()
            dek = generate_dek()
            self._store.insert_key(
                CryptoKeyRecord(
                    version=version,
                    wrapped_dek=encrypt_wrapped_dek(dek, kek=kek, key_ver=version),
                    kek_provider=self._provider.name,
                    kdf_params=self._provider.kdf_params(),
                    state=STATE_ACTIVE,
                    created_at=_now(),
                )
            )
            self._store.set_state(current, STATE_RETIRING, None)
            self.clear_cache()
            return self._load()

    def retire_version(self, version: int) -> None:
        """Take a retiring key out of service once nothing is encrypted under it any more."""
        with self._lock:
            if version == self._load():
                raise KeyRotationError("the active key version cannot be retired")
            self._store.set_state(version, STATE_RETIRED, _now())
            self.clear_cache()
            self._load()

    def reencrypt(
        self, blob: bytes, *, table: str, column: str, row_id: str
    ) -> bytes:
        """Read a field under whatever version wrote it and write it back under the active one."""
        plaintext = self.decrypt_field(blob, table=table, column=column, row_id=row_id)
        return self.encrypt_field(plaintext, table=table, column=column, row_id=row_id)

    def verify(self, records: Iterable[tuple[bytes, str, str, str]]) -> None:
        """Decrypt a sample of stored fields to prove the loaded DEKs are the right ones."""
        for blob, table, column, row_id in records:
            try:
                self.decrypt_field(blob, table=table, column=column, row_id=row_id)
            except CryptoError as exc:
                raise KeyManagerError(
                    f"{table}.{column} could not be decrypted with the loaded keys"
                ) from exc
