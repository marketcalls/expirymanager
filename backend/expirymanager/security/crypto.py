"""AES-256-GCM field encryption, the EM1 envelope and Argon2id password hashing.

The envelope layout is fixed by SECURITY.md section 2.2:

    magic     3 bytes    b"EM1"
    key_ver   1 byte     uint8, which DEK version encrypted this
    nonce    12 bytes
    ct+tag    n bytes    AESGCM.encrypt() output

Nothing here reads the database or the filesystem. Key material arrives from
``security.keys``; this module only turns bytes into envelopes and back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"EM1"
MAGIC_LEN = 3
KEY_VER_LEN = 1
NONCE_LEN = 12
TAG_LEN = 16
HEADER_LEN = MAGIC_LEN + KEY_VER_LEN

# magic, key version, nonce and the GCM tag of an empty plaintext. Anything shorter cannot be a
# well formed envelope, so it is rejected before a key is ever fetched.
MIN_ENVELOPE_LEN = HEADER_LEN + NONCE_LEN + TAG_LEN

KEY_LEN = 32
MIN_KEY_VERSION = 1
MAX_KEY_VERSION = 255

AAD_PREFIX = "expirymanager|v1"

# The wrapped DEK is not bound to a row: it is the one ciphertext whose location is the key file
# and the crypto_key table itself, so it carries its own constant AAD.
KEK_WRAP_AAD = b"expirymanager|kek-wrap|v1"

# SECURITY.md section 4: argon2-cffi library defaults, which already exceed the OWASP Argon2id
# baseline of m=19456, t=2, p=1.
PASSWORD_TIME_COST = 3
PASSWORD_MEMORY_COST = 65536
PASSWORD_PARALLELISM = 4
PASSWORD_HASH_LEN = 32
PASSWORD_SALT_LEN = 16


class CryptoError(Exception):
    """Base class for every failure raised by this module."""


class EnvelopeFormatError(CryptoError):
    """The blob is not a well formed EM1 envelope."""


class DecryptionError(CryptoError):
    """Authentication failed: wrong key, wrong AAD or tampered ciphertext."""


class UnknownKeyVersionError(CryptoError):
    """The envelope names a DEK version that is not loaded."""


class InvalidKeyError(CryptoError):
    """A key of the wrong length was supplied."""


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed EM1 blob. ``ciphertext`` still carries the trailing GCM tag."""

    key_ver: int
    nonce: bytes
    ciphertext: bytes


KeyLookup = Mapping[int, bytes] | Callable[[int], bytes]


def field_aad(table: str, column: str, row_id: str, key_ver: int) -> bytes:
    """Bind a ciphertext to its location.

    An attacker with write access to config.sqlite3 must not be able to move a ciphertext from one
    row or column to another and have it still decrypt (threat T5).
    """
    return f"{AAD_PREFIX}|{table}|{column}|{row_id}|{key_ver}".encode("utf-8")


