"""Key encryption key providers.

The KEK never touches SQLite. It wraps exactly one thing, the DEK in ``crypto_key.wrapped_dek``,
which is why switching provider is a rewrap of 32 bytes rather than a re-encryption of every
secret field.

Three providers share one interface (SECURITY.md section 2.5):

    keyfile      the default. A generated 32 byte file at ~/.expirymanager/master.key, 0600 inside
                 a 0700 directory. The only option that satisfies both zero configuration and
                 unattended start after a reboot.
    keyring      opt in. Convenient, but on macOS the Keychain ACL is granted to the Python
                 interpreter, so it must never be described as protection against local processes.
    passphrase   opt in. Argon2id over a user passphrase. The scheduler cannot run after a reboot
                 until someone types it, which the UI has to say plainly.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw

from expirymanager.security.crypto import KEY_LEN

KEYFILE = "keyfile"
KEYRING = "keyring"
PASSPHRASE = "passphrase"

# Matches the CHECK constraint on crypto_key.kek_provider.
PROVIDER_NAMES = (KEYFILE, KEYRING, PASSPHRASE)
DEFAULT_PROVIDER = KEYFILE

KEYRING_SERVICE = "expirymanager"
KEYRING_USERNAME = "kek"

# SECURITY.md section 2.5. The names differ between libraries: argon2-cffi calls these time_cost
# and parallelism, the cryptography KDF calls them iterations and lanes. The values are the same.
PASSPHRASE_TIME_COST = 3
PASSPHRASE_PARALLELISM = 4
PASSPHRASE_MEMORY_COST = 262144
PASSPHRASE_SALT_LEN = 16
PASSPHRASE_KDF = "argon2id"


class KekError(Exception):
    """Base class for KEK provider failures."""


class KekUnavailableError(KekError):
    """The backing store for this provider cannot be reached (missing library, locked keyring)."""


class KekMaterialMissingError(KekError):
    """The provider has no key material yet. Call provision()."""


class KekPermissionError(KekError):
    """The key file is readable by group or other, so it is refused the way sshd refuses one."""


class PassphraseRequiredError(KekError):
    """A passphrase provider was built without a passphrase, so it cannot derive the KEK."""


def fsync_directory(directory: Path) -> None:
    """Make a newly created directory entry durable.

    Writing and fsyncing the file is not enough: after a crash the entry itself can be missing.
    """
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_secret_file(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    """Create ``path`` 0600 with O_EXCL, fsync the file and then the parent directory.

    O_EXCL rather than a plain open, so a symlink planted at the target path is a failure rather
    than a write through it.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # On a replacement, write beside the target and rename over it, so a crash mid-write never
    # leaves a truncated key where a whole one used to be.
    target = path.with_name(path.name + ".tmp") if overwrite else path
    if overwrite and target.exists():
        target.unlink()
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if overwrite:
        os.replace(target, path)
    fsync_directory(path.parent)


def assert_owner_only(path: Path) -> None:
    """Refuse a secret file that any group or other bit can read."""
    mode = path.stat().st_mode
    if mode & 0o077:
        raise KekPermissionError(
            f"{path} is accessible to group or other, expected mode 0600"
        )


@runtime_checkable
class KekProvider(Protocol):
    """Everything ``security.keys`` needs from a provider."""

    name: str

    def provision(self) -> None:
        """Create the key material if it does not exist. Idempotent."""

    def exists(self) -> bool:
        """True when material is already present."""

    def kek(self) -> bytes:
        """The 32 byte key encryption key."""

    def kdf_params(self) -> str | None:
        """JSON for crypto_key.kdf_params, or None for providers that derive nothing."""

    def destroy(self) -> None:
        """Remove the material. Only called after a successful rewrap onto another provider."""


class KeyFileKekProvider:
    """The default provider: a random 32 byte file at ~/.expirymanager/master.key."""

    name = KEYFILE

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def provision(self) -> None:
        if self.path.exists():
            # Do not touch an existing key: overwriting it would orphan every wrapped DEK.
            assert_owner_only(self.path)
            return
        write_secret_file(self.path, os.urandom(KEY_LEN))

    def kek(self) -> bytes:
        if not self.path.exists():
            raise KekMaterialMissingError(f"{self.path} does not exist")
        assert_owner_only(self.path)
        data = self.path.read_bytes()
        if len(data) != KEY_LEN:
            raise KekError(f"{self.path} is not {KEY_LEN} bytes")
        return data

    def kdf_params(self) -> str | None:
        return None

    def destroy(self) -> None:
        if self.path.exists():
            self.path.unlink()
            fsync_directory(self.path.parent)

    def repair_mode(self) -> None:
        """Tighten the mode to 0600. Offered for a UI action, never done implicitly at startup."""
        if self.path.exists():
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)


