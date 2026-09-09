"""JSON logging, the redaction filter and the OAuth callback query-string scrubber.

Redaction is installed on the handlers, not on the loggers. A filter attached to a logger only
sees records that logger emits itself; records propagated up from `uvicorn.error` or `httpx` skip
it entirely. Attaching to the handler is the only placement that guarantees nothing reaches a
stream or a file unscrubbed.

Two independent mechanisms, because either alone leaks:

1. A named-key scrubber, for structured fields and for `key=value` text.
2. A JWT shape regex, because the Fyers app secret and both tokens are JWTs and they turn up in
   fields nobody named `access_token`, for example inside a verbatim upstream error body.

`security/redaction.py` (W07) is the long-term home of both. It is imported by name when present
and used in preference to what is defined here. Until it lands, the local implementation is a
working scrubber and not a no-op, because a phase where secrets reach the log file is not a phase
worth having.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expirymanager.version import APP_SLUG, __version__

__all__ = [
    "SENSITIVE_KEYS",
    "JWT_PATTERN",
    "REDACTED",
    "RedactionFilter",
    "CallbackQueryFilter",
    "JsonFormatter",
    "redact_text",
    "redact_value",
    "configure_logging",
    "correlation_id_var",
]

REDACTED = "[redacted]"
REDACTED_JWT = "[redacted-jwt]"

# From SECURITY.md section 10, split by how the name may be matched.
#
# The long names are distinctive enough to match as substrings, which catches `fyers_access_token`
# and `x_app_secret`. The short ones are matched exactly, because substring matching on `code`,
# `state` or `token` would eat `status_code`, `pipeline_state`, `task_state` and
# `token_fingerprint`, which are the diagnostics SECURITY.md says to log instead of the secret.
SENSITIVE_KEY_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "app_secret",
        "client_secret",
        "appidhash",
        "access_token",
        "refresh_token",
        "auth_code",
        "authcode",
        "csrf_token",
        "session_id",
        "wrapped_dek",
        "master_key",
        "ssl_keyfile",
        "private_key",
        "api_key",
    }
)

SENSITIVE_KEY_EXACT: frozenset[str] = frozenset(
    {
        "code",
        "state",
        "pin",
        "password",
        "passcode",
        "secret",
        "token",
        "dek",
        "kek",
        "authorization",
        "em_session",
        "em_csrf",
    }
)

SENSITIVE_KEYS: frozenset[str] = SENSITIVE_KEY_SUBSTRINGS | SENSITIVE_KEY_EXACT

# Exact field names that are safe despite containing a sensitive substring.
SAFE_KEYS: frozenset[str] = frozenset(
    {
        "app_secret_configured",
        "app_secret_set",
        "has_access_token",
        "access_token_expires_at",
    }
)

JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")

# `access_token=abc`, `"access_token": "abc"`, `access_token: abc`. The value run stops at a
# quote, comma, ampersand, whitespace or a closing brace, which covers query strings, JSON
# fragments and repr output.
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(sorted(re.escape(k) for k in SENSITIVE_KEYS)) + r")"
    r"(\"?\s*[:=]\s*\"?)"
    r"([^\"',&\s}\]]+)"
)

# `Authorization: app_id:access_token`. The Fyers header has no Bearer prefix, so the generic
# key/value rule above already catches it, but an explicit rule keeps it caught if that changes.
_AUTH_HEADER_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)")

_CALLBACK_PATH = "/fyers/callback"

try:  # Python 3.7+, kept explicit because the correlation id is optional plumbing.
    from contextvars import ContextVar

    correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
except ImportError:  # pragma: no cover - contextvars is always present on 3.14
    correlation_id_var = None  # type: ignore[assignment]


def _is_sensitive_key(name: str) -> bool:
    lowered = name.lower()
    if lowered in SAFE_KEYS:
        return False
    if lowered in SENSITIVE_KEY_EXACT:
        return True
    return any(sensitive in lowered for sensitive in SENSITIVE_KEY_SUBSTRINGS)


def redact_text(text: str) -> str:
    """Scrub a rendered string: JWT shapes first, then named key/value pairs."""
    scrubbed = JWT_PATTERN.sub(REDACTED_JWT, text)
    scrubbed = _AUTH_HEADER_PATTERN.sub(lambda m: m.group(1) + REDACTED, scrubbed)
    scrubbed = _KEY_VALUE_PATTERN.sub(lambda m: m.group(1) + m.group(2) + REDACTED, scrubbed)
    return scrubbed


def redact_value(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Scrub a structured value recursively.

    A key whose name is sensitive is replaced whatever its type, so a token nested inside a dict
    under `access_token` never gets rendered at all.
    """
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if _depth > 6:
        # Deep enough that the value is not a log line anyone reads, and deep enough that a
        # cyclic structure would otherwise recurse until the stack gives out.
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(k): redact_value(v, key=str(k), _depth=_depth + 1) for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ):
        return [redact_value(item, _depth=_depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray)):
        # Raw envelope bytes are on the never-log list, and a repr of them is no better.
        return f"<{len(value)} bytes>"
    return value


