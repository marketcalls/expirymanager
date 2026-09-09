"""Log redaction, handler placement and the JSON formatter.

Every secret used here is synthetic. The JWT fragments are structurally valid but carry no real
key material and decode to nothing meaningful.
"""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import pytest

from expirymanager.logging_setup import (
    REDACTED,
    REDACTED_JWT,
    CallbackQueryFilter,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    redact_text,
    redact_value,
)

# Synthetic. Shaped like a JWT so the regex has something to match, and worthless.
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMtdGVzdC1vbmx5In0.c2lnbmF0dXJl"
FAKE_SECRET = "SYNTHETICAPPSECRET1234"


def _flush(handlers: list[logging.Handler]) -> None:
    """Flush only the handlers this test installed. `logging.shutdown` would close every handler
    in the process, including the ones pytest uses to capture output."""
    for handler in handlers:
        handler.flush()


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


class TestRedactText:
    def test_a_jwt_is_replaced_wherever_it_appears(self) -> None:
        scrubbed = redact_text(f"upstream said: {FAKE_JWT} and then failed")
        assert FAKE_JWT not in scrubbed
        assert REDACTED_JWT in scrubbed

    def test_a_jwt_inside_json_is_replaced(self) -> None:
        scrubbed = redact_text(json.dumps({"unexpected_field": FAKE_JWT}))
        assert FAKE_JWT not in scrubbed

    def test_a_named_key_in_a_query_string_is_replaced(self) -> None:
        scrubbed = redact_text(f"GET /callback?auth_code={FAKE_SECRET}&state=abc123")
        assert FAKE_SECRET not in scrubbed
        assert "abc123" not in scrubbed

    def test_a_named_key_in_json_text_is_replaced(self) -> None:
        scrubbed = redact_text(f'{{"app_secret": "{FAKE_SECRET}"}}')
        assert FAKE_SECRET not in scrubbed

    def test_the_authorization_header_is_replaced(self) -> None:
        scrubbed = redact_text(f"Authorization: APPID-100:{FAKE_JWT}")
        assert FAKE_JWT not in scrubbed
        assert "APPID-100" not in scrubbed

    def test_ordinary_text_is_untouched(self) -> None:
        message = "leased 8 tasks for job 4f2a, resolution 5, 1420 rows written"
        assert redact_text(message) == message

    def test_diagnostics_the_security_doc_asks_for_survive(self) -> None:
        message = "status_code=200 pipeline_state=running token_fingerprint=a1b2c3d4"
        assert redact_text(message) == message


class TestRedactValue:
    def test_a_sensitive_key_hides_its_value_whatever_the_type(self) -> None:
        payload = {"access_token": FAKE_JWT, "expires_in": 3600}
        scrubbed = redact_value(payload)
        assert scrubbed["access_token"] == REDACTED
        assert scrubbed["expires_in"] == 3600

    def test_nesting_is_walked(self) -> None:
        payload = {"broker": {"credential": {"app_secret": FAKE_SECRET}}}
        scrubbed = redact_value(payload)
        assert scrubbed["broker"]["credential"]["app_secret"] == REDACTED

    def test_a_list_of_dicts_is_walked(self) -> None:
        scrubbed = redact_value([{"refresh_token": FAKE_JWT}, {"ok": 1}])
        assert scrubbed[0]["refresh_token"] == REDACTED
        assert scrubbed[1]["ok"] == 1

    def test_short_ambiguous_names_are_matched_exactly_not_by_substring(self) -> None:
        payload = {
            "status_code": 200,
            "pipeline_state": "running",
            "task_state": "leased",
            "token_fingerprint": "a1b2c3d4",
            "error_code": "-15",
        }
        assert redact_value(payload) == payload

    def test_the_bare_oauth_fields_are_still_caught(self) -> None:
        payload = {"code": FAKE_SECRET, "state": "opaque-value", "pin": "0000"}
        scrubbed = redact_value(payload)
        assert set(scrubbed.values()) == {REDACTED}

    def test_raw_bytes_are_never_rendered(self) -> None:
        scrubbed = redact_value({"envelope": b"EM1\x00binary-envelope-bytes"})
        assert "binary-envelope-bytes" not in str(scrubbed)

    def test_a_cycle_does_not_recurse_forever(self) -> None:
        payload: dict = {"level": 0}
        node = payload
        for depth in range(1, 12):
            node["child"] = {"level": depth}
            node = node["child"]
        redact_value(payload)


