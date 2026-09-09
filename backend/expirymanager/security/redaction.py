"""The canonical log redaction filters and scrubbers.

`logging_setup.configure_logging` imports `RedactionFilter` and `CallbackQueryFilter` from here by
name and prefers them over its Phase 0 fallback whenever both are importable, so this module is the
one place the never-log list in SECURITY.md section 10 is maintained. Nothing here imports
`logging_setup`: the dependency runs one way only, otherwise configuring logging would import the
thing it is configuring.

Two independent mechanisms are used together, because either alone leaks:

1. A named-key scrubber over structured fields and over `key=value` text.
2. A JWT shape regex, because the Fyers app secret and both broker tokens are JWTs and they turn
   up in fields nobody named `access_token`, most obviously inside a verbatim upstream error body.

The split between substring matched and exactly matched key names is deliberate and is the part
most likely to be broken by a well meaning edit. Long, distinctive names are matched as substrings
so `fyers_access_token` and `x_app_secret` are caught. Short generic names are matched exactly,
because substring matching on `code`, `state` or `token` would eat `status_code`, `pipeline_state`,
`task_state` and `token_fingerprint`, and those four are precisely the diagnostics SECURITY.md
section 10 says to log instead of the secret. Eating them would leave the pipeline undiagnosable
while making the log no safer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "REDACTED",
    "REDACTED_JWT",
    "SENSITIVE_KEY_SUBSTRINGS",
    "SENSITIVE_KEY_EXACT",
    "SENSITIVE_KEYS",
    "SAFE_KEYS",
    "SENSITIVE_QUERY_PARAMS",
    "JWT_PATTERN",
    "CALLBACK_PATH",
    "is_sensitive_key",
    "redact_text",
    "redact_value",
    "redact_query_string",
    "RedactionFilter",
    "CallbackQueryFilter",
]

REDACTED = "[redacted]"
REDACTED_JWT = "[redacted-jwt]"

# Long, distinctive names. Matched as a substring of the field name.
SENSITIVE_KEY_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "app_secret",
        "client_secret",
        "appidhash",
        "app_id_hash",
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
        "passphrase",
    }
)

# Short, generic names. Matched exactly, never as a substring. See the module docstring.
SENSITIVE_KEY_EXACT: frozenset[str] = frozenset(
    {
        "code",
        "state",
        "token",
        "secret",
        "password",
        "passcode",
        "pin",
        "dek",
        "kek",
        "authorization",
        "cookie",
        "set-cookie",
        "em_session",
        "em_csrf",
    }
)

SENSITIVE_KEYS: frozenset[str] = SENSITIVE_KEY_SUBSTRINGS | SENSITIVE_KEY_EXACT

# Names that carry a sensitive substring but hold a boolean or a timestamp rather than the secret.
# Redacting these would hide the only fields the settings screen and the token banner are built on.
SAFE_KEYS: frozenset[str] = frozenset(
    {
        "app_secret_configured",
        "app_secret_set",
        "has_access_token",
        "has_refresh_token",
        "access_token_expires_at",
        "refresh_token_expires_at",
    }
)

# The Fyers app secret and both broker tokens are JWTs. Matching the shape catches them wherever
# they appear, including in a field name this module has never heard of.
JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")

CALLBACK_PATH = "/fyers/callback"

# Query parameters scrubbed out of any URL, not only the callback one. `auth_code` and `state` are
# the pair the OAuth callback carries; the rest are here because a hand built debug URL is exactly
# where a token ends up when someone is chasing a bug at two in the morning.
SENSITIVE_QUERY_PARAMS: tuple[str, ...] = (
    "auth_code",
    "authcode",
    "state",
    "code",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "passcode",
    "api_key",
    "appidhash",
)

# `access_token=abc`, `"access_token": "abc"`, `access_token: abc`. The value run stops at a quote,
# comma, ampersand, whitespace or a closing bracket, which covers query strings, JSON fragments and
# repr output alike.
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(sorted(re.escape(key) for key in SENSITIVE_KEYS)) + r")"
    r"(\"?\s*[:=]\s*\"?)"
    r"([^\"',&\s}\]]+)"
)

# `Authorization: app_id:access_token`. The Fyers header has no Bearer prefix, so the generic
# key/value rule above already catches it. The explicit rule keeps it caught if that ever changes.
_AUTH_HEADER_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)")

_QUERY_PARAM_PATTERN = re.compile(
    r"(?i)([?&](?:" + "|".join(re.escape(name) for name in SENSITIVE_QUERY_PARAMS) + r")=)"
    r"([^&\s\"'>\]]*)"
)

# The whole query string of the callback URL goes, not just the named parameters. The uvicorn
# access log formats the request line positionally with no field names at all, and a broker that
# adds a parameter tomorrow must not be able to widen the leak.
_CALLBACK_QUERY_PATTERN = re.compile(re.escape(CALLBACK_PATH) + r"\?[^\s\"'>\]]*")

# Guard against a cyclic or absurdly nested structure walking the stack down.
_MAX_DEPTH = 6

_STANDARD_RECORD_FIELDS: frozenset[str] = frozenset(
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
        "message",
        "module",
        "msecs",
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


def is_sensitive_key(name: str) -> bool:
    """True when a structured field of this name must never carry its value into a log line."""
    lowered = name.lower()
    if lowered in SAFE_KEYS:
        return False
    if lowered in SENSITIVE_KEY_EXACT:
        return True
    return any(sensitive in lowered for sensitive in SENSITIVE_KEY_SUBSTRINGS)


def redact_query_string(text: str) -> str:
    """Scrub sensitive parameters out of any URL or query string in ``text``.

    Applied to every message, not only to the callback path, because a URL reaches the log from
    httpx request logging, from an exception repr and from hand written debug lines, and only one
    of those three knows which endpoint it belongs to.
    """
    scrubbed = _CALLBACK_QUERY_PATTERN.sub(CALLBACK_PATH + "?" + REDACTED, text)
    return _QUERY_PARAM_PATTERN.sub(lambda m: m.group(1) + REDACTED, scrubbed)


def redact_text(text: str) -> str:
    """Scrub a rendered string: JWT shapes, then the Authorization header, then key/value pairs."""
    scrubbed = JWT_PATTERN.sub(REDACTED_JWT, text)
    scrubbed = _AUTH_HEADER_PATTERN.sub(lambda m: m.group(1) + REDACTED, scrubbed)
    scrubbed = _KEY_VALUE_PATTERN.sub(lambda m: m.group(1) + m.group(2) + REDACTED, scrubbed)
    return redact_query_string(scrubbed)


def redact_value(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Scrub a structured value recursively.

    A field whose name is sensitive is replaced whatever its type, so a token nested inside a dict
    under `access_token` is never rendered at all rather than rendered and then pattern matched.
    """
    if key is not None and is_sensitive_key(key):
        return REDACTED
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(k): redact_value(v, key=str(k), _depth=_depth + 1) for k, v in value.items()
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Raw envelope bytes are on the never-log list and a repr of them is no better.
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple, set, frozenset)) or (
        isinstance(value, Sequence) and not isinstance(value, str)
    ):
        return [redact_value(item, _depth=_depth + 1) for item in value]
    if isinstance(value, BaseException):
        # An exception carries its arguments in its repr, and an upstream client error carries the
        # response body in its arguments.
        return redact_text(repr(value))
    return value