def _check_key(key: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
        raise InvalidKeyError("key must be exactly 32 bytes")


def _check_key_version(key_ver: int) -> None:
    if not isinstance(key_ver, int) or isinstance(key_ver, bool):
        raise InvalidKeyError("key version must be an integer")
    if not MIN_KEY_VERSION <= key_ver <= MAX_KEY_VERSION:
        raise InvalidKeyError("key version must fit in one unsigned byte and start at 1")


def _as_bytes(value: bytes | str) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raise TypeError("plaintext must be str or bytes")


def parse_envelope(blob: bytes) -> Envelope:
    """Validate the framing of an EM1 blob without touching any key material."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise EnvelopeFormatError("envelope must be bytes")
    raw = bytes(blob)
    if len(raw) < MIN_ENVELOPE_LEN:
        raise EnvelopeFormatError("envelope is shorter than the minimum length")
    if raw[:MAGIC_LEN] != MAGIC:
        raise EnvelopeFormatError("envelope magic does not match")
    key_ver = raw[MAGIC_LEN]
    if key_ver < MIN_KEY_VERSION:
        raise EnvelopeFormatError("envelope key version must start at 1")
    nonce = raw[HEADER_LEN : HEADER_LEN + NONCE_LEN]
    ciphertext = raw[HEADER_LEN + NONCE_LEN :]
    return Envelope(key_ver=key_ver, nonce=nonce, ciphertext=ciphertext)


def envelope_key_version(blob: bytes) -> int:
    """The DEK version that produced this envelope. Used to drive rotation queries."""
    return parse_envelope(blob).key_ver


def is_envelope(blob: object) -> bool:
    """True when the blob passes framing validation. Never raises."""
    try:
        parse_envelope(blob)  # type: ignore[arg-type]
    except (EnvelopeFormatError, TypeError):
        return False
    return True


def _resolve_key(keys: KeyLookup, key_ver: int) -> bytes:
    if callable(keys):
        try:
            key = keys(key_ver)
        except KeyError as exc:
            raise UnknownKeyVersionError(f"no DEK loaded for version {key_ver}") from exc
    else:
        try:
            key = keys[key_ver]
        except KeyError as exc:
            raise UnknownKeyVersionError(f"no DEK loaded for version {key_ver}") from exc
    if key is None:
        raise UnknownKeyVersionError(f"no DEK loaded for version {key_ver}")
    _check_key(key)
    return bytes(key)


def encrypt(plaintext: bytes | str, *, key: bytes, key_ver: int, aad: bytes) -> bytes:
    """Produce an EM1 envelope over ``plaintext`` with an explicit AAD."""
    _check_key(key)
    _check_key_version(key_ver)
    nonce = os.urandom(NONCE_LEN)
    ciphertext = AESGCM(bytes(key)).encrypt(nonce, _as_bytes(plaintext), aad)
    return MAGIC + bytes([key_ver]) + nonce + ciphertext


def decrypt(blob: bytes, *, keys: KeyLookup, aad_for: Callable[[int], bytes]) -> bytes:
    """Open an EM1 envelope.

    ``aad_for`` takes the key version parsed out of the envelope, because the version is part of
    the AAD and is only known after the header has been read.
    """
    envelope = parse_envelope(blob)
    key = _resolve_key(keys, envelope.key_ver)
    try:
        return AESGCM(key).decrypt(
            envelope.nonce, envelope.ciphertext, aad_for(envelope.key_ver)
        )
    except InvalidTag as exc:
        # The message stays generic on purpose: distinguishing a wrong key from a wrong AAD from a
        # flipped byte would be an oracle, and none of the three is actionable for the caller.
        raise DecryptionError("envelope failed authentication") from exc


def encrypt_field(
    plaintext: bytes | str,
    *,
    key: bytes,
    key_ver: int,
    table: str,
    column: str,
    row_id: str,
) -> bytes:
    """Encrypt one database field, bound to its table, column, row id and key version."""
    return encrypt(plaintext, key=key, key_ver=key_ver, aad=field_aad(table, column, row_id, key_ver))


def decrypt_field(
    blob: bytes,
    *,
    keys: KeyLookup,
    table: str,
    column: str,
    row_id: str,
) -> bytes:
    """Decrypt one database field. Fails if the ciphertext has been relocated."""
    return decrypt(
        blob,
        keys=keys,
        aad_for=lambda key_ver: field_aad(table, column, row_id, key_ver),
    )


def encrypt_wrapped_dek(dek: bytes, *, kek: bytes, key_ver: int) -> bytes:
    """Wrap a DEK under a KEK. The envelope goes into crypto_key.wrapped_dek."""
    _check_key(dek)
    return encrypt(dek, key=kek, key_ver=key_ver, aad=KEK_WRAP_AAD)


def decrypt_wrapped_dek(blob: bytes, *, kek: bytes) -> bytes:
    """Unwrap a DEK. The KEK is the same for every version, so the lookup ignores the version."""
    dek = decrypt(blob, keys=lambda _key_ver: kek, aad_for=lambda _key_ver: KEK_WRAP_AAD)
    _check_key(dek)
    return dek


def generate_dek() -> bytes:
    """A fresh 32 byte data encryption key."""
    return os.urandom(KEY_LEN)


_password_hasher = PasswordHasher(
    time_cost=PASSWORD_TIME_COST,
    memory_cost=PASSWORD_MEMORY_COST,
    parallelism=PASSWORD_PARALLELISM,
    hash_len=PASSWORD_HASH_LEN,
    salt_len=PASSWORD_SALT_LEN,
)


def password_hasher() -> PasswordHasher:
    """The shared Argon2id hasher, so every caller uses one parameter set."""
    return _password_hasher


def hash_password(password: str) -> str:
    """Return the full Argon2id PHC string for app_user.password_phc.

    Callers in async code must run this through ``run_in_threadpool``: it costs 50 to 100 ms and
    would otherwise block the event loop on every login attempt.
    """
    return _password_hasher.hash(password)


def verify_password(password_phc: str, password: str) -> bool:
    """Constant-shape verify. Returns False rather than raising on any mismatch."""
    try:
        return _password_hasher.verify(password_phc, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_phc: str) -> bool:
    """True when the stored hash predates the current parameters and should be upgraded."""
    try:
        return _password_hasher.check_needs_rehash(password_phc)
    except InvalidHashError:
        return True
