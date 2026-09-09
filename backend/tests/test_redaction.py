"""The canonical redaction filters in security/redaction.py.

Every secret in this file is synthetic. The JWT fragments are shaped like a JWT so the regex has
something to match and decode to nothing; the auth code and state values are literal test strings.

The point of most of these tests is the negative half. A scrubber that redacts everything is easy
and useless: it would eat `status_code`, `pipeline_state` and `token_fingerprint`, which are the
three fields SECURITY.md section 10 says to log instead of the secret.
"""

from __future__ import annotations

import logging

import pytest

from expirymanager.security import redaction

from expirymanager.security.redaction import (
    REDACTED,
    REDACTED_JWT,
    CallbackQueryFilter,
    RedactionFilter,
    is_sensitive_key,
    redact_query_string,
    redact_text,
    redact_value,
)

# Synthetic, structurally valid, worthless.
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMtdGVzdC1vbmx5In0.c2lnbmF0dXJl"
FAKE_AUTH_CODE = "SYNTHETICAUTHCODE0123456789"
FAKE_STATE = "SYNTHETICSTATEVALUE9876543210"
FAKE_SECRET = "SYNTHETICAPPSECRET1234"

CALLBACK_URL = (
    "https://127.0.0.1:8000/fyers/callback"
    f"?s=ok&code=200&auth_code={FAKE_AUTH_CODE}&state={FAKE_STATE}"
)


def _record(msg, *args, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=args, exc_info=None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestSensitiveKeySplit:
    """The substring versus exact match split, in both directions."""

    @pytest.mark.parametrize(
        "name",
        [
            "app_secret",
            "fyers_access_token",
            "refresh_token",
            "auth_code",
            "x_app_secret",
            "csrf_token",
            "session_id_hash",
            "wrapped_dek",
            "private_key",
            "code",
            "state",
            "token",
            "secret",
            "password",
            "Authorization",
            "em_session",
        ],
    )
    def test_sensitive_names_are_matched(self, name):
        assert is_sensitive_key(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "status_code",
            "http_status_code",
            "pipeline_state",
            "task_state",
            "job_state",
            "token_fingerprint",
            "app_secret_configured",
            "has_access_token",
            "access_token_expires_at",
            "credential_id",
            "correlation_id",
            "latency_ms",
        ],
    )
    def test_diagnostic_names_are_left_alone(self, name):
        assert is_sensitive_key(name) is False


class TestRedactText:
    def test_jwt_shape_is_replaced(self):
        assert FAKE_JWT not in redact_text(f"upstream said {FAKE_JWT} on retry")
        assert REDACTED_JWT in redact_text(f"upstream said {FAKE_JWT}")

    def test_named_key_value_pairs_are_replaced(self):
        text = f'{{"app_secret": "{FAKE_SECRET}", "app_id": "SYNTH-100"}}'
        scrubbed = redact_text(text)
        assert FAKE_SECRET not in scrubbed
        assert "SYNTH-100" in scrubbed

    def test_authorization_header_is_replaced(self):
        scrubbed = redact_text(f"Authorization: SYNTH-100:{FAKE_JWT}")
        assert FAKE_JWT not in scrubbed
        assert "SYNTH-100" not in scrubbed

    def test_status_code_survives(self):
        scrubbed = redact_text("request finished status_code=429 latency_ms=12")
        assert "status_code=429" in scrubbed
        assert "latency_ms=12" in scrubbed

    def test_token_fingerprint_survives(self):
        scrubbed = redact_text("token_fingerprint=3f9a1c2e pipeline_state=running")
        assert "token_fingerprint=3f9a1c2e" in scrubbed
        assert "pipeline_state=running" in scrubbed


class TestCallbackUrl:
    """The realistic callback URL, which is the whole reason this module exists."""

    def test_redact_text_scrubs_the_callback_url(self):
        scrubbed = redact_text(CALLBACK_URL)
        assert FAKE_AUTH_CODE not in scrubbed
        assert FAKE_STATE not in scrubbed

    def test_redact_query_string_drops_the_whole_callback_query(self):
        scrubbed = redact_query_string(CALLBACK_URL)
        assert scrubbed == f"https://127.0.0.1:8000/fyers/callback?{REDACTED}"

    def test_query_parameters_are_scrubbed_on_any_path(self):
        other = f"https://example.invalid/other?auth_code={FAKE_AUTH_CODE}&state={FAKE_STATE}&page=2"
        scrubbed = redact_query_string(other)
        assert FAKE_AUTH_CODE not in scrubbed
        assert FAKE_STATE not in scrubbed
        assert "page=2" in scrubbed


