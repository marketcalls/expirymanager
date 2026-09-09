"""Argon2id hashing for the local passcode, the needs-rehash upgrade and the lockout arithmetic.

Every password here is synthetic. Argon2id at the configured parameters costs 50 to 100 ms per
call, so the test count is kept deliberately low and the parameter checks read the configuration
rather than hashing again to observe it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher

from expirymanager.security import crypto, passwords

SYNTHETIC_PASSWORD = "synthetic-passcode-for-tests"
SYNTHETIC_OTHER = "synthetic-passcode-different"


class TestDelegation:
    """passwords.py must not build a second hasher. Two parameter sets drift, and the drift shows
    up as check_needs_rehash returning True forever and rewriting the hash on every login."""

    def test_module_has_no_password_hasher_of_its_own(self):
        constructed = [
            name
            for name, value in vars(passwords).items()
            if isinstance(value, PasswordHasher)
        ]
        assert constructed == []

    def test_hash_is_produced_by_the_shared_crypto_hasher(self, monkeypatch):
        calls: list[str] = []

        def spy(password: str) -> str:
            calls.append(password)
            return "phc-sentinel"

        monkeypatch.setattr(crypto, "hash_password", spy)
        assert passwords.hash_password(SYNTHETIC_PASSWORD) == "phc-sentinel"
        assert calls == [SYNTHETIC_PASSWORD]

    def test_parameters_are_the_documented_argon2id_defaults(self):
        hasher = crypto.password_hasher()
        assert hasher.time_cost == 3
        assert hasher.memory_cost == 65536
        assert hasher.parallelism == 4
        assert hasher.hash_len == 32
        assert hasher.salt_len == 16


class TestPolicy:
    def test_short_password_is_rejected(self):
        with pytest.raises(passwords.PasswordPolicyError):
            passwords.validate_password("a" * (passwords.MIN_PASSWORD_LENGTH - 1))

    def test_minimum_length_is_accepted(self):
        passwords.validate_password("a" * passwords.MIN_PASSWORD_LENGTH)

    def test_absurdly_long_password_is_rejected(self):
        with pytest.raises(passwords.PasswordPolicyError):
            passwords.validate_password("a" * (passwords.MAX_PASSWORD_LENGTH + 1))

    def test_hash_enforces_the_policy(self):
        with pytest.raises(passwords.PasswordPolicyError):
            passwords.hash_password("short")


class TestVerify:
    @pytest.fixture(scope="class")
    def phc(self) -> str:
        return passwords.hash_password(SYNTHETIC_PASSWORD)

    def test_correct_password_verifies(self, phc):
        result = passwords.verify_password(phc, SYNTHETIC_PASSWORD)
        assert result.ok is True
        assert bool(result) is True
        assert result.upgraded_phc is None

    def test_wrong_password_fails(self, phc):
        result = passwords.verify_password(phc, SYNTHETIC_OTHER)
        assert result.ok is False
        assert result.upgraded_phc is None

    def test_tampered_hash_fails_rather_than_raises(self, phc):
        tampered = phc[:-4] + ("aaaa" if not phc.endswith("aaaa") else "bbbb")
        assert passwords.verify_password(tampered, SYNTHETIC_PASSWORD).ok is False

    def test_malformed_hash_fails_rather_than_raises(self):
        assert passwords.verify_password("not-a-phc-string", SYNTHETIC_PASSWORD).ok is False

    def test_hashes_are_salted(self):
        assert passwords.hash_password(SYNTHETIC_PASSWORD) != passwords.hash_password(
            SYNTHETIC_PASSWORD
        )

    async def test_async_verify_matches_sync(self, phc):
        assert (await passwords.verify_password_async(phc, SYNTHETIC_PASSWORD)).ok is True
        assert (await passwords.verify_password_async(phc, SYNTHETIC_OTHER)).ok is False


class TestNeedsRehash:
    def test_stale_parameters_produce_an_upgraded_hash(self):
        # A hash produced with weaker parameters than the configured ones. Only the parameters
        # differ, so a correct password still verifies and must be silently upgraded.
        weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, hash_len=32, salt_len=16)
        stale_phc = weak.hash(SYNTHETIC_PASSWORD)

        assert passwords.needs_rehash(stale_phc) is True

        result = passwords.verify_password(stale_phc, SYNTHETIC_PASSWORD)
        assert result.ok is True
        assert result.upgraded_phc is not None
        assert result.upgraded_phc != stale_phc
        # The replacement must verify and must not itself need a rehash, or the upgrade loops.
        assert passwords.needs_rehash(result.upgraded_phc) is False
        assert passwords.verify_password(result.upgraded_phc, SYNTHETIC_PASSWORD).ok is True

    def test_wrong_password_against_a_stale_hash_produces_no_upgrade(self):
        weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, hash_len=32, salt_len=16)
        stale_phc = weak.hash(SYNTHETIC_PASSWORD)
        result = passwords.verify_password(stale_phc, SYNTHETIC_OTHER)
        assert result.ok is False
        assert result.upgraded_phc is None

    def test_malformed_hash_is_treated_as_needing_rehash(self):
        assert passwords.needs_rehash("not-a-phc-string") is True


class TestUnknownUsernameCost:
    def test_dummy_verify_fails_and_is_reusable(self):
        assert passwords.verify_dummy().ok is False
        assert passwords.verify_dummy().ok is False

    async def test_dummy_verify_async(self):
        assert (await passwords.verify_dummy_async()).ok is False


class TestLockout:
    NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

    def test_failures_below_the_threshold_do_not_lock(self):
        for attempts in range(passwords.LOCKOUT_THRESHOLD - 1):
            count, locked_until = passwords.record_failure(attempts, now=self.NOW)
            assert count == attempts + 1
            assert locked_until is None

    def test_the_tenth_failure_locks_for_fifteen_minutes(self):
        count, locked_until = passwords.record_failure(
            passwords.LOCKOUT_THRESHOLD - 1, now=self.NOW
        )
        assert count == passwords.LOCKOUT_THRESHOLD
        assert locked_until == self.NOW + timedelta(minutes=15)

    def test_success_clears_the_counter(self):
        assert passwords.record_success() == (0, None)

    def test_is_locked_at_both_edges(self):
        deadline = self.NOW + timedelta(minutes=15)
        assert passwords.is_locked(deadline, now=self.NOW) is True
        assert passwords.is_locked(deadline, now=deadline - timedelta(seconds=1)) is True
        assert passwords.is_locked(deadline, now=deadline) is False
        assert passwords.is_locked(None, now=self.NOW) is False

    def test_retry_after_is_whole_seconds_and_never_zero_while_locked(self):
        deadline = self.NOW + timedelta(seconds=0.2)
        assert passwords.lock_retry_after(deadline, now=self.NOW) == 1
        assert passwords.lock_retry_after(self.NOW + timedelta(minutes=15), now=self.NOW) == 900
        assert passwords.lock_retry_after(self.NOW, now=self.NOW) == 0
        assert passwords.lock_retry_after(None, now=self.NOW) == 0

    def test_stored_iso_string_is_accepted(self):
        deadline = self.NOW + timedelta(minutes=15)
        assert passwords.is_locked(deadline.isoformat(), now=self.NOW) is True
