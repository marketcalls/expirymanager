"""Inbound rate limiting: per-route budgets over `limits` 5.8.0.

This is a different concern from the outbound Fyers governor in `brokers/fyers/throttle.py` and the
two must never be conflated. This module protects this process from its own browser: a runaway
polling loop, a stuck retry, a brute force against the login form. The governor is a scheduling
constraint protecting the user's broker account, with durable daily counters and a queue behind it.
They share no code, no storage and no vocabulary.

`MemoryStorage` with `MovingWindowRateLimiter`. In-memory is correct rather than a compromise:
there is exactly one process by design, holding a single-instance lock, so a shared store would add
a dependency and buy nothing. A moving window rather than a fixed one because a fixed window lets
twice the budget through across a boundary, which on a five-per-fifteen-minutes login limit is the
difference between five attempts and ten.

`slowapi` is deliberately not used: it declares `requires_python <4,>=3.7` but its classifiers stop
at 3.13 while this project targets 3.14. `limits` is its underlying dependency anyway.

The login limit is two layers, and the second one is not in the SECURITY.md table:

- 5 per 15 minutes per (ip, username), which is the documented rule and is enforced by the route
  because the username is in the request body and middleware must not consume the body;
- 20 per 15 minutes per ip, enforced here, which closes the hole the first layer leaves open. An
  attacker who rotates the username on every attempt never fills a single (ip, username) bucket, so
  without the per-ip ceiling the documented limit constrains nothing at all.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from expirymanager.security.headers import error_response

__all__ = [
    "SCOPE_IP",
    "SCOPE_SESSION",
    "SCOPE_IP_USERNAME",
    "ENFORCED_BY_MIDDLEWARE",
    "ENFORCED_BY_ROUTE",
    "GLOBAL_FALLBACK",
    "LOGIN_PER_USERNAME",
    "SSE_CONCURRENCY_LIMIT",
    "ROUTE_LIMITS",
    "RouteLimit",
    "LimitDecision",
    "RateLimited",
    "RateLimiter",
    "ConcurrencyLimiter",
    "RateLimitMiddleware",
    "rate_limited_response",
]

SCOPE_IP = "ip"
SCOPE_SESSION = "session"
SCOPE_IP_USERNAME = "ip_username"

ENFORCED_BY_MIDDLEWARE = "middleware"
ENFORCED_BY_ROUTE = "route"

RATE_LIMIT_CODE = "rate_limited"
RATE_LIMIT_MESSAGE = "Too many requests. Wait a moment and try again."
RATE_LIMIT_STATUS = 429


def _path(pattern: str) -> re.Pattern[str]:
    """Compile a route pattern that matches with or without the `/api/v1` prefix.

    SECURITY.md writes the table without the prefix while the server sees it on every route except
    the OAuth callback. Accepting both keeps the table and the code readable as the same list.
    """
    return re.compile(r"^(?:/api/v1)?" + pattern + r"/?$")


@dataclass(frozen=True, slots=True)
class RouteLimit:
    """One row of the SECURITY.md section 8 table."""

    name: str
    limit: RateLimitItem
    scope: str
    methods: frozenset[str]
    pattern: re.Pattern[str]
    enforced_by: str = ENFORCED_BY_MIDDLEWARE

    def matches(self, method: str, path: str) -> bool:
        return method in self.methods and self.pattern.match(path) is not None


_POST = frozenset({"POST"})
_GET = frozenset({"GET"})
_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Applied in order; the first match wins, and the global fallback is always applied on top.
ROUTE_LIMITS: tuple[RouteLimit, ...] = (
    RouteLimit(
        name="login_per_ip",
        limit=parse("20/15 minutes"),
        scope=SCOPE_IP,
        methods=_POST,
        pattern=_path(r"/auth/login"),
    ),
    RouteLimit(
        name="auth_provisioning",
        limit=parse("5/hour"),
        scope=SCOPE_IP,
        methods=_POST,
        pattern=_path(r"/auth/(?:setup|password)"),
    ),
    RouteLimit(
        name="oauth",
        limit=parse("20/minute"),
        scope=SCOPE_IP,
        methods=frozenset({"GET", "POST"}),
        pattern=re.compile(
            r"^(?:/fyers/callback|(?:/api/v1)?/broker/fyers/(?:connect|callback(?:/manual)?))/?$"
        ),
    ),
    RouteLimit(
        name="bootstrap",
        limit=parse("60/minute"),
        scope=SCOPE_IP,
        methods=_GET,
        pattern=_path(r"/bootstrap"),
    ),
    RouteLimit(
        name="system_heavy",
        limit=parse("2/hour"),
        scope=SCOPE_SESSION,
        methods=_POST,
        pattern=_path(r"/system/(?:optimise|backup)"),
    ),
    RouteLimit(
        name="exports",
        limit=parse("10/minute"),
        scope=SCOPE_SESSION,
        methods=_POST,
        pattern=_path(r"/(?:exports|system/checkpoint)"),
    ),
    RouteLimit(
        name="mutations",
        limit=parse("30/minute"),
        scope=SCOPE_SESSION,
        methods=_UNSAFE,
        pattern=_path(r"/(?:downloads|jobs|schedules)(?:/.*)?"),
    ),
    RouteLimit(
        name="hot_reads",
        limit=parse("240/minute"),
        scope=SCOPE_SESSION,
        methods=_GET,
        pattern=re.compile(
            r"^(?:/api/v1)?/(?:bars.*|contracts/[^/]+/bounds|system/budget)/?$"
        ),
    ),
    RouteLimit(
        name="reads",
        limit=parse("120/minute"),
        scope=SCOPE_SESSION,
        methods=_GET,
        pattern=re.compile(r"^.*$"),
    ),
)

# Always applied in addition to whichever route rule matched.
GLOBAL_FALLBACK = RouteLimit(
    name="global",
    limit=parse("300/minute"),
    scope=SCOPE_SESSION,
    methods=frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "POST", "PUT", "PATCH", "DELETE"}),
    pattern=re.compile(r"^.*$"),
)

# Enforced by the login route, which is the only place that has parsed the username out of the
# body. The middleware cannot read the body without consuming it.
LOGIN_PER_USERNAME = RouteLimit(
    name="login_per_username",
    limit=parse("5/15 minutes"),
    scope=SCOPE_IP_USERNAME,
    methods=_POST,
    pattern=_path(r"/auth/login"),
    enforced_by=ENFORCED_BY_ROUTE,
)

# `GET /events/stream`: 10 concurrent streams per session. A concurrency ceiling, not a window,
# because an SSE connection is held open rather than repeated.
SSE_CONCURRENCY_LIMIT = 10

# Paths that are never rate limited. The SSE stream holds one long-lived request and is governed by
# the concurrency limiter instead; counting it in the per-minute windows would let one open chart
# tab starve the rest of the app.
_UNLIMITED = re.compile(r"^(?:/api/v1)?/events/stream/?$")


@dataclass(frozen=True, slots=True)
class LimitDecision:
    """The outcome of one limiter check, and the headers it produces."""

    allowed: bool
    name: str
    limit: int
    remaining: int
    reset_seconds: int

    def __bool__(self) -> bool:
        return self.allowed

    @property
    def retry_after(self) -> int:
        """Whole seconds for `Retry-After`. Never zero: a zero tells a client to retry at once."""
        return max(1, self.reset_seconds)

    def headers(self) -> dict[str, str]:
        """The draft `RateLimit-*` set. `Reset` is a delta in seconds, not an epoch."""
        return {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset": str(max(0, self.reset_seconds)),
        }


class RateLimited(Exception):
    """Raised by the route-level helpers. The app's error handler renders it as a 429."""

    def __init__(self, decision: LimitDecision) -> None:
        super().__init__(f"rate limit {decision.name} exceeded")
        self.decision = decision

    @property
    def retry_after(self) -> int:
        return self.decision.retry_after

    def headers(self) -> dict[str, str]:
        headers = self.decision.headers()
        headers["Retry-After"] = str(self.retry_after)
        return headers


