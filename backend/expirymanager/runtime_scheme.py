"""The scheme the server is actually serving on, decided once at startup.

This exists because two security decisions depend on it and neither can be a module constant.
A `Secure` cookie is never sent over http, so hardcoding `Secure` while serving http does not
harden anything, it silently breaks login: the browser accepts the Set-Cookie and then declines
to send it back, and every subsequent request looks unauthenticated with no error anywhere. The
CSRF origin allowlist has the same problem in reverse, since an origin string carries its scheme.

The scheme is not a preference. It is dictated by the redirect URI registered on the Fyers
dashboard, because Fyers matches that string exactly, scheme included. Whoever changes one must
change the other.

Set once from the entry point before the application is created, then read. A single process
serving a single origin is the whole deployment model, so a module level value is the honest
representation rather than threading a parameter through every cookie call.
"""

from __future__ import annotations

__all__ = [
    "HOST",
    "PORT",
    "set_https",
    "is_https",
    "scheme",
    "origin",
    "callback_url",
]

HOST = "127.0.0.1"
PORT = 8000

# Default false. The registered redirect URI is the authority, and it is currently http.
_https = False


def set_https(value: bool) -> None:
    """Record what the server is about to serve. Call before create_app."""
    global _https
    _https = bool(value)


def is_https() -> bool:
    return _https


def scheme() -> str:
    return "https" if _https else "http"


def origin(port: int = PORT) -> str:
    return f"{scheme()}://{HOST}:{port}"


def callback_url() -> str:
    """The redirect URI this build expects to be registered with Fyers."""
    return f"{origin()}/fyers/callback"
