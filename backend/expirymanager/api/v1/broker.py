"""Fyers credentials and the OAuth login, API.md section 2.

Three things live here and they are all one story: storing the app registration, starting a login,
and finishing one.

**The app secret is write only.** It is encrypted with the active DEK inside the handler that
receives it, bound by AAD to its table, column and row, and no route in this application reads it
back out. `GET /broker/fyers` reports `app_secret_configured`, a boolean. There is deliberately no
mask: a mask confirms a guess character by character, and a frontend that can see one will
eventually round-trip it back on save, which is how a mask becomes the stored secret.

**The state parameter is the whole CSRF story for the callback.** `GET /fyers/callback` is a
cross-site top-level navigation from Fyers, so it can carry no header and no matching Origin, and
it is the one CSRF-exempt route in the codebase. What protects it instead is a 256 bit value that
is stored only as a sha256, is bound to the session that started the login, expires in ten
minutes, and is consumed in the same transaction that reads it. Every way that check can fail
returns the same generic reason, because a callback that says which of missing, expired, already
used or wrong-session applied is an oracle.

**Nothing here logs an auth code or a token.** The redirected URL the manual route accepts is a
credential in its own right.

`complete_oauth_login` is the single verification path. The root callback and the manual paste
fallback both call it, so the fallback cannot drift into a weaker check than the automatic route.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, status
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from expirymanager.api.deps import (
    CurrentUserDep,
    EngineDep,
    KeyManagerDep,
    RequiredSessionDep,
    SettingsDep,
    StateDep,
)
from expirymanager.api.errors import ApiError, needs_reauth
from expirymanager.api.schemas.broker import (
    BrokerConnectResponse,
    BrokerCredentialsRequest,
    BrokerManualCallbackRequest,
    BrokerStatusResponse,
    BrokerTestResponse,
)
from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers.auth import (
    AuthCodeExchangeFailed,
    AuthError,
    CallbackParams,
    DEFAULT_REDIRECT_URI,
    complete_login,
    parse_redirected_url,
    start_login,
    state_digest,
    state_matches,
)
from expirymanager.brokers.fyers.throttle import AccountBlocked, GovernorMode
from expirymanager.brokers.fyers.tokens import (
    APP_SECRET_COLUMN,
    CREDENTIAL_TABLE,
    NoCredentialsError,
    TokenRecord,
)
from expirymanager.lifespan import AppState

__all__ = [
    "router",
    "BROKER_FYERS",
    "CODE_NO_CREDENTIALS",
    "CODE_INVALID_REDIRECT_URI",
    "CODE_OAUTH_STATE_INVALID",
    "CODE_LOGIN_FAILED",
    "GENERIC_STATE_MESSAGE",
    "TEST_SYMBOL",
    "OAuthStateRejected",
    "OAuthLoginFailed",
    "complete_oauth_login",
    "read_broker_status",
    "park_jobs_for_reauth",
    "resume_jobs_after_login",
    "scheduled_logout",
]

log = logging.getLogger(__name__)

router = APIRouter()

BROKER_FYERS = "fyers"

CODE_NO_CREDENTIALS = "no_credentials"
CODE_INVALID_REDIRECT_URI = "invalid_redirect_uri"
CODE_OAUTH_STATE_INVALID = "oauth_state_invalid"
CODE_LOGIN_FAILED = "login_failed"

# One sentence for every state failure. Missing, expired, already used and bound to another
# session are indistinguishable from outside on purpose.
GENERIC_STATE_MESSAGE = (
    "That broker login could not be verified. Start the connection again from Settings."
)

# The one governed request `POST /broker/fyers/test` spends.
TEST_SYMBOL = "NSE:NIFTY50-INDEX"
TEST_WINDOW_DAYS = 7
# A ceiling on the whole test call, so a pipeline that pauses between the mode check and the
# governor gate cannot leave an HTTP request hanging on the outbound queue.
TEST_TIMEOUT_SECONDS = 30.0

_SELECT_CREDENTIAL = """
SELECT credential_id, label, app_id, redirect_uri, plan, length(app_secret_enc)
  FROM broker_credential
 WHERE broker = :broker AND is_active = 1
 ORDER BY updated_at DESC
 LIMIT 1
"""

_INSERT_CREDENTIAL = """
INSERT INTO broker_credential (credential_id, broker, label, app_id, app_secret_enc,
                               redirect_uri, plan, key_ver, is_active, created_at, updated_at)
