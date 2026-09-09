"""The token broker: the one place that holds a decrypted Fyers access token.

There is no refresh path, and its absence is a design decision rather than a gap. SEBI
discontinued the refresh token flow from 1 April 2026, so unattended refresh is not possible at
all. A missing or rejected token therefore means exactly one thing: the user must log in again.
There is no PIN anywhere in this product, because a PIN only ever unlocked the flow that is gone
and would be a fourth secret to protect for nothing.

Token loss is scheduled, not merely detected. `scheduled_logout` is the entry point the 03:00 IST
schedule calls. Running jobs are not failed by it: they checkpoint, park in the awaiting
authentication state, and resume at the exact task after the next successful login. Treating the
daily logout as a planned event rather than as a surprise expiry is what makes the resume path
ordinary and testable instead of exceptional.

The generation counter is what collapses N concurrent auth errors into one reaction. Eight workers
that all see -8 within the same millisecond call `on_auth_error` with the same now stale
generation; the first one parks the pipeline and the other seven return immediately.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from expirymanager.brokers.fyers.client import AuthContext

__all__ = [
    "TOKEN_TABLE",
    "ACCESS_TOKEN_COLUMN",
    "REFRESH_TOKEN_COLUMN",
    "CREDENTIAL_TABLE",
    "APP_SECRET_COLUMN",
    "TokenState",
    "FyersCredentials",
    "TokenRecord",
    "CredentialStore",
    "TokenStore",
    "InMemoryTokenStore",
    "SqliteCredentialStore",
    "SqliteTokenStore",
    "TokenBroker",
    "NoCredentialsError",
    "decode_jwt_expiry",
    "token_fingerprint",
]

log = logging.getLogger(__name__)

# The AAD that binds each ciphertext to its exact location. Written down once so a value can never
# be decrypted after being moved to another column or row.
TOKEN_TABLE = "broker_token"
ACCESS_TOKEN_COLUMN = "access_token_enc"
REFRESH_TOKEN_COLUMN = "refresh_token_enc"
CREDENTIAL_TABLE = "broker_credential"
APP_SECRET_COLUMN = "app_secret_enc"

# How close to the JWT exp claim counts as expiring. The token health schedule parks the pipeline
# at this margin so no in flight request is wasted on a token that dies mid call.
EXPIRY_MARGIN_SECONDS = 120


class TokenState:
    """The values `broker_token.state` may take. Mirrors the CHECK constraint."""

    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"


class NoCredentialsError(Exception):
    """No active Fyers app registration. The setup wizard has not been completed."""


@dataclass(frozen=True, slots=True)
class FyersCredentials:
    """One Fyers app registration. There is no PIN field."""

    credential_id: str
    app_id: str
    app_secret: str = field(repr=False)
    redirect_uri: str
    plan: str = "standard"
    label: str = "Fyers"


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """One stored token, minus the token.

    The access token itself is deliberately not on this record: everything the UI, the logs and
    the coverage rows need is the fingerprint, and a value that is never carried cannot leak.
    """

    token_id: str
    credential_id: str
    generation: int
    fingerprint: str
    issued_at: str
    access_expires_at: str | None
    state: str
    last_error: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.state in (TokenState.ACTIVE, TokenState.EXPIRING)


class CredentialStore(Protocol):
    def load_active(self) -> FyersCredentials | None: ...


class TokenStore(Protocol):
    def load_active(self, credential_id: str) -> tuple[TokenRecord, str] | None: ...

    def save(
        self,
        *,
        credential_id: str,
        access_token: str,
        refresh_token: str | None,
        generation: int,
        access_expires_at: str | None,
        refresh_expires_at: str | None,
    ) -> TokenRecord: ...

    def set_state(self, token_id: str, state: str, *, last_error: str | None = None) -> None: ...

    def clear(self, credential_id: str, *, reason: str) -> None: ...

    def max_generation(self, credential_id: str) -> int: ...


def token_fingerprint(access_token: str) -> str:
    """Lowercase hex sha256 of the access token.

    Safe to log and safe to store on coverage rows: it identifies which token fetched a chunk
    without being usable as one. The UI shows only its first eight characters.
    """
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


def decode_jwt_expiry(access_token: str) -> datetime | None:
    """Read the `exp` claim out of the access token without verifying it.

    The token is a JWT issued by Fyers and we are not its audience, so there is nothing to verify
    against and nothing that needs verifying: this value only decides when to park proactively.
    Reading it locally beats assuming an undocumented lifetime. Any malformed input answers None,
    which the caller treats as an unknown expiry rather than as an error.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded)
    except (ValueError, binascii.Error):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class InMemoryTokenStore:
    """A store that forgets on restart. Tests only, never wired into the app."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[TokenRecord, str]] = {}
        self._generations: dict[str, int] = {}

    def load_active(self, credential_id: str) -> tuple[TokenRecord, str] | None:
        entry = self._tokens.get(credential_id)
        if entry is None or not entry[0].is_usable:
            return None
        return entry

    def save(
        self,
        *,
        credential_id: str,
        access_token: str,
        refresh_token: str | None,
        generation: int,
        access_expires_at: str | None,
        refresh_expires_at: str | None,
    ) -> TokenRecord:
        record = TokenRecord(
            token_id=str(uuid.uuid4()),
            credential_id=credential_id,
            generation=generation,
            fingerprint=token_fingerprint(access_token),
            issued_at=datetime.now(UTC).isoformat(),
            access_expires_at=access_expires_at,
            state=TokenState.ACTIVE,
        )
        self._tokens[credential_id] = (record, access_token)
        self._generations[credential_id] = max(
            generation, self._generations.get(credential_id, 0)
        )
        return record

    def set_state(self, token_id: str, state: str, *, last_error: str | None = None) -> None:
        for credential_id, (record, secret) in list(self._tokens.items()):
            if record.token_id == token_id:
                updated = TokenRecord(
                    token_id=record.token_id,
                    credential_id=record.credential_id,
                    generation=record.generation,
                    fingerprint=record.fingerprint,
                    issued_at=record.issued_at,
                    access_expires_at=record.access_expires_at,
                    state=state,
                    last_error=last_error,
                )
                self._tokens[credential_id] = (updated, secret)

    def clear(self, credential_id: str, *, reason: str) -> None:
        entry = self._tokens.pop(credential_id, None)
        if entry is not None:
            self._generations[credential_id] = max(
                entry[0].generation, self._generations.get(credential_id, 0)
            )

    def max_generation(self, credential_id: str) -> int:
        entry = self._tokens.get(credential_id)
        stored = self._generations.get(credential_id, 0)
        return max(stored, entry[0].generation if entry else 0)


_SELECT_CREDENTIAL = """
SELECT credential_id, app_id, app_secret_enc, redirect_uri, plan, label
  FROM broker_credential
 WHERE broker = 'fyers' AND is_active = 1
 ORDER BY updated_at DESC
 LIMIT 1
