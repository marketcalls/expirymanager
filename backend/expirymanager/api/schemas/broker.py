"""Request and response models for the broker credential and OAuth routes, API.md section 2.

The one rule that governs this file: **no secret and no mask ever leaves it.** `BrokerStatus`
carries `app_secret_configured`, a boolean, and nothing else about the secret. A mask such as
`abc...xyz` is an oracle, it confirms a guess character by character, and it tempts the frontend
into round-tripping the mask back on save, at which point the mask becomes the stored secret.

There is no PIN field, in the request or in the response. SEBI discontinued the refresh token flow
from 1 April 2026, so the flow a PIN unlocked no longer exists and storing one would be a fourth
secret to protect for nothing. API.md's sample body still shows `pin_configured: false`; the
frontend's own `BrokerStatus` type already omits it, and so does this.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from expirymanager.api.schemas.common import ApiModel, RequestModel
from expirymanager.brokers.fyers.auth import DEFAULT_REDIRECT_URI

__all__ = [
    "BrokerPlan",
    "BrokerStatusResponse",
    "BrokerCredentialsRequest",
    "BrokerConnectResponse",
    "BrokerManualCallbackRequest",
    "BrokerTestResponse",
]

BrokerPlan = Literal["standard", "prime"]


class BrokerStatusResponse(ApiModel):
    """`GET /api/v1/broker/fyers`, and the body of every write that changes it.

    Every field is null-safe on purpose: this route answers before any credential exists, which is
    the state the setup wizard reads to decide what to render.
    """

    credential_id: str | None = None
    label: str | None = None
    app_id: str | None = None
    redirect_uri: str = DEFAULT_REDIRECT_URI
    plan: BrokerPlan = "standard"
    app_secret_configured: bool = False
    connected: bool = False
    token_state: str = "none"
    token_expires_at: str | None = None
    # The first 8 characters of the sha256 of the access token. Enough to tell two tokens apart in
    # the UI and in a log line, and useless as a credential.
    token_fingerprint: str | None = None
    last_error: str | None = None


class BrokerCredentialsRequest(RequestModel):
    """`POST /api/v1/broker/fyers/credentials`.

    `app_secret` is write only. It is encrypted with the active DEK inside the route and is never
    read back out by any endpoint.
    """

    label: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "Fyers"
    app_id: Annotated[str, StringConstraints(min_length=3, max_length=128)]
    # Not stripped: whitespace inside a secret is part of the secret. The dashboard never issues
    # one with a leading space, but silently editing a credential is not this layer's decision.
    app_secret: Annotated[
        str, StringConstraints(strip_whitespace=False, min_length=1, max_length=512)
    ]
    redirect_uri: Annotated[str, StringConstraints(min_length=1, max_length=512)] = (
        DEFAULT_REDIRECT_URI
    )
    plan: BrokerPlan = "standard"

    @field_validator("app_id")
    @classmethod
    def _reject_pasted_pair(cls, value: str) -> str:
        """A Fyers app id never contains a colon.

        The dashboard shows the app id and the secret next to each other, and the appIdHash is
        documented as the sha256 of `app_id:app_secret`, so pasting the joined pair into this
        field is a mistake a careful person makes. Caught here, it is a 422 on a field name. Let
        through, it stores the secret in a column that is not encrypted and puts it in the
        authorize URL.
        """
        if ":" in value:
            raise ValueError("the app id must not contain a colon or the app secret")
        return value


class BrokerConnectResponse(ApiModel):
    """`POST /api/v1/broker/fyers/connect`. The SPA opens `authorize_url` in a new tab."""

    authorize_url: str
    state_expires_at: str


class BrokerManualCallbackRequest(RequestModel):
    """`POST /api/v1/broker/fyers/callback/manual`.

    The whole URL the browser landed on, pasted by the user. It carries an auth code, so it is
    treated as a credential: never logged, never echoed back, and not stripped of whitespace by
    the model because the parser handles that itself.
    """

    redirected_url: Annotated[
        str, StringConstraints(strip_whitespace=False, min_length=1, max_length=4096)
    ] = Field(description="The full https://127.0.0.1:8000/fyers/callback?... URL.")


class BrokerTestResponse(ApiModel):
    """`POST /api/v1/broker/fyers/test`. Exactly one governed request was spent to produce this."""

    ok: bool
    endpoint: str
    latency_ms: int
    requests_used_today: int