def rate_limited_response(decision: LimitDecision):  # type: ignore[no-untyped-def]
    """The 429 body and headers. Used by the middleware and by the app's error handler."""
    headers = decision.headers()
    headers["Retry-After"] = str(decision.retry_after)
    return error_response(
        RATE_LIMIT_STATUS, RATE_LIMIT_CODE, RATE_LIMIT_MESSAGE, headers=headers
    )


class RateLimiter:
    """The inbound limiter. One instance per process, constructed by the app factory."""

    def __init__(
        self,
        *,
        route_limits: Iterable[RouteLimit] = ROUTE_LIMITS,
        fallback: RouteLimit | None = GLOBAL_FALLBACK,
        storage: MemoryStorage | None = None,
    ) -> None:
        self._storage = storage or MemoryStorage()
        self._limiter = MovingWindowRateLimiter(self._storage)
        self._route_limits = tuple(route_limits)
        self._fallback = fallback

    @property
    def route_limits(self) -> tuple[RouteLimit, ...]:
        return self._route_limits

    def reset(self) -> None:
        """Drop every window. Used between tests and by nothing in production."""
        self._storage.reset()

    def rule_for(self, method: str, path: str) -> RouteLimit | None:
        """The first matching rule that the middleware is responsible for."""
        for rule in self._route_limits:
            if rule.enforced_by != ENFORCED_BY_MIDDLEWARE:
                continue
            if rule.matches(method.upper(), path):
                return rule
        return None

    def hit(self, rule: RouteLimit, identity: str) -> LimitDecision:
        """Consume one unit against `rule` for `identity` and report the outcome."""
        allowed = self._limiter.hit(rule.limit, rule.name, identity)
        stats = self._limiter.get_window_stats(rule.limit, rule.name, identity)
        reset_seconds = max(0, math.ceil(stats.reset_time - time.time()))
        return LimitDecision(
            allowed=allowed,
            name=rule.name,
            limit=rule.limit.amount,
            remaining=stats.remaining,
            reset_seconds=reset_seconds,
        )

    def peek(self, rule: RouteLimit, identity: str) -> LimitDecision:
        """Report the outcome without consuming a unit."""
        allowed = self._limiter.test(rule.limit, rule.name, identity)
        stats = self._limiter.get_window_stats(rule.limit, rule.name, identity)
        reset_seconds = max(0, math.ceil(stats.reset_time - time.time()))
        return LimitDecision(
            allowed=allowed,
            name=rule.name,
            limit=rule.limit.amount,
            remaining=stats.remaining,
            reset_seconds=reset_seconds,
        )

    def check_login(self, client_ip: str, username: str) -> LimitDecision:
        """The documented 5 per 15 minutes per (ip, username). Called by the login route.

        The username is lowercased so that rotating the case does not mint a fresh bucket.
        """
        return self.hit(LOGIN_PER_USERNAME, f"{client_ip}|{username.strip().lower()}")

    def enforce_login(self, client_ip: str, username: str) -> LimitDecision:
        """`check_login`, raising `RateLimited` instead of returning a refusal."""
        decision = self.check_login(client_ip, username)
        if not decision.allowed:
            raise RateLimited(decision)
        return decision

    def evaluate(
        self, *, method: str, path: str, client_ip: str, session_key: str | None
    ) -> tuple[LimitDecision | None, list[LimitDecision]]:
        """Apply the matching route rule and the global fallback.

        Returns the first refusal, if any, and every decision made, so the caller can attach the
        headers of the most constraining one to a successful response as well.
        """
        if _UNLIMITED.match(path):
            return None, []

        decisions: list[LimitDecision] = []
        rule = self.rule_for(method, path)
        rules = [r for r in (rule, self._fallback) if r is not None]
        refusal: LimitDecision | None = None
        for candidate in rules:
            identity = self._identity(candidate.scope, client_ip, session_key)
            decision = self.hit(candidate, identity)
            decisions.append(decision)
            if not decision.allowed and refusal is None:
                refusal = decision
        return refusal, decisions

    @staticmethod
    def _identity(scope: str, client_ip: str, session_key: str | None) -> str:
        if scope == SCOPE_SESSION and session_key:
            return f"session:{session_key}"
        # A session-scoped rule on an unauthenticated request falls back to the ip. The prefix
        # keeps the two key spaces apart so a session key can never collide with an address.
        return f"ip:{client_ip}"