"""


class SqliteCredentialStore:
    """Reads `broker_credential` and decrypts the app secret through the key manager."""

    def __init__(self, engine: Any, key_manager: Any) -> None:
        self._engine = engine
        self._keys = key_manager

    def load_active(self) -> FyersCredentials | None:
        from sqlalchemy import text as sa_text

        with self._engine.connect() as connection:
            row = connection.execute(sa_text(_SELECT_CREDENTIAL)).fetchone()
        if row is None:
            return None
        credential_id = row[0]
        app_secret = self._keys.decrypt_text(
            row[2],
            table=CREDENTIAL_TABLE,
            column=APP_SECRET_COLUMN,
            row_id=credential_id,
        )
        return FyersCredentials(
            credential_id=credential_id,
            app_id=row[1],
            app_secret=app_secret,
            redirect_uri=row[3],
            plan=row[4] or "standard",
            label=row[5] or "Fyers",
        )


_SELECT_TOKEN = """
SELECT token_id, credential_id, access_token_enc, generation, token_fingerprint, issued_at,
       access_expires_at, state, last_error
  FROM broker_token
 WHERE credential_id = :credential_id AND state IN ('active','expiring')
 ORDER BY generation DESC
 LIMIT 1
"""

_MAX_GENERATION = """
SELECT COALESCE(MAX(generation), 0) FROM broker_token WHERE credential_id = :credential_id
"""

_INSERT_TOKEN = """
INSERT INTO broker_token (token_id, credential_id, access_token_enc, refresh_token_enc, key_ver,
                          generation, token_fingerprint, issued_at, access_expires_at,
                          refresh_expires_at, state, last_error, revoked_at)
