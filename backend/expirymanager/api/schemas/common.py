"""Shared request and response models: the error envelope, cursor pagination, the base models.

Every schema module under `api/schemas/` builds on the two base classes here, so that the rules
that must hold across the whole surface are stated once:

- A request model forbids unknown fields. A typo in a JSON body is a 422 and not a silently
  ignored field, which is what turns a mistyped `include_oi` into a download that quietly omits
  open interest.
- A response model does not forbid them, because a route that returns a superset of its declared
  model during a migration should not 500 on the way out.

Cursor pagination is here rather than in each route module for the same reason. API.md documents
one opaque cursor shape (`"eyJrIjo0MjEwMX0"`, a base64url JSON object), and three route items are
about to page three different tables. One codec means the frontend can treat every cursor as an
opaque string, and a cursor minted by one route can never be silently accepted by another,
because the payload carries the key set the route expects.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ApiModel",
    "RequestModel",
    "ErrorDetail",
    "ErrorEnvelope",
    "Page",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "CursorError",
    "encode_cursor",
    "decode_cursor",
]

# API.md section 6: jobs default 25, tasks default 100, contracts default 100 with a max of 500.
# The two constants here are the shared ceiling and the shared default for a route that does not
# document one of its own.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

ItemT = TypeVar("ItemT")


class ApiModel(BaseModel):
    """Base for every response model."""

    model_config = ConfigDict(
        # Datetimes are serialised by the route from already-formatted IST strings, so no encoder
        # is configured here. Pinning one would silently rewrite a value a route formatted itself.
        populate_by_name=True,
    )


class RequestModel(BaseModel):
    """Base for every request body model."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class ErrorDetail(ApiModel):
    """The inner object of the API.md section 0 envelope."""

    code: str
    message: str
    correlation_id: str | None = None
    detail: Any | None = None


class ErrorEnvelope(ApiModel):
    """`{"error": {...}}`. Declared so it can be attached to route responses in the OpenAPI."""

    error: ErrorDetail


class Page(ApiModel, Generic[ItemT]):
    """One page of a cursor-paged collection.

    `next_cursor` is null on the last page. There is no total count on purpose: counting the
    matching rows costs a second scan on tables with hundreds of millions of rows, and no screen
    in the application renders a total.
    """

    items: list[ItemT] = Field(default_factory=list)
    next_cursor: str | None = None


class CursorError(ValueError):
    """A cursor that was not minted by `encode_cursor`, or not for this route."""


def encode_cursor(payload: dict[str, Any]) -> str:
    """Encode a keyset position as one opaque base64url token, with no padding."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, expect: tuple[str, ...] = ()) -> dict[str, Any]:
    """Decode a cursor and assert it carries exactly the keys the caller expects.

    A cursor is client-supplied input that becomes part of a WHERE clause, so it is validated
    here rather than trusted at the call site. `expect` is what stops a cursor minted by the jobs
    route from being fed to the contracts route and silently paging from the wrong key.
    """
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CursorError("the cursor is not a valid pagination token") from exc
    if not isinstance(payload, dict):
        raise CursorError("the cursor is not a valid pagination token")
    if expect and set(payload) != set(expect):
        raise CursorError("the cursor does not belong to this collection")
    return payload
