"""The application factory.

`__main__` runs `uvicorn.run("expirymanager.app:create_app", factory=True, workers=1)`, so this is
where the process is assembled: the middleware stack, the routers, the error handlers, the static
mount and the lifespan.

Middleware order, outermost first, which is the order the list below is written in:

  CorrelationId    binds the id before anything can log, including a host rejection
  TrustedHost      rejects a foreign Host, the only defence against DNS rebinding
  SecurityHeaders  the CSP and the rest of the set, on every response including a rejection
  Session          resolves the cookie into request.state, never issues and never rejects
  CSRF             needs the session the previous layer resolved
  RateLimit        innermost, so a request rejected for CSRF costs nobody a unit of budget

Every one of them is pure ASGI rather than `BaseHTTPMiddleware`, which matters for exactly one
route: `BaseHTTPMiddleware` buffers a response through an anyio task group, and that turns
`GET /api/v1/events/stream` into a response that arrives only when the stream ends.

There is no `CORSMiddleware` anywhere in this codebase and there must not be. There is one browser
origin in development, `https://127.0.0.1:5173` with the Vite proxy carrying `/api`, and one in
production, `https://127.0.0.1:8000`. Adding CORS would only widen what the CSRF layer narrows.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from expirymanager import paths as paths_module
from expirymanager.api import static as static_module
from expirymanager.api import v1 as api_v1
from expirymanager.api.errors import CorrelationIdMiddleware, install_error_handlers
from expirymanager.lifespan import AppState, lifespan_context
from expirymanager.security.csrf import CsrfMiddleware, DEFAULT_EXEMPT_PATHS
from expirymanager.security.headers import (
    ENVIRONMENT_DEVELOPMENT,
    ENVIRONMENT_PRODUCTION,
    SecurityHeadersMiddleware,
)
from expirymanager.security.ratelimit import RateLimitMiddleware
from expirymanager.security.sessions import SessionMiddleware
from expirymanager.version import APP_NAME, __version__

__all__ = ["ALLOWED_HOSTS", "OAUTH_CALLBACK_MODULE", "create_app"]

log = logging.getLogger(__name__)

# SECURITY.md section 6.3. `localhost` is deliberately absent: the app has exactly one host name
# and narrowing the allowlist costs nothing. Port 5173 is the Vite dev server, which proxies to
# this process and forwards its own Host.
ALLOWED_HOSTS: list[str] = ["127.0.0.1", "127.0.0.1:8000", "127.0.0.1:5173"]

# W16 creates this module. It holds `GET /fyers/callback` and is included at the ROOT, outside the
# `/api` prefix, because the registered Fyers redirect URI is exactly
# https://127.0.0.1:8000/fyers/callback and Fyers matches it character for character. It is
# included here rather than in api/v1/__init__.py for that reason alone.
OAUTH_CALLBACK_MODULE = "expirymanager.api.oauth_callback"


class ServiceProxy:
    """Resolve a service off `AppState` at first use.

    Starlette builds its middleware stack while handling the lifespan scope, which is before the
    lifespan body has run, so `SessionManager` and `RateLimiter` do not exist yet when
    `SessionMiddleware` and `RateLimitMiddleware` are constructed. Deferring the lookup to the
    first attribute access moves it to the first request, by which point startup has completed.

    The alternative was to build those two services in the factory instead of in the lifespan,
    which would have meant opening the SQLite engine outside the ordered startup sequence and
    losing the guarantee that a failed startup tears down exactly what it started.
    """

    def __init__(self, state: AppState, attribute: str) -> None:
        self._state = state
        self._attribute = attribute

    def __getattr__(self, name: str) -> Any:
        service = getattr(self._state, self._attribute)
        if service is None:
            raise RuntimeError(
                f"{self._attribute} was read before startup finished building it"
            )
        return getattr(service, name)


def _include_oauth_callback(app: FastAPI) -> bool:
    """Include `GET /fyers/callback` at the root once W16 has created the module."""
    import importlib

    try:
        module = importlib.import_module(OAUTH_CALLBACK_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != OAUTH_CALLBACK_MODULE:
            raise
        log.debug("oauth callback module not built yet", extra={"build_item": "W16"})
        return False
    app.include_router(module.router, tags=["broker"])
    return True


def _install_components() -> None:
    """Register the pipeline, the job recovery step, the scheduler and the task handlers.

    None of these register themselves at import time, deliberately: doing that as an import side
    effect changes the behaviour of any test that merely imports the module, and it makes the set
    of installed components depend on import order. So each exposes an explicit install(), and this
    is the one place that calls them.

    Without this the application still starts and every route answers, which is exactly what makes
    the omission dangerous: the three lifespan slots stay empty, the worker registry stays empty,
    api.deps.get_supervisor raises its documented 503, no schedule ever fires, and any leased task
    fails with "no handler is registered for task kind". Nothing looks broken until a download is
    started and silently does nothing.

    Import failures are tolerated per component so a partially built tree still boots, which is how
    this file behaved through the phases when these modules did not exist yet.
    """
    from expirymanager.lifespan import (
        SLOT_JOB_RECOVERY,
        SLOT_PIPELINE_SUPERVISOR,
        SLOT_SCHEDULER,
    )

    components = (
        ("expirymanager.pipeline.supervisor", "install", SLOT_PIPELINE_SUPERVISOR),
        ("expirymanager.pipeline.jobs", "install", SLOT_JOB_RECOVERY),
        ("expirymanager.scheduler.service", "install", SLOT_SCHEDULER),
        # Not a lifespan slot: this one fills the worker handler registry.
        ("expirymanager.pipeline.handlers.candle_chunk", "install_all", None),
    )

    from expirymanager.lifespan import registered_components

    already = set(registered_components())

    for module_name, function_name, slot in components:
        # An explicit prior registration wins. A test that registers a fake supervisor and then
        # builds an app must keep its fake, and building the app twice in one process must not
        # replace a running component's factory underneath it.
        if slot is not None and slot in already:
            continue
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            log.debug("component not built yet", extra={"component": module_name})
            continue
        installer = getattr(module, function_name, None)
        if installer is None:
            log.warning(
                "component has no installer, slot stays empty",
                extra={"component": module_name, "installer": function_name, "slot": slot},
            )
            continue
        installer()


def create_app(
    *,
    root: Path | str | None = None,
    environment: str = ENVIRONMENT_DEVELOPMENT,
    dist_dir: Path | None = None,
    serve_static: bool | None = None,
) -> FastAPI:
    """Build the application.

    `root` overrides the data directory, which is what lets a test run against a temporary one.
    `serve_static` forces the SPA mount on or off; the default mounts it whenever a built frontend
    is on disk, because a build being present is the honest signal that this process is the one
    serving the browser rather than Vite.
    """
    _install_components()
    paths = paths_module.ensure(root, ensure_tls=False)
    state = AppState(paths=paths, environment=environment, app_version=__version__)

    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        lifespan=lambda application: lifespan_context(application, state),
        # Swagger UI and ReDoc load their assets from a CDN, which `script-src 'self'` blocks, so
        # they would render an empty page. The schema itself stays available in development.
        docs_url=None,
        redoc_url=None,
        openapi_url=(
            "/api/v1/openapi.json" if environment != ENVIRONMENT_PRODUCTION else None
        ),
        middleware=[
            Middleware(CorrelationIdMiddleware),
            Middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS),
            Middleware(SecurityHeadersMiddleware, environment=environment),
            Middleware(SessionMiddleware, manager=ServiceProxy(state, "session_manager")),
            # The exempt list is exactly one path and must stay that way. The callback is a
            # cross-site top-level navigation from Fyers that cannot carry a header, and it is
            # protected by the single-use state parameter instead.
            Middleware(CsrfMiddleware, exempt_paths=DEFAULT_EXEMPT_PATHS),
            Middleware(RateLimitMiddleware, limiter=ServiceProxy(state, "rate_limiter")),
        ],
    )

    # Set before the first request as well as by the lifespan, so that a route reached from a
    # TestClient built without the context manager still finds the container.
    app.state.services = state

    install_error_handlers(app)

    app.include_router(api_v1.build_router(), prefix=api_v1.API_V1_PREFIX)
    _include_oauth_callback(app)

    # Last, because a mount at the root matches everything that reaches it.
    if serve_static is None:
        serve_static = dist_dir is not None or static_module.default_dist_dir() is not None
    if serve_static:
        static_module.mount_spa(app, dist_dir)

    return app