class RedactionFilter(logging.Filter):
    """Scrub the message, the format arguments and every structured extra on a record.

    Constructible with no arguments, because `logging_setup` instantiates it by name.

    Installed on the handlers rather than on the loggers. A filter attached to a logger only sees
    the records that logger emits itself; records propagated up from `uvicorn.error` or `httpx`
    skip it entirely.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        elif isinstance(record.msg, BaseException):
            record.msg = redact_text(repr(record.msg))
        elif record.msg is not None and not isinstance(record.msg, (int, float, bool)):
            record.msg = redact_value(record.msg)

        if record.args:
            if isinstance(record.args, Mapping):
                record.args = {  # type: ignore[assignment]
                    key: redact_value(value, key=str(key))
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_value(arg) for arg in record.args)

        for name, value in list(record.__dict__.items()):
            if name in _STANDARD_RECORD_FIELDS or name.startswith("_"):
                continue
            record.__dict__[name] = redact_value(value, key=name)

        return True


class CallbackQueryFilter(logging.Filter):
    """Strip the OAuth query string out of any log line that carries a URL.

    Constructible with no arguments, because `logging_setup` instantiates it by name.

    SECURITY.md section 7 requires this for the uvicorn access log, where the request line is
    formatted positionally and the named-key scrubber has no field names to work with. It is
    applied to every record rather than only to lines mentioning the callback path, because
    `auth_code` and `state` reach the log from the httpx request line and from exception reprs too,
    and a filter that only fires on one path is a filter that misses the other two.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_query_string(record.msg)

        if record.args and isinstance(record.args, tuple):
            record.args = tuple(
                redact_query_string(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif record.args and isinstance(record.args, Mapping):
            record.args = {  # type: ignore[assignment]
                key: redact_query_string(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }

        return True
