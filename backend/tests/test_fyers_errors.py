"""Every documented Fyers error code lands in exactly one retry class."""

from __future__ import annotations

import random

import httpx
import pytest

from expirymanager.brokers.fyers.errors import (
    DOCUMENTED_CODES,
    MAX_BACKOFF_SECONDS,
    Classification,
    PipelineMode,
    RetryClass,
    backoff_seconds,
    classify,
    classify_exception,
)

# The full documented table from section 4 of the reference, plus the two auth codes that only
# ever arrive on the auth endpoints. Any code added to the docs must be added here first.
EXPECTED_CODE_CLASSES = {
    -8: RetryClass.AUTH_RECOVERABLE,
    -15: RetryClass.AUTH_FATAL,
    -16: RetryClass.AUTH_RECOVERABLE,
    -17: RetryClass.AUTH_RECOVERABLE,
    -50: RetryClass.FATAL,
    -51: RetryClass.FATAL,
    -53: RetryClass.FATAL,
    -99: RetryClass.FATAL,
    -300: RetryClass.FATAL,
    -352: RetryClass.FATAL,
    -429: RetryClass.RATE_LIMITED,
    400: RetryClass.FATAL,
}

EXPECTED_HTTP_CLASSES = {
    400: RetryClass.FATAL,
    401: RetryClass.AUTH_FATAL,
    403: RetryClass.FATAL,
    429: RetryClass.RATE_LIMITED,
    500: RetryClass.TRANSIENT,
}


@pytest.mark.parametrize(("code", "expected"), sorted(EXPECTED_CODE_CLASSES.items()))
def test_every_documented_code_classifies(code: int, expected: RetryClass) -> None:
    result = classify(status="error", code=code, message="", http_status=400)
    assert result.retry_class is expected


def test_every_documented_code_has_a_description() -> None:
    assert set(EXPECTED_CODE_CLASSES) == set(DOCUMENTED_CODES)


@pytest.mark.parametrize(("http_status", "expected"), sorted(EXPECTED_HTTP_CLASSES.items()))
def test_documented_http_status_classifies(http_status: int, expected: RetryClass) -> None:
    result = classify(status="error", code=None, message="", http_status=http_status)
    assert result.retry_class is expected


def test_no_data_is_a_success_not_a_failure() -> None:
    result = classify(status="no_data", http_status=200)
    assert result.retry_class is RetryClass.EMPTY
    assert result.is_success
    assert result.task_state == "empty"
    assert not result.consumes_attempt


def test_classify_refuses_a_success() -> None:
    with pytest.raises(ValueError):
        classify(status="ok", code=200, http_status=200)


def test_rate_limit_outranks_everything_else() -> None:
    # A 429 carried alongside another code still costs a strike, so it must win.
    result = classify(status="error", code=-50, message="", http_status=429)
    assert result.retry_class is RetryClass.RATE_LIMITED
    assert result.pipeline_mode is PipelineMode.PAUSED_RATE


def test_http_401_outranks_an_unrelated_body_code() -> None:
    result = classify(status="error", code=-51, message="", http_status=401)
    assert result.retry_class is RetryClass.AUTH_FATAL


def test_auth_failures_never_consume_a_retry_attempt() -> None:
    for code in (-8, -15, -16, -17):
        result = classify(status="error", code=code, http_status=401 if code == -15 else 400)
        assert result.is_auth_failure
        assert not result.consumes_attempt
        assert result.pipeline_mode is PipelineMode.PAUSED_AUTH
        assert result.task_state == "pending"


def test_rate_limit_never_consumes_a_retry_attempt() -> None:
    result = classify(status="error", code=-429, http_status=429)
    assert not result.consumes_attempt
    assert not result.is_retryable
    assert result.task_state == "pending"


def test_transient_is_the_only_retryable_class() -> None:
    result = classify(status="error", code=None, http_status=500)
    assert result.is_retryable
    assert result.consumes_attempt
    assert result.pipeline_mode is None


def test_an_auth_failure_is_distinguishable_from_a_transient_one() -> None:
    auth = classify(status="error", code=-8, http_status=400)
    transient = classify(status="error", code=None, http_status=500)
    assert auth.is_auth_failure and not transient.is_auth_failure
    assert transient.is_retryable and not auth.is_retryable


def test_invalid_symbol_asks_for_an_encoding_check() -> None:
    result = classify(status="error", code=-300, message="invalid symbol")
    assert result.check_symbol_encoding
    assert not classify(status="error", code=-50).check_symbol_encoding


def test_a_string_code_is_accepted() -> None:
    assert classify(status="error", code="-429").retry_class is RetryClass.RATE_LIMITED


def test_an_undocumented_5xx_is_transient_and_anything_else_is_fatal() -> None:
    assert classify(status="error", code=-9999, http_status=503).retry_class is RetryClass.TRANSIENT
    assert classify(status="error", code=-9999, http_status=418).retry_class is RetryClass.FATAL


def test_transport_failures_classify_as_transient() -> None:
    request = httpx.Request("GET", "https://api-t1.fyers.in/data/history")
    for exc in (
        httpx.ConnectTimeout("timed out", request=request),
        httpx.ReadTimeout("timed out", request=request),
        httpx.ConnectError("refused", request=request),
        httpx.RemoteProtocolError("reset", request=request),
    ):
        result = classify_exception(exc)
        assert result.retry_class is RetryClass.TRANSIENT
        assert result.consumes_attempt


def test_an_unexpected_exception_is_fatal() -> None:
    assert classify_exception(ValueError("boom")).retry_class is RetryClass.FATAL


def test_backoff_stays_inside_the_documented_envelope() -> None:
    rng = random.Random(20260909)
    for attempt in range(6):
        base = min(MAX_BACKOFF_SECONDS, 2**attempt)
        for _ in range(50):
            delay = backoff_seconds(attempt, rng=rng)
            assert base * 0.5 <= delay < base * 1.5


def test_backoff_rejects_a_negative_attempt() -> None:
    with pytest.raises(ValueError):
        backoff_seconds(-1)


def test_classification_is_frozen() -> None:
    result = Classification(retry_class=RetryClass.FATAL, reason="test")
    with pytest.raises(Exception):
        result.reason = "changed"  # type: ignore[misc]
