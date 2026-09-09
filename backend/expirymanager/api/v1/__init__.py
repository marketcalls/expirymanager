"""The `/api/v1` router, and the include list for every route module.

The full list is written here now, before the modules exist, so that W16 to W20 create their own
file and nothing else: no edit to this file, no edit to the app factory, no ordering to get right.
A module that is not present yet is skipped with a debug line, so the application boots and serves
today with only `bootstrap.py` in place.

The skip is deliberately narrow. Only a `ModuleNotFoundError` naming the module in the spec is
treated as "not built yet"; a module that exists and fails to import, including one whose own
import of something else is missing, propagates. Otherwise the first typo inside a route module
would make its routes silently disappear and every request to them would 404 with no explanation.

Path convention: each module declares its routes exactly as API.md writes them, minus the
`/api/v1` prefix, which is applied once when this router is included by the app factory. So
`api/v1/auth.py` declares `@router.post("/auth/login")`. That is the same form the rate limit
table in `security/ratelimit.py` uses, so a route and its limit can be read side by side.

`GET /fyers/callback` is not here. It is mounted at the root by the app factory, because the
registered Fyers redirect URI is exactly `https://127.0.0.1:8000/fyers/callback` and Fyers matches
it character for character.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field

from fastapi import APIRouter

__all__ = ["API_V1_PREFIX", "RouterSpec", "ROUTER_SPECS", "build_router", "missing_modules"]

log = logging.getLogger(__name__)

API_V1_PREFIX = "/api/v1"

_PACKAGE = "expirymanager.api.v1"


@dataclass(frozen=True, slots=True)
class RouterSpec:
    """One route module and the work item that owns it."""

    module: str
    tags: tuple[str, ...] = ()
    item: str = ""
    attribute: str = "router"

    @property
    def dotted(self) -> str:
        return f"{_PACKAGE}.{self.module}"


# The complete surface from API.md, in the order the document sets it out. `bootstrap` is the only
# one that exists today; the rest are the next stage.
ROUTER_SPECS: tuple[RouterSpec, ...] = (
    RouterSpec("bootstrap", ("bootstrap",), "W15"),
    RouterSpec("auth", ("auth",), "W16"),
    RouterSpec("broker", ("broker",), "W16"),
    RouterSpec("underlyings", ("underlyings",), "W17"),
    RouterSpec("expiries", ("expiries",), "W17"),
    RouterSpec("contracts", ("contracts",), "W17"),
    RouterSpec("downloads", ("downloads",), "W18"),
    RouterSpec("jobs", ("jobs",), "W18"),
    RouterSpec("coverage", ("coverage",), "W18"),
    RouterSpec("bars", ("bars",), "W19"),
    RouterSpec("chain", ("chain",), "W19"),
    RouterSpec("exports", ("exports",), "W20"),
    RouterSpec("schedules", ("schedules",), "W20"),
    RouterSpec("system", ("system",), "W20"),
    RouterSpec("events", ("events",), "W20"),
)


@dataclass
class _IncludeReport:
    included: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _include(router: APIRouter, spec: RouterSpec, report: _IncludeReport) -> None:
    try:
        module = importlib.import_module(spec.dotted)
    except ModuleNotFoundError as exc:
        if exc.name != spec.dotted:
            # The module exists but something it imports does not. That is a real error and
            # hiding it would turn a broken import into a silent 404 on every one of its routes.
            raise
        report.missing.append(spec.module)
        log.debug(
            "route module not built yet",
            extra={"route_module": spec.module, "build_item": spec.item},
        )
        return

    sub_router = getattr(module, spec.attribute, None)
    if sub_router is None:
        raise AttributeError(
            f"{spec.dotted} must expose an APIRouter named {spec.attribute!r} "
            f"(owned by {spec.item})"
        )
    router.include_router(sub_router, tags=list(spec.tags))
    report.included.append(spec.module)


def build_router() -> APIRouter:
    """Build the `/api/v1` router with every module that exists."""
    router = APIRouter()
    report = _IncludeReport()
    for spec in ROUTER_SPECS:
        _include(router, spec, report)
    log.info(
        "api routers included",
        extra={"routers_included": report.included, "routers_pending": report.missing},
    )
    return router


def missing_modules() -> tuple[str, ...]:
    """The specs with no module on disk. Used by tests and by the startup diagnostics."""
    report = _IncludeReport()
    for spec in ROUTER_SPECS:
        try:
            importlib.import_module(spec.dotted)
        except ModuleNotFoundError as exc:
            if exc.name != spec.dotted:
                raise
            report.missing.append(spec.module)
    return tuple(report.missing)
