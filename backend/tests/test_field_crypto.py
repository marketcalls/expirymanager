"""W04 tests for the EM1 envelope, the location-bound AAD and Argon2id password hashing.

Every value here is synthetic. No real credential appears in this file.
"""

from __future__ import annotations

import os

import pytest

from expirymanager.security import crypto

SYNTHETIC_SECRET = "synthetic-app-secret-not-a-real-credential"


@pytest.fixture()
def dek() -> bytes:
    return crypto.generate_dek()


def test_envelope_layout_is_magic_version_nonce_ciphertext(dek: bytes) -> None:
    blob = crypto.encrypt_field(
        SYNTHETIC_SECRET,
        key=dek,
        key_ver=1,
        table="broker_credential",
        column="app_secret_enc",
        row_id="row-1",
    )
    assert blob[:3] == b"EM1"
    assert blob[3] == 1
    assert len(blob) == crypto.MIN_ENVELOPE_LEN + len(SYNTHETIC_SECRET.encode())
    parsed = crypto.parse_envelope(blob)
    assert parsed.key_ver == 1
    assert len(parsed.nonce) == crypto.NONCE_LEN


def test_round_trip_for_every_secret_column(dek: bytes) -> None:
    columns = [
        ("broker_credential", "app_secret_enc", "cred-1"),
        ("broker_token", "access_token_enc", "tok-1"),
        ("broker_token", "refresh_token_enc", "tok-1"),
    ]
    for table, column, row_id in columns:
        plaintext = f"synthetic-{column}"
        blob = crypto.encrypt_field(
            plaintext, key=dek, key_ver=1, table=table, column=column, row_id=row_id
        )
        assert (
            crypto.decrypt_field(
                blob, keys={1: dek}, table=table, column=column, row_id=row_id
            ).decode()
            == plaintext
        )


def test_nonce_is_fresh_per_encryption(dek: bytes) -> None:
    first = crypto.encrypt_field(
        SYNTHETIC_SECRET, key=dek, key_ver=1, table="t", column="c", row_id="r"
    )
    second = crypto.encrypt_field(
        SYNTHETIC_SECRET, key=dek, key_ver=1, table="t", column="c", row_id="r"
    )
    assert crypto.parse_envelope(first).nonce != crypto.parse_envelope(second).nonce
    assert first != second


def test_ciphertext_tamper_is_detected(dek: bytes) -> None:
    blob = bytearray(
        crypto.encrypt_field(
            SYNTHETIC_SECRET, key=dek, key_ver=1, table="t", column="c", row_id="r"
        )
    )
    blob[-1] ^= 0x01
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(bytes(blob), keys={1: dek}, table="t", column="c", row_id="r")


def test_nonce_tamper_is_detected(dek: bytes) -> None:
    blob = bytearray(
        crypto.encrypt_field(
            SYNTHETIC_SECRET, key=dek, key_ver=1, table="t", column="c", row_id="r"
        )
    )
    blob[crypto.HEADER_LEN] ^= 0x01
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(bytes(blob), keys={1: dek}, table="t", column="c", row_id="r")


def test_relocating_to_another_column_fails(dek: bytes) -> None:
    blob = crypto.encrypt_field(
        SYNTHETIC_SECRET,
        key=dek,
        key_ver=1,
        table="broker_credential",
        column="app_secret_enc",
        row_id="cred-1",
    )
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(
            blob,
            keys={1: dek},
            table="broker_token",
            column="access_token_enc",
            row_id="cred-1",
        )


def test_relocating_to_another_row_fails(dek: bytes) -> None:
    blob = crypto.encrypt_field(
        SYNTHETIC_SECRET,
        key=dek,
        key_ver=1,
        table="broker_credential",
        column="app_secret_enc",
        row_id="cred-1",
    )
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(
            blob,
            keys={1: dek},
            table="broker_credential",
            column="app_secret_enc",
            row_id="cred-2",
        )


def test_relocating_to_another_table_fails(dek: bytes) -> None:
    blob = crypto.encrypt_field(
        SYNTHETIC_SECRET, key=dek, key_ver=1, table="table_a", column="col", row_id="row"
    )
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(blob, keys={1: dek}, table="table_b", column="col", row_id="row")


def test_key_version_is_part_of_the_aad(dek: bytes) -> None:
    blob = bytearray(
        crypto.encrypt_field(
            SYNTHETIC_SECRET, key=dek, key_ver=1, table="t", column="c", row_id="r"
        )
    )
    blob[3] = 2
    # The same DEK is offered for version 2, so only the AAD binding can reject this.
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(bytes(blob), keys={2: dek}, table="t", column="c", row_id="r")