class TestRedactionFilter:
    @staticmethod
    def _record(message: str, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg=message, args=(), exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_the_message_is_scrubbed(self) -> None:
        record = self._record(f"token is {FAKE_JWT}")
        RedactionFilter().filter(record)
        assert FAKE_JWT not in record.getMessage()

    def test_format_arguments_are_scrubbed(self) -> None:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="exchange returned %s", args=(FAKE_JWT,), exc_info=None,
        )
        RedactionFilter().filter(record)
        assert FAKE_JWT not in record.getMessage()

    def test_structured_extras_are_scrubbed(self) -> None:
        record = self._record("credential stored", app_secret=FAKE_SECRET, credential_id="c-1")
        RedactionFilter().filter(record)
        assert record.app_secret == REDACTED
        assert record.credential_id == "c-1"

    def test_the_filter_returns_true_so_the_record_still_reaches_the_handler(self) -> None:
        assert RedactionFilter().filter(self._record("hello")) is True


class TestCallbackQueryFilter:
    def test_the_callback_query_string_is_replaced(self) -> None:
        record = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
            msg='%s - "%s %s HTTP/1.1" %d',
            args=("127.0.0.1:1", "GET", f"/fyers/callback?auth_code={FAKE_SECRET}&state=x", 303),
            exc_info=None,
        )
        CallbackQueryFilter().filter(record)
        rendered = record.getMessage()
        assert FAKE_SECRET not in rendered
        assert "/fyers/callback?" + REDACTED in rendered

    def test_other_paths_are_untouched(self) -> None:
        record = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
            msg="%s %s", args=("GET", "/api/v1/jobs?limit=20"), exc_info=None,
        )
        CallbackQueryFilter().filter(record)
        assert "limit=20" in record.getMessage()


class TestJsonFormatter:
    def test_one_json_object_per_line(self) -> None:
        record = logging.LogRecord(
            name="expirymanager.pipeline", level=logging.INFO, pathname=__file__, lineno=1,
            msg="job started", args=(), exc_info=None,
        )
        record.job_id = "j-1"
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "job started"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "expirymanager.pipeline"
        assert payload["job_id"] == "j-1"
        assert payload["version"]
        assert "\n" not in JsonFormatter().format(record)

    def test_a_traceback_is_scrubbed(self) -> None:
        try:
            raise ValueError(f"upstream body contained {FAKE_JWT}")
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname=__file__, lineno=1,
                msg="failed", args=(), exc_info=sys.exc_info(),
            )
        payload = json.loads(JsonFormatter().format(record))
        assert FAKE_JWT not in payload["traceback"]


class TestConfigureLogging:
    def test_filters_are_installed_on_handlers_not_on_loggers(
        self, tmp_path: Path, restore_logging
    ) -> None:
        handlers = configure_logging(log_file=tmp_path / "logs" / "app.log")
        for handler in handlers:
            names = {type(f).__name__ for f in handler.filters}
            assert "RedactionFilter" in names
            assert "CallbackQueryFilter" in names
        # A logger-level filter would miss records propagated from a library logger.
        assert logging.getLogger().filters == []

    def test_a_propagated_library_record_is_scrubbed_in_the_file(
        self, tmp_path: Path, restore_logging
    ) -> None:
        log_file = tmp_path / "logs" / "app.log"
        handlers = configure_logging(log_file=log_file, level="DEBUG")

        logging.getLogger("httpx").warning("request failed with token %s", FAKE_JWT)
        _flush(handlers)

        written = log_file.read_text(encoding="utf-8")
        assert FAKE_JWT not in written
        assert REDACTED_JWT in written

    def test_a_structured_extra_is_scrubbed_in_the_file(
        self, tmp_path: Path, restore_logging
    ) -> None:
        log_file = tmp_path / "logs" / "app.log"
        handlers = configure_logging(log_file=log_file)

        logging.getLogger("expirymanager.brokers.fyers").info(
            "credential saved", extra={"app_secret": FAKE_SECRET, "credential_id": "c-1"}
        )
        _flush(handlers)

        written = log_file.read_text(encoding="utf-8")
        assert FAKE_SECRET not in written
        assert "c-1" in written

    def test_the_log_file_is_created_0600_under_the_umask(
        self, tmp_path: Path, restore_logging
    ) -> None:
        import os

        previous = os.umask(0o077)
        try:
            log_file = tmp_path / "logs" / "app.log"
            handlers = configure_logging(log_file=log_file)
            logging.getLogger("expirymanager").info("first line")
            _flush(handlers)

            assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
            assert stat.S_IMODE(log_file.parent.stat().st_mode) == 0o700
        finally:
            os.umask(previous)

    def test_calling_twice_does_not_duplicate_handlers(
        self, tmp_path: Path, restore_logging
    ) -> None:
        configure_logging(log_file=tmp_path / "logs" / "app.log")
        first = len(logging.getLogger().handlers)
        configure_logging(log_file=tmp_path / "logs" / "app.log")
        assert len(logging.getLogger().handlers) == first

    def test_library_loggers_propagate_so_nothing_bypasses_the_filters(
        self, tmp_path: Path, restore_logging
    ) -> None:
        configure_logging(log_file=tmp_path / "logs" / "app.log")
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx"):
            library_logger = logging.getLogger(name)
            assert library_logger.propagate is True
            assert library_logger.handlers == []