_STANDARD_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class RedactionFilter(logging.Filter):
    """Scrub the message, the format arguments and every structured extra on a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        elif record.msg is not None and not isinstance(record.msg, (int, float, bool)):
            record.msg = redact_value(record.msg)

        if record.args:
            if isinstance(record.args, Mapping):
                record.args = {
                    k: redact_value(v, key=str(k)) for k, v in record.args.items()
                }  # type: ignore[assignment]
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_value(arg) for arg in record.args)

        for name, value in list(record.__dict__.items()):
            if name in _STANDARD_RECORD_FIELDS or name.startswith("_"):
                continue
            record.__dict__[name] = redact_value(value, key=name)

        return True


class CallbackQueryFilter(logging.Filter):
    """Replace the query string with `?[redacted]` on the OAuth callback access log line.

    The uvicorn access log renders the full request line, and the callback carries `auth_code`
    and `state` in the query string. Scrubbing the pair by name is not enough here, because the
    access log formats the line positionally with no field names at all.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _CALLBACK_PATH not in str(record.getMessage()):
            return True

        if record.args and isinstance(record.args, tuple):
            record.args = tuple(
                _scrub_callback_query(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _scrub_callback_query(record.msg)
        return True


def _scrub_callback_query(text: str) -> str:
    if _CALLBACK_PATH not in text:
        return text
    return re.sub(
        re.escape(_CALLBACK_PATH) + r"\?[^\s\"']*",
        _CALLBACK_PATH + "?" + REDACTED,
        text,
    )


class JsonFormatter(logging.Formatter):
    """One JSON object per line: machine greppable, and stable across handlers."""

    def __init__(self, *, service: str = APP_SLUG) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            "version": __version__,
        }

        correlation = _current_correlation_id()
        if correlation:
            payload["correlation_id"] = correlation

        for name, value in record.__dict__.items():
            if name in _STANDARD_RECORD_FIELDS or name.startswith("_"):
                continue
            if name in payload:
                continue
            payload[name] = value

        if record.exc_info:
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", "Exception")
            payload["traceback"] = redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact_text(self.formatStack(record.stack_info))

        return json.dumps(payload, default=_json_default, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human-readable console output. Plain text, no colour codes and no symbols."""

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def formatTime(  # noqa: N802 - the logging API defines this name
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds")


def _json_default(value: Any) -> str:
    return str(value)


def _current_correlation_id() -> str | None:
    if correlation_id_var is None:
        return None
    try:
        return correlation_id_var.get()
    except LookupError:  # pragma: no cover - default makes this unreachable
        return None


def _resolve_filters() -> list[logging.Filter]:
    """Prefer W07's shared redaction filters; fall back to the ones defined here.

    W07 owns `security/redaction.py` and removes this fallback when it lands. Until then the
    fallback is a real scrubber, because Phase 0 already writes log lines that contain broker
    responses.
    """
    filters: list[logging.Filter] = []
    try:
        from expirymanager.security import redaction as redaction_module
    except ImportError:
        redaction_module = None

    if redaction_module is not None:
        for attribute in ("RedactionFilter", "CallbackQueryFilter"):
            factory = getattr(redaction_module, attribute, None)
            if factory is not None:
                try:
                    filters.append(factory())
                except Exception:  # noqa: BLE001 - never let logging setup fail on this
                    filters = []
                    break
        if len(filters) == 2:
            return filters

    return [RedactionFilter(), CallbackQueryFilter()]


def _clear_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def configure_logging(
    *,
    log_file: Path | None = None,
    level: str | int = "INFO",
    json_console: bool = False,
    max_bytes: int = 8 * 1024 * 1024,
    backup_count: int = 5,
) -> list[logging.Handler]:
    """Install the handlers and the redaction filters. Idempotent, safe to call twice.

    Returns the installed handlers so a caller can assert on them in a test.
    """
    resolved_level = logging.getLevelNamesMapping().get(
        str(level).upper(), level if isinstance(level, int) else logging.INFO
    )

    filters = _resolve_filters()
    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(JsonFormatter() if json_console else ConsoleFormatter())
    handlers.append(console)

    if log_file is not None:
        log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)

    for handler in handlers:
        handler.setLevel(resolved_level)
        for log_filter in filters:
            handler.addFilter(log_filter)

    root = logging.getLogger()
    _clear_handlers(root)
    root.setLevel(resolved_level)
    for handler in handlers:
        root.addHandler(handler)

    # These libraries install their own handlers. Removing them and letting the records propagate
    # to the root handlers is what puts every line through the redaction filters exactly once.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "apscheduler"):
        library_logger = logging.getLogger(name)
        _clear_handlers(library_logger)
        library_logger.propagate = True

    # httpx logs the request line at INFO, and a Fyers URL carries symbol and range parameters
    # that are noise at best. httpcore at DEBUG logs raw bytes.
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return handlers