VALUES (:credential_id, :broker, :label, :app_id, :app_secret_enc, :redirect_uri, :plan,
        :key_ver, 1, :now, :now)
"""

_UPDATE_CREDENTIAL = """
UPDATE broker_credential
   SET label = :label, app_id = :app_id, app_secret_enc = :app_secret_enc,
       redirect_uri = :redirect_uri, plan = :plan, key_ver = :key_ver, is_active = 1,
       updated_at = :now
 WHERE credential_id = :credential_id
"""

_INSERT_STATE = """
INSERT INTO oauth_state (state_hash, session_id_hash, credential_id, created_at, expires_at,
                         used_at)
VALUES (:state_hash, :session_id_hash, :credential_id, :created_at, :expires_at, NULL)
"""

_SELECT_STATE = """
SELECT state_hash, session_id_hash, credential_id, expires_at, used_at
  FROM oauth_state
 WHERE state_hash = :state_hash
"""

_CONSUME_STATE = """
UPDATE oauth_state SET used_at = :now WHERE state_hash = :state_hash AND used_at IS NULL
"""

# Consumed and expired rows are rubbish the moment they are one of those two things. Pruned on
# every connect so the table cannot grow without a scheduled job owning it.
_PRUNE_STATE = """
DELETE FROM oauth_state WHERE expires_at < :now OR used_at IS NOT NULL
"""

_RESUME_JOBS = """
UPDATE job SET status = 'queued', block_reason = NULL WHERE status = 'blocked_auth'
"""

_PARK_JOBS = """
UPDATE job SET status = 'blocked_auth', block_reason = :reason
 WHERE status IN ('queued', 'running')