class ConcurrencyLimiter:
    """A ceiling on simultaneously held slots per key. `GET /events/stream` uses it.

    Thread safe, because a slot is released from whichever thread the stream ends on.
    """

    def __init__(self, limit: int = SSE_CONCURRENCY_LIMIT) -> None:
        self._limit = limit
        self._held: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def held(self, key: str) -> int:
        with self._lock:
            return self._held.get(key, 0)

    def acquire(self, key: str) -> bool:
        with self._lock:
            current = self._held.get(key, 0)
            if current >= self._limit:
                return False
            self._held[key] = current + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            current = self._held.get(key, 0)
            if current <= 1:
                self._held.pop(key, None)
            else:
                self._held[key] = current - 1


class RateLimitMiddleware:
    """Apply the per-route budgets and attach the `RateLimit-*` headers.

    Pure ASGI, mounted innermost so it sees the session that `SessionMiddleware` resolved and so a
    request rejected for CSRF never consumes a unit of anyone's budget.
    """

    def __init__(self, app, limiter: RateLimiter | None = None) -> None:  # type: ignore[no-untyped-def]
        self._app = app
        self._limiter = limiter or RateLimiter()

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        session = (scope.get("state") or {}).get("session")
        session_key = getattr(session, "id_hash", None)
        refusal, decisions = self._limiter.evaluate(
            method=scope.get("method", "GET"),
            path=scope.get("path", ""),
            client_ip=_client_ip(scope),
            session_key=session_key.hex() if isinstance(session_key, bytes) else None,
        )

        if refusal is not None:
            await rate_limited_response(refusal)(scope, receive, send)
            return

        if not decisions:
            await self._app(scope, receive, send)
            return

        # Report the rule closest to its ceiling, which is the one the client needs to back off
        # against. Reporting the loosest would tell a client it has 299 requests left while the
        # route rule is about to refuse the next one.
        tightest = min(decisions, key=lambda d: d.remaining)
        extra = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in tightest.headers().items()
        ]

        async def send_wrapper(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self._app(scope, receive, send_wrapper)


def _client_ip(scope) -> str:  # type: ignore[no-untyped-def]
    """The peer address.

    No `X-Forwarded-For` handling on purpose. The app binds loopback only and there is no proxy in
    front of it, so trusting a client-settable header here would let the browser choose its own
    rate limit bucket.
    """
    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"