def test_wrong_key_fails(dek: bytes) -> None:
    blob = crypto.encrypt_field(
        SYNTHETIC_SECRET, key=dek, key_ver=1, table="t", column="c", row_id="r"
    )
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(
            blob, keys={1: crypto.generate_dek()}, table="t", column="c", row_id="r"
        )


def test_wrong_magic_is_rejected_before_any_crypto(dek: bytes) -> None:
    blob = bytearray(
        crypto.encrypt_field(
            SYNTHETIC_SECRET, key=dek, key_ver=1, table="t", column="c", row_id="r"
        )
    )
    blob[:3] = b"XX1"

    def exploding_lookup(_key_ver: int) -> bytes:
        raise AssertionError("the key must not be fetched for a malformed envelope")

    with pytest.raises(crypto.EnvelopeFormatError):
        crypto.decrypt(
            bytes(blob), keys=exploding_lookup, aad_for=lambda _v: crypto.KEK_WRAP_AAD
        )


def test_truncated_envelope_is_rejected() -> None:
    with pytest.raises(crypto.EnvelopeFormatError):
        crypto.parse_envelope(b"EM1" + bytes([1]) + os.urandom(crypto.NONCE_LEN))
    with pytest.raises(crypto.EnvelopeFormatError):
        crypto.parse_envelope(b"")


def test_zero_key_version_is_rejected() -> None:
    blob = b"EM1" + bytes([0]) + os.urandom(crypto.NONCE_LEN + crypto.TAG_LEN)
    with pytest.raises(crypto.EnvelopeFormatError):
        crypto.parse_envelope(blob)


def test_is_envelope_never_raises() -> None:
    assert crypto.is_envelope(b"") is False
    assert crypto.is_envelope(None) is False
    assert crypto.is_envelope("not bytes") is False


def test_unknown_key_version_is_reported_distinctly(dek: bytes) -> None:
    blob = crypto.encrypt_field(
        SYNTHETIC_SECRET, key=dek, key_ver=2, table="t", column="c", row_id="r"
    )
    with pytest.raises(crypto.UnknownKeyVersionError):
        crypto.decrypt_field(blob, keys={1: dek}, table="t", column="c", row_id="r")


def test_short_key_is_refused() -> None:
    with pytest.raises(crypto.InvalidKeyError):
        crypto.encrypt(b"x", key=b"tooshort", key_ver=1, aad=b"aad")


def test_key_version_must_fit_one_byte(dek: bytes) -> None:
    with pytest.raises(crypto.InvalidKeyError):
        crypto.encrypt(b"x", key=dek, key_ver=256, aad=b"aad")
    with pytest.raises(crypto.InvalidKeyError):
        crypto.encrypt(b"x", key=dek, key_ver=0, aad=b"aad")


def test_field_aad_is_the_documented_string() -> None:
    assert crypto.field_aad("broker_token", "access_token_enc", "tok-1", 3) == (
        b"expirymanager|v1|broker_token|access_token_enc|tok-1|3"
    )


def test_wrapped_dek_round_trip() -> None:
    kek = crypto.generate_dek()
    dek = crypto.generate_dek()
    wrapped = crypto.encrypt_wrapped_dek(dek, kek=kek, key_ver=1)
    assert crypto.decrypt_wrapped_dek(wrapped, kek=kek) == dek
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_wrapped_dek(wrapped, kek=crypto.generate_dek())


def test_wrapped_dek_cannot_be_read_as_a_field(dek: bytes) -> None:
    kek = crypto.generate_dek()
    wrapped = crypto.encrypt_wrapped_dek(dek, kek=kek, key_ver=1)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(
            wrapped, keys={1: kek}, table="crypto_key", column="wrapped_dek", row_id="1"
        )


def test_password_hash_and_verify() -> None:
    password = "synthetic-local-passcode"
    phc = crypto.hash_password(password)
    assert phc.startswith("$argon2id$")
    assert crypto.verify_password(phc, password) is True
    assert crypto.verify_password(phc, "synthetic-wrong-passcode") is False
    assert crypto.verify_password("not-a-phc-string", password) is False


def test_password_hashes_are_salted() -> None:
    password = "synthetic-local-passcode"
    assert crypto.hash_password(password) != crypto.hash_password(password)


def test_password_needs_rehash_is_false_at_current_parameters() -> None:
    phc = crypto.hash_password("synthetic-local-passcode")
    assert crypto.password_needs_rehash(phc) is False
    assert crypto.password_needs_rehash("not-a-phc-string") is True


def test_password_parameters_match_the_security_document() -> None:
    hasher = crypto.password_hasher()
    assert hasher.time_cost == 3
    assert hasher.memory_cost == 65536
    assert hasher.parallelism == 4
    assert hasher.hash_len == 32
    assert hasher.salt_len == 16