"""

_SELECT_USER_IDS = "SELECT user_id FROM app_user"


class OAuthStateRejected(Exception):
    """The state did not verify. Never carries which of the four checks failed."""


class OAuthLoginFailed(Exception):
    """Fyers did not return a usable auth code, or refused to exchange the one it returned."""


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CredentialRow:
    """The non-secret half of `broker_credential`. The ciphertext is only ever measured."""

    credential_id: str
    label: str
    app_id: str
    redirect_uri: str
    plan: str
    secret_length: int

    @property
    def app_secret_configured(self) -> bool:
        # A revoked or half written row holds a zero length blob. Present but empty is not
        # configured, which is what stops the UI from offering a connect button that cannot work.
        return self.secret_length > 0


def _read_credential(engine: Engine) -> _CredentialRow | None:
    with engine.connect() as connection:
        row = connection.execute(text(_SELECT_CREDENTIAL), {"broker": BROKER_FYERS}).first()
    if row is None:
        return None
    return _CredentialRow(
        credential_id=str(row[0]),
        label=str(row[1] or "Fyers"),
        app_id=str(row[2]),
        redirect_uri=str(row[3]),
        plan=str(row[4] or "standard"),
        secret_length=int(row[5] or 0),
    )


def read_broker_status(state: AppState) -> BrokerStatusResponse:
    """The whole of `GET /broker/fyers`, synchronous. Call through `run_in_threadpool`.

    Also the body of every write that changes the connection, so the frontend has exactly one
    shape to hold for the token chip in the top bar.
    """
    engine = state.engine
    if engine is None:  # pragma: no cover - deps already rejected this with a 503
        raise RuntimeError("the operational database is not open")

    credential = _read_credential(engine)
    plan = credential.plan if credential else _plan_setting(state)
    response = BrokerStatusResponse(
        credential_id=credential.credential_id if credential else None,
        label=credential.label if credential else None,
        app_id=credential.app_id if credential else None,
        redirect_uri=credential.redirect_uri if credential else DEFAULT_REDIRECT_URI,
        plan="prime" if plan == "prime" else "standard",
        app_secret_configured=bool(credential and credential.app_secret_configured),
    )

    broker = state.token_broker
    if broker is None or credential is None:
        return response

    try:
        record: TokenRecord | None = broker.record()
    except Exception:  # noqa: BLE001 - the top bar must render even with an unreadable token
        # A token row whose ciphertext no longer decrypts, for instance after a key rotation that
        # did not rewrap it, must read as disconnected rather than take down the screen that
        # offers the reconnect button.
        log.warning("the stored broker token could not be read")
        return response.model_copy(update={"token_state": "needs_reauth"})
    if record is None:
        return response
    return response.model_copy(
        update={
            "connected": bool(broker.has_valid_token()),
            "token_state": record.state,
            "token_expires_at": record.access_expires_at,
            # Eight characters. Enough to tell two tokens apart, useless as a credential.
            "token_fingerprint": record.fingerprint[:8] or None,
            "last_error": record.last_error,
        }
    )


def _plan_setting(state: AppState) -> str:
    if state.settings is None:
        return "standard"
    return str(state.settings.get_str("plan_tier") or "standard")


# ---------------------------------------------------------------------------
# The one verification path
# ---------------------------------------------------------------------------


def _consume_state(engine: Engine, *, state_value: str, session_id_hash: bytes) -> str:
    """Verify the state and mark it used, in one transaction. Returns the credential id.

    Every rejection raises the same exception with no distinguishing detail. The UPDATE carries
    `used_at IS NULL` in its WHERE clause and its row count is checked, so two callbacks arriving
    with the same state can never both succeed: the second finds no row to update.
    """
    digest = state_digest(state_value)
    now = _now()
    with engine.begin() as connection:
        row = connection.execute(text(_SELECT_STATE), {"state_hash": digest}).first()
        if row is None:
            raise OAuthStateRejected("no such state")
        stored_hash, stored_session, credential_id, expires_at, used_at = row
        # Re-derived from the presented value rather than trusted from the lookup, and compared in
        # constant time, which is what SECURITY.md section 7 asks for by name.
        if not state_matches(bytes(stored_hash), state_value):
            raise OAuthStateRejected("state digest mismatch")
        if not hmac.compare_digest(bytes(stored_session), session_id_hash):
            raise OAuthStateRejected("state belongs to another session")
        if used_at is not None:
            raise OAuthStateRejected("state already used")
        if now >= _parse(str(expires_at)):
            raise OAuthStateRejected("state expired")
        consumed = connection.execute(
            text(_CONSUME_STATE), {"now": now.isoformat(), "state_hash": digest}
        ).rowcount
        if consumed != 1:
            raise OAuthStateRejected("state already used")
    return str(credential_id)


def resume_jobs_after_login(engine: Engine) -> int:
    """Move every `blocked_auth` job back to `queued`. Returns how many moved.

    PIPELINE.md section 7.2: a backfill parked by the 03:00 logout resumes at the exact task after
    the next login, not at the start of the job. The task rows are untouched here; they are still
    `pending` with their leases released, which is what makes the resume exact.
    """
    with engine.begin() as connection:
        moved = connection.execute(text(_RESUME_JOBS)).rowcount
    return int(moved or 0)


def park_jobs_for_reauth(engine: Engine, *, reason: str) -> int:
    """Move queued and running jobs to `blocked_auth`. Returns how many moved.

    The mirror of `resume_jobs_after_login`, and the job-table half of the scheduled logout. Jobs
    are parked, never failed: their tasks stay pending, so nothing is lost and nothing is
    re-downloaded.
    """
    with engine.begin() as connection:
        moved = connection.execute(text(_PARK_JOBS), {"reason": reason}).rowcount
    return int(moved or 0)


async def complete_oauth_login(
    state: AppState,
    *,
    params: CallbackParams,
    session_id_hash: bytes | None,
) -> TokenRecord:
    """Verify the state, exchange the auth code, store the token, unpark the pipeline.

    The root callback and the manual paste fallback both end here, which is the point: one path,
    one set of checks, no chance of the fallback being the weaker of the two.
    """
    engine = state.engine
    broker = state.token_broker
    client = state.fyers_client
    if engine is None or broker is None or client is None:  # pragma: no cover - 503 upstream
        raise RuntimeError("the broker services are not available")

    if not params.state or session_id_hash is None:
        raise OAuthStateRejected("no state, or no session to bind it to")

    credential_id = await run_in_threadpool(
        _consume_state, engine, state_value=params.state, session_id_hash=session_id_hash
    )

    try:
        credentials = await run_in_threadpool(broker.credentials)
    except NoCredentialsError as exc:
        raise OAuthStateRejected("the credential behind this state is gone") from exc
    if credentials.credential_id != credential_id:
        # The credentials were replaced while the user was away at the Fyers login page. The auth
        # code belongs to the old app id and would fail the exchange with a confusing -352.
        raise OAuthStateRejected("the credential changed while the login was in flight")

    if not params.auth_code:
        # Fyers reached the callback but refused, so there is nothing to exchange. The state is
        # already consumed, which is correct: it was used.
        raise OAuthLoginFailed("the broker returned no auth code")

    record = await complete_login(
        client, broker, auth_code=params.auth_code, credentials=credentials
    )

    await _unpark_after_login(state)
    moved = await run_in_threadpool(resume_jobs_after_login, engine)
    if moved:
        log.info("parked jobs resumed after login", extra={"jobs_resumed": moved})
    return record


async def _unpark_after_login(state: AppState) -> None:
    """Return the governor to `running` if a rejected token is what stopped it.

    Only `paused_auth` is lifted. A pipeline paused on rate, on budget or by the user was not
    stopped by the token, and a login is not consent to restart it.
    """
    governor = state.governor
    if governor is None or governor.mode is not GovernorMode.PAUSED_AUTH:
        return
    try:
        await governor.resume(by="fyers login")
    except AccountBlocked:
        # A broker block outranks a fresh token: the account is blocked until the IST date rolls,
        # and spending requests into it would earn more strikes.
        log.warning("the pipeline stayed paused after login because the account is blocked")


async def scheduled_logout(state: AppState, *, revoke_sessions: bool = True) -> None:
    """The entry point the 03:00 IST schedule calls. Decision 3, and PIPELINE.md section 7.3.

    Token loss is a planned event here, not an expiry that surprises a worker mid request. The
    order matters: park the jobs first, so no worker leases another task, then destroy the token,
    which closes the auth gate and stops the pool at the top of its loop.

    W14 owns the schedule itself. This function is what it calls, and it is safe to call twice.
    """
    engine = state.engine
    broker = state.token_broker
    if engine is None or broker is None:
        log.warning("scheduled logout skipped because the broker services are not available")
        return

    reason = "scheduled daily logout at 03:00 ist"
    parked = await run_in_threadpool(park_jobs_for_reauth, engine, reason=reason)
    await broker.scheduled_logout()

    if revoke_sessions and state.session_manager is not None:
        # The local passcode session goes too. SECURITY.md section 5 and the daily logout in
        # PIPELINE.md section 7.3 both describe one nightly reset, and an 8 hour idle window means
        # an overnight session was almost always dead by then anyway.
        revoked = await run_in_threadpool(_revoke_every_session, engine, state.session_manager)
        log.info("local sessions revoked by the scheduled logout", extra={"sessions": revoked})

    log.info("scheduled logout completed", extra={"jobs_parked": parked})


def _revoke_every_session(engine: Engine, manager) -> int:  # type: ignore[no-untyped-def]
    with engine.connect() as connection:
        user_ids = [str(row[0]) for row in connection.execute(text(_SELECT_USER_IDS))]
    return sum(manager.revoke_all(user_id) for user_id in user_ids)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/broker/fyers",
    response_model=BrokerStatusResponse,
    summary="Fyers credential and token status",
)
async def get_broker(state: StateDep, _user: CurrentUserDep) -> BrokerStatusResponse:
    """Whether a credential is configured, whether a token exists, and when it expires.

    This is what the top bar reads. No secret and no mask is in the body.
    """
    return await run_in_threadpool(read_broker_status, state)


@router.post(
    "/broker/fyers/credentials",
    response_model=BrokerStatusResponse,
    summary="Store the Fyers app registration",
)
async def save_credentials(
    body: BrokerCredentialsRequest,
    state: StateDep,
    engine: EngineDep,
    keys: KeyManagerDep,
    settings: SettingsDep,
    _user: CurrentUserDep,
) -> BrokerStatusResponse:
    """Encrypt the app secret and write the registration.

    The plaintext exists in this function and nowhere else: it is not logged, not returned, and
    not kept on any object that outlives the request. Saving credentials revokes any existing
    token, because a token issued to the previous app id cannot be used with the new one.
    """
    redirect_uri = _validated_redirect_uri(body.redirect_uri)

    existing = await run_in_threadpool(_read_credential, engine)
    credential_id = existing.credential_id if existing else str(uuid.uuid4())

    def write() -> None:
        # The row id is part of the AAD, so it is decided before the ciphertext is produced. That
        # is why this table has a TEXT primary key rather than an autoincrementing integer.
        blob = keys.encrypt_field(
            body.app_secret,
            table=CREDENTIAL_TABLE,
            column=APP_SECRET_COLUMN,
            row_id=credential_id,
        )
        params = {
            "credential_id": credential_id,
            "broker": BROKER_FYERS,
            "label": body.label,
            "app_id": body.app_id,
            "app_secret_enc": blob,
            "redirect_uri": redirect_uri,
            "plan": body.plan,
            "key_ver": int(keys.active_version),
            "now": _now().isoformat(),
        }
        with engine.begin() as connection:
            statement = _UPDATE_CREDENTIAL if existing else _INSERT_CREDENTIAL
            connection.execute(text(statement), params)

    await run_in_threadpool(write)

    # The governor reads the plan from settings, so the two would drift if only the row moved.
    settings.set("plan_tier", body.plan)

    broker = state.token_broker
    if broker is not None:
        await broker.clear(reason="the broker credentials were replaced")
        await run_in_threadpool(broker.reload)

    log.info(
        "fyers credentials saved",
        extra={"credential_id": credential_id, "plan": body.plan, "replaced": bool(existing)},
    )
    return await run_in_threadpool(read_broker_status, state)


def _validated_redirect_uri(value: str) -> str:
    """Absolute https only, and the registered value by default.

    Fyers matches the redirect URI character for character against the registration, so a typo
    here does not fail at save time, it fails much later with an error that reads like a wrong app
    id. Rejecting anything that is not an absolute https URL catches the common half of that.
    """
    candidate = value.strip()
    if not candidate.lower().startswith("https://") or len(candidate) <= len("https://"):
        raise ApiError(
            400,
            CODE_INVALID_REDIRECT_URI,
            "The redirect URL must be an absolute https URL. Use "
            f"{DEFAULT_REDIRECT_URI}, which is the value registered on the Fyers dashboard.",
        )
    return candidate


@router.post(
    "/broker/fyers/connect",
    response_model=BrokerConnectResponse,
    summary="Start a Fyers OAuth login",
)
async def connect(
    state: StateDep,
    engine: EngineDep,
    session: RequiredSessionDep,
    _user: CurrentUserDep,
) -> BrokerConnectResponse:
    """Mint a single-use state, bind it to this session, and return the authorize URL.

    Only the sha256 of the state is stored. The raw value exists in the URL the browser opens and
    in the redirect Fyers sends back, and in neither case does this application need to keep it.
    """
    broker = state.token_broker
    if broker is None:  # pragma: no cover - 503 upstream
        raise ApiError(503, "service_unavailable", "The broker services are not available.")

    try:
        credentials = await run_in_threadpool(broker.credentials)
    except NoCredentialsError as exc:
        raise ApiError(
            400,
            CODE_NO_CREDENTIALS,
            "Save the Fyers app id and app secret before connecting.",
        ) from exc

    request = start_login(credentials)

    def store() -> None:
        with engine.begin() as connection:
            connection.execute(text(_PRUNE_STATE), {"now": _now().isoformat()})
            connection.execute(
                text(_INSERT_STATE),
                {
                    "state_hash": request.state_hash,
                    "session_id_hash": session.id_hash,
                    "credential_id": request.credential_id,
                    "created_at": _now().isoformat(),
                    "expires_at": request.expires_at.isoformat(),
                },
            )

    await run_in_threadpool(store)
    log.info(
        "fyers login started",
        extra={
            "credential_id": request.credential_id,
            "session_prefix": session.id_prefix,
            "state_expires_at": request.expires_at.isoformat(),
        },
    )
    # The authorize URL embeds the state, so it goes to the browser and never to the log.
    return BrokerConnectResponse(
        authorize_url=request.authorize_url,
        state_expires_at=request.expires_at.isoformat(),
    )


@router.post(
    "/broker/fyers/callback/manual",
    response_model=BrokerStatusResponse,
    summary="Finish a login by pasting the redirected URL",
)
async def manual_callback(
    body: BrokerManualCallbackRequest,
    state: StateDep,
    session: RequiredSessionDep,
    _user: CurrentUserDep,
) -> BrokerStatusResponse:
    """The fallback for a redirect that never landed.

    The first visit to the callback shows a certificate warning, and a user who declines it in
    that tab never completes the automatic return. They paste the whole URL here instead, and it
    runs the identical verification: same state row, same single use, same session binding.
    """
    try:
        params = parse_redirected_url(body.redirected_url)
    except AuthError as exc:
        raise ApiError(400, CODE_OAUTH_STATE_INVALID, GENERIC_STATE_MESSAGE) from exc

    try:
        await complete_oauth_login(state, params=params, session_id_hash=session.id_hash)
    except OAuthStateRejected as exc:
        raise ApiError(400, CODE_OAUTH_STATE_INVALID, GENERIC_STATE_MESSAGE) from exc
    except OAuthLoginFailed as exc:
        raise ApiError(400, CODE_LOGIN_FAILED, _login_failed_message(params)) from exc
    except AuthCodeExchangeFailed as exc:
        # The broker's own message is not forwarded: an upstream auth error body is exactly where
        # a token or an app secret can appear.
        raise ApiError(
            400,
            CODE_LOGIN_FAILED,
            "The broker did not accept that login. Start the connection again from Settings.",
        ) from exc

    return await run_in_threadpool(read_broker_status, state)


def _login_failed_message(params: CallbackParams) -> str:
    if params.status and params.status != "ok":
        return "Fyers did not complete the login. Start the connection again from Settings."
    return "That login carried no auth code. Start the connection again from Settings."


@router.post(
    "/broker/fyers/test",
    response_model=BrokerTestResponse,
    summary="Spend one governed request against Fyers",
)
async def test_connection(state: StateDep, _user: CurrentUserDep) -> BrokerTestResponse:
    """One real request, so the answer means something.

    Exactly one governed call against expiry-dates over a one week window. It counts against the
    daily budget like any other request, which is why the response reports the budget back.
    """
    client = state.fyers_client
    governor = state.governor
    broker = state.token_broker
    if client is None or governor is None or broker is None:  # pragma: no cover - 503 upstream
        raise ApiError(503, "service_unavailable", "The broker services are not available.")

    if not broker.has_valid_token():
        raise needs_reauth()
    if governor.mode is not GovernorMode.RUNNING:
        # The governor gate blocks until the pipeline resumes, which for an HTTP request means
        # hanging until the client gives up. Refusing immediately is the honest answer.
        raise _pipeline_not_running(governor)

    today = datetime.now(UTC).date()
    try:
        response = await asyncio.wait_for(
            ep.expiry_dates(
                client,
                symbol=TEST_SYMBOL,
                range_from=today,
                range_to=today + timedelta(days=TEST_WINDOW_DAYS),
            ),
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("the fyers connection test timed out")
        return BrokerTestResponse(
            ok=False,
            endpoint=ep.EXPIRY_DATES.name,
            latency_ms=int(TEST_TIMEOUT_SECONDS * 1000),
            requests_used_today=governor.snapshot().requests_used,
        )

    classification = response.classification()
    if classification is not None and classification.is_auth_failure:
        # One rejection is enough to park: the token is gone whether a worker or this route found
        # out. The broker collapses duplicates by generation.
        await broker.on_auth_error(broker.generation, reason="the connection test was rejected")
        raise needs_reauth()

    return BrokerTestResponse(
        ok=response.ok,
        endpoint=ep.EXPIRY_DATES.name,
        latency_ms=response.latency_ms,
        requests_used_today=governor.snapshot().requests_used,
    )


def _pipeline_not_running(governor) -> ApiError:  # type: ignore[no-untyped-def]
    if governor.mode is GovernorMode.PAUSED_AUTH:
        return needs_reauth()
    return ApiError(
        503,
        "pipeline_stopped",
        "The outbound request governor is not running, so no request can be sent.",
        detail={"mode": str(governor.mode), "reason": governor.reason},
    )


@router.post(
    "/broker/fyers/disconnect",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the stored Fyers token",
)
async def disconnect(state: StateDep, _user: CurrentUserDep):  # type: ignore[no-untyped-def]
    """Destroy the token and leave the credentials in place.

    The ciphertext is overwritten rather than the row merely marked revoked: a revoked row holding
    a usable bearer credential is a stored secret with nobody responsible for it.
    """
    broker = state.token_broker
    if broker is None:  # pragma: no cover - 503 upstream
        raise ApiError(503, "service_unavailable", "The broker services are not available.")
    await broker.clear(reason="disconnected by the user")
    log.info("fyers token revoked by the user")
    return None