class TestRedactValue:
    def test_nested_mapping_is_scrubbed_by_key(self):
        payload = {"broker": {"app_secret": FAKE_SECRET, "app_id": "SYNTH-100"}}
        scrubbed = redact_value(payload)
        assert scrubbed["broker"]["app_secret"] == REDACTED
        assert scrubbed["broker"]["app_id"] == "SYNTH-100"

    def test_list_of_strings_is_scrubbed_by_shape(self):
        scrubbed = redact_value([f"token is {FAKE_JWT}", "fine"])
        assert FAKE_JWT not in scrubbed[0]
        assert scrubbed[1] == "fine"

    def test_bytes_never_render(self):
        assert redact_value(b"\x00envelope-bytes") == "<15 bytes>"

    def test_cyclic_structure_terminates(self):
        node: dict = {"name": "root"}
        node["self"] = node
        assert redact_value(node) is not None

    def test_exception_argument_is_scrubbed(self):
        scrubbed = redact_value(RuntimeError(f"upstream body {FAKE_JWT}"))
        assert FAKE_JWT not in scrubbed


class TestRedactionFilter:
    def test_message_arguments_and_extras_are_scrubbed(self):
        record = _record(
            "connect %s",
            f"auth_code={FAKE_AUTH_CODE}",
            access_token=FAKE_JWT,
            token_fingerprint="3f9a1c2e",
        )
        assert RedactionFilter().filter(record) is True
        rendered = record.getMessage()
        assert FAKE_AUTH_CODE not in rendered
        assert record.access_token == REDACTED
        assert record.token_fingerprint == "3f9a1c2e"

    def test_dict_message_is_scrubbed(self):
        record = _record({"app_secret": FAKE_SECRET, "credential_id": "abc"})
        RedactionFilter().filter(record)
        assert record.msg["app_secret"] == REDACTED
        assert record.msg["credential_id"] == "abc"

    def test_nested_exception_argument_is_scrubbed(self):
        record = _record("failed: %s", RuntimeError(f"body {FAKE_JWT}"))
        RedactionFilter().filter(record)
        assert FAKE_JWT not in record.getMessage()

    def test_constructible_with_no_arguments(self):
        assert isinstance(RedactionFilter(), logging.Filter)


class TestCallbackQueryFilter:
    def test_access_log_request_line_is_scrubbed(self):
        # The uvicorn access log formats positionally, with no field names at all.
        record = _record(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:54321",
            "GET",
            f"/fyers/callback?s=ok&code=200&auth_code={FAKE_AUTH_CODE}&state={FAKE_STATE}",
            "1.1",
            303,
        )
        assert CallbackQueryFilter().filter(record) is True
        rendered = record.getMessage()
        assert FAKE_AUTH_CODE not in rendered
        assert FAKE_STATE not in rendered
        assert "/fyers/callback" in rendered

    def test_any_url_carrying_auth_code_is_scrubbed(self):
        record = _record(f"redirecting to https://example.invalid/x?auth_code={FAKE_AUTH_CODE}")
        CallbackQueryFilter().filter(record)
        assert FAKE_AUTH_CODE not in record.getMessage()

    def test_unrelated_line_is_untouched(self):
        record = _record("job %s finished with status_code=%d", "job-1", 200)
        CallbackQueryFilter().filter(record)
        assert record.getMessage() == "job job-1 finished with status_code=200"

    def test_constructible_with_no_arguments(self):
        assert isinstance(CallbackQueryFilter(), logging.Filter)


class TestLoggingSetupPrefersThisModule:
    """Phase 0's fallback is retired by this module existing, so assert the handshake holds."""

    def test_configure_logging_installs_these_filters(self, tmp_path):
        from expirymanager import logging_setup

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        try:
            handlers = logging_setup.configure_logging(log_file=tmp_path / "logs" / "app.log")
            installed = [type(f) for handler in handlers for f in handler.filters]
            assert RedactionFilter in installed
            assert CallbackQueryFilter in installed
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
                handler.close()
            for handler in saved_handlers:
                root.addHandler(handler)
            root.setLevel(saved_level)


class _UrlLike:
    """Stands in for httpx.URL, which logs as an object rather than a str."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


def test_non_string_argument_carrying_a_query_is_scrubbed() -> None:
    # httpx logs its request line with a URL object, so a str-only guard would let the whole
    # callback query through untouched.
    url = _UrlLike("https://127.0.0.1:8000/fyers/callback?auth_code=S3CR3T&state=xyz")
    scrubbed = str(redaction._redact_arg(url))
    assert "S3CR3T" not in scrubbed
    assert "xyz" not in scrubbed


def test_non_string_argument_without_a_query_is_returned_unchanged() -> None:
    # A %d placeholder must still receive an int, or formatting the record raises.
    assert redaction._redact_arg(42) == 42
    assert redaction._redact_arg(None) is None


def test_redaction_is_idempotent() -> None:
    # A record filtered on both the logger and its handler passes through twice, and a marker
    # that grows on each pass is both unreadable and untestable.
    once = redaction.redact_query_string("cb?auth_code=A&state=B")
    assert redaction.redact_query_string(once) == once
    callback_once = redaction.redact_query_string("/fyers/callback?auth_code=A&state=B")
    assert redaction.redact_query_string(callback_once) == callback_once


def test_unrenderable_argument_does_not_break_the_record() -> None:
    class Explodes:
        def __str__(self) -> str:
            raise RuntimeError("no repr")

    value = Explodes()
    assert redaction._redact_arg(value) is value