VALUES (:token_id, :credential_id, :access_token_enc, :refresh_token_enc, :key_ver,
        :generation, :token_fingerprint, :issued_at, :access_expires_at,
        :refresh_expires_at, :state, NULL, NULL)
"""

_SET_STATE = """
UPDATE broker_token SET state = :state, last_error = :last_error WHERE token_id = :token_id
"""

# The ciphertext is destroyed rather than merely marked revoked. A revoked row that still holds a
# usable bearer credential is a stored secret with no owner.
_CLEAR_TOKENS = """
UPDATE broker_token
   SET state = 'revoked',
       access_token_enc = X'',
       refresh_token_enc = NULL,
       last_error = :reason,
       revoked_at = :revoked_at
 WHERE credential_id = :credential_id AND state IN ('active','expiring','expired','needs_reauth')
"""


class SqliteTokenStore:
    """The real token store. Ciphertext in, ciphertext out, decrypted only on load."""

    def __init__(self, engine: Any, key_manager: Any) -> None:
        self._engine = engine
        self._keys = key_manager

    def load_active(self, credential_id: str) -> tuple[TokenRecord, str] | None:
        from sqlalchemy import text as sa_text

        with self._engine.connect() as connection:
            row = connection.execute(
                sa_text(_SELECT_TOKEN), {"credential_id": credential_id}
            ).fetchone()
        if row is None:
            return None
        blob = row[2]
        if not blob:
            return None
        access_token = self._keys.decrypt_text(
            blob, table=TOKEN_TABLE, column=ACCESS_TOKEN_COLUMN, row_id=row[0]
        )
        record = TokenRecord(
            token_id=row[0],
            credential_id=row[1],
            generation=int(row[3]),
            fingerprint=row[4],
            issued_at=row[5],
            access_expires_at=row[6],
            state=row[7],
            last_error=row[8],
        )
        return record, access_token

    def max_generation(self, credential_id: str) -> int:
        from sqlalchemy import text as sa_text

        with self._engine.connect() as connection:
            row = connection.execute(
                sa_text(_MAX_GENERATION), {"credential_id": credential_id}
            ).fetchone()
        return int(row[0]) if row else 0

    def save(
        self,
        *,
        credential_id: str,
        access_token: str,
        refresh_token: str | None,
        generation: int,
        access_expires_at: str | None,
        refresh_expires_at: str | None,
    ) -> TokenRecord:
        from sqlalchemy import text as sa_text

        # The uuid exists before the INSERT because it is part of the AAD. That is why this table
        # uses a TEXT primary key rather than an autoincrementing integer.
        token_id = str(uuid.uuid4())
        access_enc = self._keys.encrypt_field(
            access_token, table=TOKEN_TABLE, column=ACCESS_TOKEN_COLUMN, row_id=token_id
        )
        refresh_enc = None
        if refresh_token:
            refresh_enc = self._keys.encrypt_field(
                refresh_token, table=TOKEN_TABLE, column=REFRESH_TOKEN_COLUMN, row_id=token_id
            )
        params = {
            "token_id": token_id,
            "credential_id": credential_id,
            "access_token_enc": access_enc,
            "refresh_token_enc": refresh_enc,
            "key_ver": int(self._keys.active_version),
            "generation": generation,
            "token_fingerprint": token_fingerprint(access_token),
            "issued_at": datetime.now(UTC).isoformat(),
            "access_expires_at": access_expires_at,
            "refresh_expires_at": refresh_expires_at,
            "state": TokenState.ACTIVE,
        }
        with self._engine.begin() as connection:
            # A login supersedes whatever came before it, in the same transaction that writes the
            # new row, so there is never a moment with two usable tokens.
            connection.execute(
                sa_text(_CLEAR_TOKENS),
                {
                    "credential_id": credential_id,
                    "reason": "superseded by a newer login",
                    "revoked_at": datetime.now(UTC).isoformat(),
                },
            )
            connection.execute(sa_text(_INSERT_TOKEN), params)
        return TokenRecord(
            token_id=token_id,
            credential_id=credential_id,
            generation=generation,
            fingerprint=params["token_fingerprint"],
            issued_at=params["issued_at"],
            access_expires_at=access_expires_at,
            state=TokenState.ACTIVE,
        )

    def set_state(self, token_id: str, state: str, *, last_error: str | None = None) -> None:
        from sqlalchemy import text as sa_text

        with self._engine.begin() as connection:
            connection.execute(
                sa_text(_SET_STATE),
                {"token_id": token_id, "state": state, "last_error": last_error},
            )

    def clear(self, credential_id: str, *, reason: str) -> None:
        from sqlalchemy import text as sa_text

        with self._engine.begin() as connection:
            connection.execute(
                sa_text(_CLEAR_TOKENS),
                {
                    "credential_id": credential_id,
                    "reason": reason,
                    "revoked_at": datetime.now(UTC).isoformat(),
                },
            )


class TokenBroker:
    """Holds the current token, the generation counter and the auth gate.

    One instance per process, constructed in the lifespan and injected. The client asks it for an
    `AuthContext` per request, so a login that lands mid job is picked up by the very next request
    with nothing to rebuild.
    """

    def __init__(
        self,
        *,
        credentials: CredentialStore,
        tokens: TokenStore,
        governor: Any = None,
        now: Any = None,
    ) -> None:
        self._credentials = credentials
        self._tokens = tokens
        self._governor = governor
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._record: TokenRecord | None = None
        self._access_token: str | None = None
        self._generation = 0
        # The generation this broker has already parked on. Without it, eight workers reporting
        # the same rejection would each pass the generation guard, because nothing bumps the
        # generation until the user logs in again.
        self._parked_generation: int | None = None
        self._loaded = False
        # Set while a token is usable. Every worker waits on this at the top of its loop, so
        # clearing it stops the whole pool before it can spend another request.
        self.auth_gate = asyncio.Event()

    # Credentials

    def credentials(self) -> FyersCredentials:
        found = self._credentials.load_active()
        if found is None:
            raise NoCredentialsError("no active fyers app registration")
        return found

    def has_credentials(self) -> bool:
        return self._credentials.load_active() is not None

    # Token access

    @property
    def generation(self) -> int:
        """Bumped on every successful login. The guard against N concurrent refreshes."""
        return self._generation

    def _load(self) -> None:
        if self._loaded:
            return
        credentials = self._credentials.load_active()
        if credentials is None:
            self._loaded = True
            return
        entry = self._tokens.load_active(credentials.credential_id)
        self._generation = max(
            self._generation, self._tokens.max_generation(credentials.credential_id)
        )
        if entry is None:
            self._record = None
            self._access_token = None
            self.auth_gate.clear()
        else:
            self._record, self._access_token = entry
            self._generation = max(self._generation, self._record.generation)
            if self._parked_generation is None or self._parked_generation < self._generation:
                self.auth_gate.set()
        self._loaded = True

    def reload(self) -> None:
        """Drop the cache so the next read comes from SQLite. Called after an external write."""
        self._loaded = False
        self._record = None
        self._access_token = None
        self._load()

    def record(self) -> TokenRecord | None:
        self._load()
        return self._record

    def has_valid_token(self) -> bool:
        """Whether a usable, unexpired token exists right now.

        The expiry check is local, against the JWT `exp` claim decoded at store time. Asking the
        broker instead would spend a request to learn something the token already says.
        """
        self._load()
        if self._record is None or self._access_token is None:
            return False
        if not self._record.is_usable:
            return False
        expires_at = self._record.access_expires_at
        if expires_at is None:
            # An unknown expiry is not an expired one. The token is used until the broker rejects
            # it, which is exactly the reactive path this module already handles.
            return True
        return self._now() < datetime.fromisoformat(expires_at)

    def seconds_to_expiry(self) -> float | None:
        """Seconds until the JWT exp claim, or None when the token does not carry one."""
        self._load()
        if self._record is None or self._record.access_expires_at is None:
            return None
        delta = datetime.fromisoformat(self._record.access_expires_at) - self._now()
        return delta.total_seconds()

    def is_expiring(self, margin_seconds: int = EXPIRY_MARGIN_SECONDS) -> bool:
        """True inside the proactive parking margin, so no in flight request is wasted."""
        remaining = self.seconds_to_expiry()
        return remaining is not None and remaining <= margin_seconds

    async def auth_context(self) -> AuthContext | None:
        """What `FyersClient` asks for before every authenticated request."""
        async with self._lock:
            self._load()
            if not self.has_valid_token():
                return None
            credentials = self._credentials.load_active()
            if credentials is None or self._access_token is None:
                return None
            return AuthContext(
                app_id=credentials.app_id,
                access_token=self._access_token,
                generation=self._generation,
            )

    # Login

    async def store_login(
        self,
        *,
        access_token: str,
        refresh_token: str | None = None,
        credential_id: str | None = None,
    ) -> TokenRecord:
        """Persist a freshly issued token, bump the generation and open the auth gate.

        Called by the OAuth callback. The token is encrypted before it is written and is never
        returned to the caller, logged, or put on a response model.
        """
        async with self._lock:
            credentials = self._credentials.load_active()
            if credentials is None:
                raise NoCredentialsError("no active fyers app registration")
            target = credential_id or credentials.credential_id
            expires_at = decode_jwt_expiry(access_token)
            refresh_expires_at = None
            if refresh_token:
                # The documented refresh token life, recorded for provenance only. There is no
                # code path that spends it: SEBI discontinued that flow from 1 April 2026.
                refresh_expires_at = (self._now() + timedelta(days=15)).isoformat()
            generation = max(self._generation, self._tokens.max_generation(target)) + 1
            record = self._tokens.save(
                credential_id=target,
                access_token=access_token,
                refresh_token=refresh_token,
                generation=generation,
                access_expires_at=expires_at.isoformat() if expires_at else None,
                refresh_expires_at=refresh_expires_at,
            )
            self._record = record
            self._access_token = access_token
            self._generation = record.generation
            self._parked_generation = None
            self._loaded = True
            self.auth_gate.set()
        log.info(
            "fyers token stored",
            extra={
                "generation": record.generation,
                "fingerprint": record.fingerprint[:8],
                "expires_at": record.access_expires_at or "unknown",
            },
        )
        return record

    # Failure handling

    async def on_auth_error(self, generation: int, *, reason: str = "") -> bool:
        """React to one auth failure. Returns True if this caller was the one that parked.

        The generation guard collapses N concurrent auth errors into exactly one reaction: every
        worker that saw the failure passes the generation it used, and only the caller whose
        generation still matches the current one does any work. Since refresh is discontinued
        there is nothing to retry here. Parking and telling the user is the whole recovery path.
        """
        async with self._lock:
            self._load()
            if generation < self._generation:
                # A newer login has already landed. Parking now would undo a good token.
                return False
            if self._parked_generation is not None and generation <= self._parked_generation:
                # Another worker already parked on this same generation. This is the case that
                # collapses eight simultaneous rejections into one reaction.
                return False
            self._parked_generation = generation
            self.auth_gate.clear()
            if self._record is not None:
                self._tokens.set_state(
                    self._record.token_id,
                    TokenState.NEEDS_REAUTH,
                    last_error=reason or "the broker rejected the access token",
                )
            self._access_token = None
            self._loaded = False
            self._record = None
        if self._governor is not None:
            await self._governor.pause_auth(
                reason=reason or "the broker rejected the access token"
            )
        log.warning(
            "fyers authentication failed, the pipeline is parked",
            extra={"generation": generation, "reason": reason or "token rejected"},
        )
        return True

    async def clear(self, *, reason: str = "logout") -> None:
        """Destroy the stored token and park the pipeline.

        The entry point for the daily 03:00 IST scheduled logout, for the disconnect button and
        for a credential change. Jobs are not failed by it: the gate closes, workers stop at the
        top of their loop, and everything resumes at the exact task after the next login.
        """
        async with self._lock:
            credentials = self._credentials.load_active()
            if credentials is not None:
                self._tokens.clear(credentials.credential_id, reason=reason)
                self._generation = max(
                    self._generation, self._tokens.max_generation(credentials.credential_id)
                )
            self._record = None
            self._access_token = None
            self._loaded = False
            self._parked_generation = self._generation
            self.auth_gate.clear()
        if self._governor is not None:
            await self._governor.pause_auth(reason=reason)
        log.info("fyers token cleared", extra={"reason": reason})

    async def scheduled_logout(self) -> None:
        """What the 03:00 IST schedule calls.

        Named separately from `clear` so the schedule reads as the planned event it is, and so the
        reason string that reaches the UI banner says why the user is being asked to log in again.
        """
        await self.clear(reason="scheduled daily logout at 03:00 ist")
