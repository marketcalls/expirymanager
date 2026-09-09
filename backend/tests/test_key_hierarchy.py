"""W04 tests for the KEK providers and the DEK lifecycle.

No real credential appears here. The keyring provider is exercised against an in-memory stand-in
installed into sys.modules, so the developer's own OS keychain is never touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import types

import pytest

from expirymanager.security import crypto, kek, keys

# The crypto_key DDL from DATA-MODEL.md section 1.2. W02 owns the migration; this copy only
# exists so the store can be tested without depending on that item.
CRYPTO_KEY_DDL = """
CREATE TABLE crypto_key (
    version       INTEGER PRIMARY KEY,
    wrapped_dek   BLOB    NOT NULL,
    kek_provider  TEXT    NOT NULL CHECK (kek_provider IN ('keyfile','keyring','passphrase')),
    kdf_params    TEXT,
    state         TEXT    NOT NULL CHECK (state IN ('active','retiring','retired')),
    created_at    TEXT    NOT NULL,
    retired_at    TEXT,
    use_count     INTEGER NOT NULL DEFAULT 0
);
"""

SYNTHETIC_SECRET = "synthetic-app-secret-not-a-real-credential"
SYNTHETIC_PASSPHRASE = "synthetic-passphrase-for-tests"


@pytest.fixture()
def key_file(tmp_path):
    directory = tmp_path / ".expirymanager"
    directory.mkdir(mode=0o700)
    return directory / "master.key"


@pytest.fixture()
def fake_keyring(monkeypatch):
    """A minimal in-memory replacement for the keyring package."""
    store: dict[tuple[str, str], str] = {}
    module = types.ModuleType("keyring")

    def get_password(service, username):
        return store.get((service, username))

    def set_password(service, username, password):
        store[(service, username)] = password

    def delete_password(service, username):
        del store[(service, username)]

    module.get_password = get_password
    module.set_password = set_password
    module.delete_password = delete_password
    monkeypatch.setitem(sys.modules, "keyring", module)
    return store


def make_manager(store, provider) -> keys.KeyManager:
    manager = keys.KeyManager(store, provider)
    manager.ensure_dek()
    return manager


def test_key_file_is_created_0600_with_32_bytes(key_file) -> None:
    provider = kek.KeyFileKekProvider(key_file)
    provider.provision()
    assert key_file.exists()
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert len(key_file.read_bytes()) == crypto.KEY_LEN
    assert len(provider.kek()) == crypto.KEY_LEN


def test_key_file_provision_is_idempotent(key_file) -> None:
    provider = kek.KeyFileKekProvider(key_file)
    provider.provision()
    first = provider.kek()
    provider.provision()
    assert provider.kek() == first


def test_loose_key_file_permissions_are_refused(key_file) -> None:
    provider = kek.KeyFileKekProvider(key_file)
    provider.provision()
    key_file.chmod(0o644)
    with pytest.raises(kek.KekPermissionError):
        provider.kek()
    provider.repair_mode()
    assert len(provider.kek()) == crypto.KEY_LEN


def test_missing_key_file_reports_missing_material(key_file) -> None:
    provider = kek.KeyFileKekProvider(key_file)
    with pytest.raises(kek.KekMaterialMissingError):
        provider.kek()


def test_key_file_of_wrong_length_is_refused(key_file) -> None:
    kek.write_secret_file(key_file, os.urandom(16))
    with pytest.raises(kek.KekError):
        kek.KeyFileKekProvider(key_file).kek()


def test_keyring_provider_round_trip(fake_keyring) -> None:
    provider = kek.KeyringKekProvider()
    assert provider.exists() is False
    provider.provision()
    assert provider.exists() is True
    first = provider.kek()
    assert len(first) == crypto.KEY_LEN
    provider.provision()
    assert provider.kek() == first
    provider.destroy()
    assert provider.exists() is False
    with pytest.raises(kek.KekMaterialMissingError):
        provider.kek()


def test_passphrase_provider_is_deterministic_for_one_salt() -> None:
    provider = kek.PassphraseKekProvider(
        SYNTHETIC_PASSPHRASE, time_cost=1, parallelism=1, memory_cost=8
    )
    first = provider.kek()
    assert len(first) == crypto.KEY_LEN
    rebuilt = kek.PassphraseKekProvider.from_params(
        SYNTHETIC_PASSPHRASE, provider.kdf_params()
    )
    assert rebuilt.kek() == first
    wrong = kek.PassphraseKekProvider.from_params(
        "synthetic-wrong-passphrase", provider.kdf_params()
    )
    assert wrong.kek() != first


def test_passphrase_provider_needs_a_passphrase() -> None:
    provider = kek.PassphraseKekProvider(None)
    with pytest.raises(kek.PassphraseRequiredError):
        provider.kek()
    with pytest.raises(kek.PassphraseRequiredError):
        provider.provision()


def test_passphrase_kdf_params_carry_the_documented_parameters() -> None:
    provider = kek.PassphraseKekProvider(SYNTHETIC_PASSPHRASE)
    params = json.loads(provider.kdf_params())
    assert params["kdf"] == "argon2id"
    assert params["time_cost"] == 3
    assert params["parallelism"] == 4
    assert params["memory_cost"] == 262144
    assert params["hash_len"] == 32
    assert len(params["salt"]) > 0
    # One derivation at the real cost, to prove the documented parameters actually run.
    assert len(provider.kek()) == crypto.KEY_LEN


def test_build_kek_provider_selects_by_name(key_file) -> None:
    assert isinstance(
        kek.build_kek_provider("keyfile", key_path=key_file), kek.KeyFileKekProvider
    )
    assert isinstance(kek.build_kek_provider("keyring"), kek.KeyringKekProvider)
    assert isinstance(
        kek.build_kek_provider("passphrase", passphrase=SYNTHETIC_PASSPHRASE),
        kek.PassphraseKekProvider,
    )
    with pytest.raises(kek.KekError):
        kek.build_kek_provider("hsm")
    assert kek.DEFAULT_PROVIDER == "keyfile"
    assert kek.PROVIDER_NAMES == ("keyfile", "keyring", "passphrase")


def test_write_secret_file_refuses_to_clobber(key_file) -> None:
    kek.write_secret_file(key_file, b"a" * 32)
    with pytest.raises(FileExistsError):
        kek.write_secret_file(key_file, b"b" * 32)
    kek.write_secret_file(key_file, b"c" * 32, overwrite=True)
    assert key_file.read_bytes() == b"c" * 32
    assert key_file.stat().st_mode & 0o777 == 0o600


def test_ensure_dek_creates_one_active_version(key_file) -> None:
    store = keys.InMemoryCryptoKeyStore()
    manager = make_manager(store, kek.KeyFileKekProvider(key_file))
    rows = store.list_keys()
    assert len(rows) == 1
    assert rows[0].version == 1
    assert rows[0].state == "active"
    assert rows[0].kek_provider == "keyfile"
    assert rows[0].kdf_params is None
    assert manager.active_version == 1
    # ensure_dek is idempotent: a second call must not mint a second key.
    assert manager.ensure_dek() == 1
    assert len(store.list_keys()) == 1


def test_manager_round_trip_and_use_count(key_file) -> None:
    store = keys.InMemoryCryptoKeyStore()
    manager = make_manager(store, kek.KeyFileKekProvider(key_file))
    blob = manager.encrypt_field(
        SYNTHETIC_SECRET,
        table="broker_credential",
        column="app_secret_enc",
        row_id="cred-1",
    )
    assert (
        manager.decrypt_text(
            blob, table="broker_credential", column="app_secret_enc", row_id="cred-1"
        )
        == SYNTHETIC_SECRET
    )
    assert store.list_keys()[0].use_count == 1


def test_wrapped_dek_is_not_the_dek(key_file) -> None:
    store = keys.InMemoryCryptoKeyStore()
    manager = make_manager(store, kek.KeyFileKekProvider(key_file))
    wrapped = store.list_keys()[0].wrapped_dek
    assert wrapped[:3] == b"EM1"
    assert manager.dek() not in wrapped


def test_rewrap_onto_another_provider_leaves_ciphertexts_identical(key_file, fake_keyring) -> None:
    store = keys.InMemoryCryptoKeyStore()
    manager = make_manager(store, kek.KeyFileKekProvider(key_file))
    blob = manager.encrypt_field(
        SYNTHETIC_SECRET,
        table="broker_credential",
        column="app_secret_enc",
        row_id="cred-1",
    )
    before = store.list_keys()[0].wrapped_dek
    dek_before = manager.dek()

    manager.rewrap(kek.KeyringKekProvider())

    after = store.list_keys()[0].wrapped_dek
    assert after != before
    assert store.list_keys()[0].kek_provider == "keyring"
    assert manager.dek() == dek_before
    # The field ciphertext is untouched, which is the entire point of the two level hierarchy.
    assert (
        manager.decrypt_text(
            blob, table="broker_credential", column="app_secret_enc", row_id="cred-1"
        )
        == SYNTHETIC_SECRET
    )


def test_rewrap_can_destroy_the_old_material(key_file, fake_keyring) -> None:
    store = keys.InMemoryCryptoKeyStore()
    old = kek.KeyFileKekProvider(key_file)
    manager = make_manager(store, old)
    manager.rewrap(kek.KeyringKekProvider(), destroy_old=True)
    assert not key_file.exists()


def test_rewrap_to_passphrase_stores_kdf_params(key_file) -> None:
    store = keys.InMemoryCryptoKeyStore()
    manager = make_manager(store, kek.KeyFileKekProvider(key_file))
    passphrase_provider = kek.PassphraseKekProvider(
        SYNTHETIC_PASSPHRASE, time_cost=1, parallelism=1, memory_cost=8
    )
    manager.rewrap(passphrase_provider)
    row = store.list_keys()[0]
    assert row.kek_provider == "passphrase"
    assert json.loads(row.kdf_params)["kdf"] == "argon2id"

    # A restart reconstructs the provider from the stored parameters plus the typed passphrase.
    reopened = keys.KeyManager(
        store, kek.PassphraseKekProvider.from_params(SYNTHETIC_PASSPHRASE, row.kdf_params)
    )
    assert reopened.active_version == 1
    wrong = keys.KeyManager(
        store,
        kek.PassphraseKekProvider.from_params("synthetic-wrong-passphrase", row.kdf_params),
    )
    with pytest.raises(crypto.DecryptionError):
        wrong.active_version


def test_rotation_keeps_two_versions_readable(key_file) -> None:
    store = keys.InMemoryCryptoKeyStore()
    manager = make_manager(store, kek.KeyFileKekProvider(key_file))
    old_blob = manager.encrypt_field(
        SYNTHETIC_SECRET,
        table="broker_credential",
        column="app_secret_enc",
        row_id="cred-1",
    )
    assert crypto.envelope_key_version(old_blob) == 1

    new_version = manager.rotate_dek()
    assert new_version == 2
    assert manager.active_version == 2
    assert manager.loaded_versions() == (1, 2)
    assert manager.dek(1) != manager.dek(2)

    # Written under version 1, still readable while the sweep runs.
    assert (
        manager.decrypt_text(
            old_blob, table="broker_credential", column="app_secret_enc", row_id="cred-1"
        )
        == SYNTHETIC_SECRET
    )
    new_blob = manager.reencrypt(
        old_blob, table="broker_credential", column="app_secret_enc", row_id="cred-1"
    )
    assert crypto.envelope_key_version(new_blob) == 2

    manager.retire_version(1)
    assert manager.loaded_versions() == (2,)
    assert (
        manager.decrypt_text(
            new_blob, table="broker_credential", column="app_secret_enc", row_id="cred-1"
        )
        == SYNTHETIC_SECRET
    )
    with pytest.raises(crypto.UnknownKeyVersionError):
        manager.decrypt_field(
            old_blob, table="broker_credential", column="app_secret_enc", row_id="cred-1"
        )


def test_active_version_cannot_be_retired(key_file) -> None:
    store = keys.InMemoryCryptoKeyStore()
    manager = make_manager(store, kek.KeyFileKekProvider(key_file))
    with pytest.raises(keys.KeyRotationError):
        manager.retire_version(manager.active_version)


def test_manager_without_any_key_raises(key_file) -> None:
    manager = keys.KeyManager(
        keys.InMemoryCryptoKeyStore(), kek.KeyFileKekProvider(key_file)
    )
    with pytest.raises(keys.NoActiveKeyError):
        manager.active_version


def test_verify_reports_a_wrong_key(key_file, tmp_path) -> None:
    store = keys.InMemoryCryptoKeyStore()
    manager = make_manager(store, kek.KeyFileKekProvider(key_file))
    blob = manager.encrypt_field(
        SYNTHETIC_SECRET, table="t", column="c", row_id="r"
    )
    manager.verify([(blob, "t", "c", "r")])
    with pytest.raises(keys.KeyManagerError):
        manager.verify([(blob, "t", "c", "other-row")])


def test_sqlite_store_persists_the_hierarchy(key_file) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(CRYPTO_KEY_DDL)
    store = keys.SqliteCryptoKeyStore(connection)
    provider = kek.KeyFileKekProvider(key_file)
    manager = make_manager(store, provider)
    blob = manager.encrypt_field(
        SYNTHETIC_SECRET,
        table="broker_credential",
        column="app_secret_enc",
        row_id="cred-1",
    )

    # A fresh manager over the same rows, as a restart would build.
    reopened = keys.KeyManager(keys.SqliteCryptoKeyStore(connection), provider)
    assert reopened.active_version == 1
    assert (
        reopened.decrypt_text(
            blob, table="broker_credential", column="app_secret_enc", row_id="cred-1"
        )
        == SYNTHETIC_SECRET
    )

    reopened.rotate_dek()
    states = {row.version: row.state for row in store.list_keys()}
    assert states == {1: "retiring", 2: "active"}
    reopened.retire_version(1)
    row = connection.execute(
        "SELECT state, retired_at, use_count FROM crypto_key WHERE version = 1"
    ).fetchone()
    assert row[0] == "retired"
    assert row[1] is not None
    assert row[2] == 1
    connection.close()