class KeyringKekProvider:
    """Opt in provider backed by the OS keyring. The KEK is stored base64 encoded."""

    name = KEYRING

    def __init__(
        self, service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME
    ) -> None:
        self.service = service
        self.username = username

    def _keyring(self):
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover, keyring is a declared dependency
            raise KekUnavailableError("the keyring package is not installed") from exc
        return keyring

    def _get(self) -> str | None:
        keyring = self._keyring()
        try:
            return keyring.get_password(self.service, self.username)
        except Exception as exc:
            raise KekUnavailableError(f"the OS keyring could not be read: {type(exc).__name__}") from exc

    def exists(self) -> bool:
        return self._get() is not None

    def provision(self) -> None:
        if self._get() is not None:
            return
        keyring = self._keyring()
        secret = base64.b64encode(os.urandom(KEY_LEN)).decode("ascii")
        try:
            keyring.set_password(self.service, self.username, secret)
        except Exception as exc:
            raise KekUnavailableError(f"the OS keyring could not be written: {type(exc).__name__}") from exc

    def kek(self) -> bytes:
        stored = self._get()
        if stored is None:
            raise KekMaterialMissingError("no KEK is stored in the OS keyring")
        try:
            data = base64.b64decode(stored, validate=True)
        except Exception as exc:
            raise KekError("the keyring entry is not valid base64") from exc
        if len(data) != KEY_LEN:
            raise KekError(f"the keyring entry is not {KEY_LEN} bytes")
        return data

    def kdf_params(self) -> str | None:
        return None

    def destroy(self) -> None:
        keyring = self._keyring()
        try:
            keyring.delete_password(self.service, self.username)
        except Exception:
            # Deleting an entry that is already gone is the desired end state, not a failure.
            return


class PassphraseKekProvider:
    """Opt in provider deriving the KEK from a passphrase with Argon2id.

    The salt and the cost parameters are stored in crypto_key.kdf_params so a later unlock can
    reproduce the derivation. The passphrase itself is never stored anywhere.
    """

    name = PASSPHRASE

    def __init__(
        self,
        passphrase: str | None,
        *,
        salt: bytes | None = None,
        time_cost: int = PASSPHRASE_TIME_COST,
        parallelism: int = PASSPHRASE_PARALLELISM,
        memory_cost: int = PASSPHRASE_MEMORY_COST,
    ) -> None:
        self._passphrase = passphrase
        self.salt = salt if salt is not None else os.urandom(PASSPHRASE_SALT_LEN)
        self.time_cost = time_cost
        self.parallelism = parallelism
        self.memory_cost = memory_cost

    @classmethod
    def from_params(cls, passphrase: str | None, params_json: str) -> "PassphraseKekProvider":
        """Rebuild a provider from the stored crypto_key.kdf_params JSON."""
        try:
            params = json.loads(params_json)
        except (TypeError, ValueError) as exc:
            raise KekError("kdf_params is not valid JSON") from exc
        if params.get("kdf") != PASSPHRASE_KDF:
            raise KekError("kdf_params names an unsupported KDF")
        try:
            salt = base64.b64decode(params["salt"], validate=True)
        except Exception as exc:
            raise KekError("kdf_params carries an invalid salt") from exc
        return cls(
            passphrase,
            salt=salt,
            time_cost=int(params["time_cost"]),
            parallelism=int(params["parallelism"]),
            memory_cost=int(params["memory_cost"]),
        )

    def exists(self) -> bool:
        # Nothing is stored, so the provider is usable exactly when a passphrase has been supplied.
        return self._passphrase is not None

    def provision(self) -> None:
        if self._passphrase is None:
            raise PassphraseRequiredError("a passphrase is required to derive the KEK")

    def kek(self) -> bytes:
        if self._passphrase is None:
            raise PassphraseRequiredError("a passphrase is required to derive the KEK")
        return hash_secret_raw(
            secret=self._passphrase.encode("utf-8"),
            salt=self.salt,
            time_cost=self.time_cost,
            memory_cost=self.memory_cost,
            parallelism=self.parallelism,
            hash_len=KEY_LEN,
            type=Argon2Type.ID,
        )

    def kdf_params(self) -> str | None:
        return json.dumps(
            {
                "kdf": PASSPHRASE_KDF,
                "salt": base64.b64encode(self.salt).decode("ascii"),
                "time_cost": self.time_cost,
                "parallelism": self.parallelism,
                "memory_cost": self.memory_cost,
                "hash_len": KEY_LEN,
            },
            sort_keys=True,
        )

    def destroy(self) -> None:
        # There is no stored material to remove. Drop the in-memory copy so a switched-away
        # provider cannot keep deriving.
        self._passphrase = None


def build_kek_provider(
    name: str,
    *,
    key_path: Path | str | None = None,
    passphrase: str | None = None,
    kdf_params: str | None = None,
) -> KekProvider:
    """Construct the provider named in crypto_key.kek_provider or in settings."""
    if name == KEYFILE:
        if key_path is None:
            raise KekError("the keyfile provider needs a path")
        return KeyFileKekProvider(key_path)
    if name == KEYRING:
        return KeyringKekProvider()
    if name == PASSPHRASE:
        if kdf_params:
            return PassphraseKekProvider.from_params(passphrase, kdf_params)
        return PassphraseKekProvider(passphrase)
    raise KekError(f"unknown KEK provider: {name!r}")
