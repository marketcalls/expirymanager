"""Request and response models for the local passcode routes, API.md section 1.

Two rules shape every model here.

A password field is never stripped. `RequestModel` sets `str_strip_whitespace=True`, which is
right for a username and wrong for a secret: silently trimming a password changes what the user
typed, and the only reason it does not lock anyone out today is that the same trimming happens on
the way in and on the way out. A field-level `StringConstraints(strip_whitespace=False)` overrides
the model config for exactly the fields that hold a secret.

No response model carries a password, a hash, or anything derived from one. The only values that
cross the boundary are the user id, the username and a session deadline.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from expirymanager.api.schemas.common import ApiModel, RequestModel
from expirymanager.security.passwords import MAX_PASSWORD_LENGTH

__all__ = [
    "USERNAME_MIN_LENGTH",
    "USERNAME_MAX_LENGTH",
    "Secret",
    "Username",
    "SetupRequest",
    "SetupResponse",
    "LoginRequest",
    "LoginResponse",
    "CurrentUserResponse",
    "PasswordChangeRequest",
]

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 64

# The alias every password field uses. `max_length` is a memory bound rather than a policy: the
# policy lives in `security/passwords.validate_password`, so there is one place that decides what
# an acceptable password is and one message that says so.
Secret = Annotated[
    str,
    StringConstraints(strip_whitespace=False, min_length=1, max_length=MAX_PASSWORD_LENGTH),
]

Username = Annotated[
    str,
    StringConstraints(min_length=USERNAME_MIN_LENGTH, max_length=USERNAME_MAX_LENGTH),
]


class SetupRequest(RequestModel):
    """`POST /auth/setup`. Accepted only while no user account exists."""

    username: Username
    password: Secret


class SetupResponse(ApiModel):
    """The new account. The session arrives as a Set-Cookie pair, not in the body."""

    user_id: str


class LoginRequest(RequestModel):
    """`POST /auth/login`.

    The username is not length-constrained the way `SetupRequest` constrains it. A login must
    fail with the same 401 whatever was submitted: a 422 on a two character username would tell
    an attacker which inputs are worth trying.
    """

    username: Annotated[str, StringConstraints(max_length=USERNAME_MAX_LENGTH)]
    password: Secret


class LoginResponse(ApiModel):
    user_id: str
    username: str


class CurrentUserResponse(ApiModel):
    """`GET /auth/me`. `session_expires_at` is whichever of the two windows comes first."""

    user_id: str
    username: str
    session_expires_at: str


class PasswordChangeRequest(RequestModel):
    """`POST /auth/password`. Both fields are secrets, so neither is stripped."""

    current_password: Secret
    new_password: Secret = Field(
        description="Validated by security/passwords.validate_password, not by this model."
    )
