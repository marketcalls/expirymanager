"""Classify one Fyers outcome into exactly one retry class.

Classification happens here and nowhere else. Every other module consumes the class, never the raw
code, because the same numeric code means different things to the queue depending on whether it is
the task's fault, our token's fault or the broker's rate limiter. Spreading that judgement across
handlers is how a token expiry at 03:00 quietly burns four contracts' worth of retry budget.

Two rules from PIPELINE.md section 4 that are easy to get wrong and are therefore encoded as
explicit fields rather than left to the caller:

- An auth error and a rate limit error never consume a retry attempt. Neither is the task's fault.
- The first rate limit response stops the pipeline. It does not back off and retry. The account is
  blocked for the rest of the day after the per minute limit is exceeded more than three times, so
  the cheapest correct reaction to strike one is to stop and let a human look.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "RetryClass",
    "Classification",
    "PipelineMode",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_SECONDS",
    "DOCUMENTED_CODES",
    "CODE_CLASSES",
    "HTTP_CLASSES",
    "STATUS_OK",
    "STATUS_ERROR",
    "STATUS_NO_DATA",
    "classify",
    "classify_exception",
    "backoff_seconds",
]


# The three values the `s` field can carry. `no_data` appears only on the historical data
# endpoints and is a success, not a failure.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_NO_DATA = "no_data"

# PIPELINE.md section 4. Only TRANSIENT consumes attempts, so this bounds nothing else.
MAX_ATTEMPTS = 4
MAX_BACKOFF_SECONDS = 60


class RetryClass(StrEnum):
    """The six outcomes the queue knows how to act on."""

    EMPTY = "empty"
    FATAL = "fatal"
    TRANSIENT = "transient"
    AUTH_RECOVERABLE = "auth_recoverable"
    AUTH_FATAL = "auth_fatal"
    RATE_LIMITED = "rate_limited"


class PipelineMode(StrEnum):
    """The subset of `pipeline_state.mode` a classification can demand."""

    PAUSED_AUTH = "paused_auth"
    PAUSED_RATE = "paused_rate"


# Every code documented in section 4 of the Fyers reference, plus the two that only ever arrive on
# the auth endpoints. A code absent from this map is not undefined behaviour: see `classify`.
DOCUMENTED_CODES: dict[int, str] = {
    -8: "token expired",
    -15: "invalid token",
    -16: "server unable to authenticate the user token",
    -17: "token invalid or expired",
    -50: "one or more invalid parameters",
    -51: "invalid order id",
    -53: "invalid position id",
    -99: "order placement rejected",
    -300: "invalid symbol",
    -352: "invalid app id",
    -429: "api rate limit exceeded",
    400: "invalid input",
}

CODE_CLASSES: dict[int, RetryClass] = {
    # Auth. -15 is separated from the other three deliberately: the docs describe it as an invalid
    # token rather than an expired one, which means re-login and not merely a token refresh, and
    # the supervisor needs that distinction to decide whether to prompt the user.
    -8: RetryClass.AUTH_RECOVERABLE,
    -16: RetryClass.AUTH_RECOVERABLE,
    -17: RetryClass.AUTH_RECOVERABLE,
    -15: RetryClass.AUTH_FATAL,
    # Bad request shapes. Retrying an invalid parameter set spends budget to receive the same
    # answer, so none of these are retryable.
    -50: RetryClass.FATAL,
    -51: RetryClass.FATAL,
    -53: RetryClass.FATAL,
    -99: RetryClass.FATAL,
    -300: RetryClass.FATAL,
    # -352 is overloaded in the docs: invalid app id, and separately no position available to
    # exit. This product never exits positions, so only the invalid app id meaning applies.
    -352: RetryClass.FATAL,
    400: RetryClass.FATAL,
    # Rate limiting.
    -429: RetryClass.RATE_LIMITED,
}

HTTP_CLASSES: dict[int, RetryClass] = {
    400: RetryClass.FATAL,
    401: RetryClass.AUTH_FATAL,
    403: RetryClass.FATAL,
    429: RetryClass.RATE_LIMITED,
    500: RetryClass.TRANSIENT,
    502: RetryClass.TRANSIENT,
    503: RetryClass.TRANSIENT,
    504: RetryClass.TRANSIENT,
}

_AUTH_CLASSES = frozenset({RetryClass.AUTH_RECOVERABLE, RetryClass.AUTH_FATAL})


@dataclass(frozen=True, slots=True)
class Classification:
    """One outcome, decided once."""

    retry_class: RetryClass
    reason: str
    code: int | None = None
    http_status: int | None = None
    message: str = ""
    # True only for the invalid symbol code, whose documented cause is a special character that
    # was not percent encoded. The handler asserts its own encoding before it blames the symbol.
    check_symbol_encoding: bool = False

    @property
    def is_success(self) -> bool:
        """EMPTY is a success. A contract that never traded is data, not a failure."""
        return self.retry_class is RetryClass.EMPTY

    @property
    def is_auth_failure(self) -> bool:
        """The supervisor parks on this rather than spending retries."""
        return self.retry_class in _AUTH_CLASSES

    @property
    def is_retryable(self) -> bool:
        """Only a transient fault is worth sending again on its own."""
        return self.retry_class is RetryClass.TRANSIENT

    @property
    def consumes_attempt(self) -> bool:
        """Whether `task.attempt` is incremented. Auth and rate limits never are."""
        return self.retry_class is RetryClass.TRANSIENT

    @property
    def pipeline_mode(self) -> PipelineMode | None:
        """The mode the pipeline must move to, or None to keep running."""
        if self.retry_class is RetryClass.RATE_LIMITED:
            return PipelineMode.PAUSED_RATE
        if self.is_auth_failure:
            return PipelineMode.PAUSED_AUTH
        return None

    @property
    def task_state(self) -> str:
        """The `task.state` this outcome produces."""
        if self.retry_class is RetryClass.EMPTY:
            return "empty"
        if self.retry_class is RetryClass.FATAL:
            return "failed"
        # Transient, auth and rate limited all go back to pending. What differs is whether the
        # attempt counter moved and whether the pipeline keeps running.
        return "pending"


def _coerce_code(code: Any) -> int | None:
    if code is None or isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str):
        try:
            return int(code.strip())
        except ValueError:
            return None
    return None


def classify(
    *,
    status: str | None = None,
    code: Any = None,
    message: str = "",
    http_status: int | None = None,
) -> Classification:
    """Decide the retry class for one Fyers outcome.

    Precedence is rate limit, then auth, then the documented code table, then the HTTP status.
    Rate limiting outranks everything because a 429 carried alongside any other code still costs a
    strike, and auth outranks the code table because a 401 with a stale body must not be read as
    an ordinary bad request and retried.
    """
    numeric = _coerce_code(code)
    text = message or ""

    # A success has no retry class, and inventing one would let a caller silently record an ok
    # response as `empty` and drop its candles. Callers ask only about failures.
    if status == STATUS_OK:
        raise ValueError("classify is for failures, and s is ok")

    if status == STATUS_NO_DATA:
        return Classification(
            retry_class=RetryClass.EMPTY,
            reason="no data for the requested window",
            code=numeric,
            http_status=http_status,
            message=text,
        )

    if http_status == 429 or numeric == -429:
        return Classification(
            retry_class=RetryClass.RATE_LIMITED,
            reason="rate limit exceeded",
            code=numeric,
            http_status=http_status,
            message=text,
        )

    if http_status == 401:
        return Classification(
            retry_class=RetryClass.AUTH_FATAL,
            reason="http 401, the access token was rejected",
            code=numeric,
            http_status=http_status,
            message=text,
        )

    if numeric is not None and numeric in CODE_CLASSES:
        retry_class = CODE_CLASSES[numeric]
        return Classification(
            retry_class=retry_class,
            reason=DOCUMENTED_CODES.get(numeric, "documented broker error"),
            code=numeric,
            http_status=http_status,
            message=text,
            check_symbol_encoding=numeric == -300,
        )

    if http_status is not None and http_status in HTTP_CLASSES:
        retry_class = HTTP_CLASSES[http_status]
        return Classification(
            retry_class=retry_class,
            reason=f"http {http_status}",
            code=numeric,
            http_status=http_status,
            message=text,
        )

    # An undocumented failure. A 5xx is the broker's problem and is worth one more try; anything
    # else is treated as fatal, because spending four attempts to relearn an unknown answer costs
    # budget that the three strikes rule makes expensive.
    if http_status is not None and 500 <= http_status < 600:
        return Classification(
            retry_class=RetryClass.TRANSIENT,
            reason=f"undocumented server error, http {http_status}",
            code=numeric,
            http_status=http_status,
            message=text,
        )

    return Classification(
        retry_class=RetryClass.FATAL,
        reason="undocumented broker error",
        code=numeric,
        http_status=http_status,
        message=text,
    )


def classify_exception(exc: BaseException) -> Classification:
    """Classify a transport level failure, which never reaches the envelope parsers.

    Imported lazily so this module stays importable without httpx, which keeps it usable from the
    migration and bootstrap paths.
    """
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return Classification(
            retry_class=RetryClass.TRANSIENT,
            reason="request timed out",
            message=type(exc).__name__,
        )
    if isinstance(exc, httpx.TransportError):
        return Classification(
            retry_class=RetryClass.TRANSIENT,
            reason="transport error",
            message=type(exc).__name__,
        )
    return Classification(
        retry_class=RetryClass.FATAL,
        reason="unexpected client error",
        message=type(exc).__name__,
    )


def backoff_seconds(attempt: int, *, rng: random.Random | None = None) -> float:
    """Jittered exponential backoff for a TRANSIENT retry.

    `min(60, 2 ** attempt) * (0.5 + random())`, so attempts land at roughly 1 to 3, 2 to 6, 4 to 12
    and 8 to 24 seconds. The jitter matters more than the base: eight workers that all fail at the
    same instant must not all come back at the same instant.
    """
    if attempt < 0:
        raise ValueError("attempt must not be negative")
    source = rng or random
    base = min(MAX_BACKOFF_SECONDS, 2**attempt)
    return base * (0.5 + source.random())
