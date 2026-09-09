"""Argon2id hashing for the local app passcode, plus the lockout arithmetic.

Every parameter and every primitive comes from `security.crypto`. There is exactly one
`argon2.PasswordHasher` in this process and it lives there, because two hashers configured
independently drift, and the drift shows up as `check_needs_rehash` returning True forever and
rewriting the stored hash on every single login.

What this module adds on top of crypto.py is the policy that only the HTTP layer cares about: the
minimum length, the needs-rehash upgrade path expressed as one call, the equal-cost verify for an
unknown username, and the pure lockout arithmetic.

Verification costs 50 to 100 ms. Calling it directly from an async route blocks the event loop and
makes the scheduler stutter on every login attempt, so every function here has an `_async`
counterpart that goes through `run_in_threadpool`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from starlette.concurrency import run_in_threadpool

from expirymanager.security import crypto

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "MAX_PASSWORD_LENGTH",
    "LOCKOUT_THRESHOLD",
    "LOCKOUT_DURATION",
    "PasswordPolicyError",
    "VerifyResult",
    "validate_password",
    "hash_password",
    "hash_password_async",
    "verify_password",
    "verify_password_async",
    "verify_dummy",
    "verify_dummy_async",
    "needs_rehash",
    "record_failure",
    "record_success",
    "is_locked",
    "lock_retry_after",
]

# API.md section 1: `password (min 12 chars)`. Length is the only rule enforced. A composition rule
# would push the user towards a shorter password that satisfies a checklist, and this passcode
# guards a loopback app whose real perimeter is the filesystem.
MIN_PASSWORD_LENGTH = 12

# Argon2id has no 72 byte ceiling the way bcrypt does, so this bound exists only to stop a
# multi-megabyte body turning a login attempt into a memory-hard denial of service against itself.
MAX_PASSWORD_LENGTH = 1024

# SECURITY.md section 4.
LOCKOUT_THRESHOLD = 10
LOCKOUT_DURATION = timedelta(minutes=15)

# A hash of a value nobody knows, used to spend the same time on an unknown username as on a wrong
# password. Built once, lazily, because building it costs a full Argon2id hash and paying that at
# import time would slow every process start including `--check`.
_dummy_phc: str | None = None


class PasswordPolicyError(ValueError):
    """The supplied password does not meet the length policy."""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """The outcome of one verification.

    `upgraded_phc` is set only when the password was correct and the stored hash predates the
    current Argon2id parameters. The caller writes it back; nothing here touches the database.
    """

    ok: bool
    upgraded_phc: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def validate_password(password: str) -> None:
    """Raise `PasswordPolicyError` unless the password is acceptable to store."""
    if not isinstance(password, str):
        raise PasswordPolicyError("password must be text")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must be at most {MAX_PASSWORD_LENGTH} bytes"
        )


def hash_password(password: str, *, validate: bool = True) -> str:
    """Return the full Argon2id PHC string for `app_user.password_phc`."""
    if validate:
        validate_password(password)
    return crypto.hash_password(password)


async def hash_password_async(password: str, *, validate: bool = True) -> str:
    """`hash_password` off the event loop."""
    if validate:
        # Cheap and synchronous, and raising here keeps the error out of the worker thread.
        validate_password(password)
    return await run_in_threadpool(crypto.hash_password, password)


def verify_password(password_phc: str, password: str) -> VerifyResult:
    """Verify and, when the hash is stale, produce its replacement in the same call.

    SECURITY.md section 4 requires `check_needs_rehash` after every successful verify. Returning
    the upgraded hash rather than a bare boolean is what stops that requirement from being quietly
    dropped by a caller that only looked at the truthiness.
    """
    if not crypto.verify_password(password_phc, password):
        return VerifyResult(ok=False)
    if crypto.password_needs_rehash(password_phc):
        return VerifyResult(ok=True, upgraded_phc=crypto.hash_password(password))
    return VerifyResult(ok=True)


async def verify_password_async(password_phc: str, password: str) -> VerifyResult:
    """`verify_password` off the event loop."""
    return await run_in_threadpool(verify_password, password_phc, password)


def _dummy_hash() -> str:
    global _dummy_phc
    if _dummy_phc is None:
        _dummy_phc = crypto.hash_password(secrets.token_urlsafe(32))
    return _dummy_phc


def verify_dummy() -> VerifyResult:
    """Spend one Argon2id verification against a hash of an unknown value.

    Called on the unknown-username branch of login. Without it, an unknown username returns in
    microseconds and a known one takes 50 to 100 ms, which is a username oracle that no amount of
    care over the response body can close.
    """
    crypto.verify_password(_dummy_hash(), "")
    return VerifyResult(ok=False)


async def verify_dummy_async() -> VerifyResult:
    """`verify_dummy` off the event loop, so the equal cost is genuinely equal."""
    return await run_in_threadpool(verify_dummy)


def needs_rehash(password_phc: str) -> bool:
    """True when the stored hash predates the current Argon2id parameters."""
    return crypto.password_needs_rehash(password_phc)


def is_locked(locked_until: datetime | str | None, *, now: datetime | None = None) -> bool:
    """True while the account is inside its lockout window."""
    deadline = _parse_deadline(locked_until)
    if deadline is None:
        return False
    return _now(now) < deadline


def lock_retry_after(
    locked_until: datetime | str | None, *, now: datetime | None = None
) -> int:
    """Whole seconds to put in the `Retry-After` header of a 423. Never negative, never zero."""
    deadline = _parse_deadline(locked_until)
    if deadline is None:
        return 0
    remaining = (deadline - _now(now)).total_seconds()
    return max(1, int(remaining + 0.999)) if remaining > 0 else 0


def record_failure(
    failed_attempts: int, *, now: datetime | None = None
) -> tuple[int, datetime | None]:
    """The new `(failed_attempts, locked_until)` for `app_user` after one failed login.

    Pure arithmetic with no database access, so the transaction boundary belongs to the caller and
    this rule stays testable without a schema.
    """
    attempts = max(0, int(failed_attempts)) + 1
    if attempts >= LOCKOUT_THRESHOLD:
        return attempts, _now(now) + LOCKOUT_DURATION
    return attempts, None


def record_success() -> tuple[int, None]:
    """The new `(failed_attempts, locked_until)` after a successful login."""
    return 0, None


def _now(now: datetime | None) -> datetime:
    if now is not None:
        return now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _parse_deadline(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = value.strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
