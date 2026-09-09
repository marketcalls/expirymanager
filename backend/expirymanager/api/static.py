"""The production static mount for the built SPA, with the single-page fallback.

API.md section 12: `StaticFiles` over `frontend/dist`, falling back to `index.html` for any path
that is not an API route and does not resolve to a real file. The fallback is what makes a deep
link such as `/jobs/1841` work on a hard refresh, because the router that understands that path
only exists once `index.html` has loaded.

The mount is added last so that every API route and the OAuth callback are matched first. It also
refuses to answer for `/api` and `/fyers/callback` itself rather than relying on that ordering: a
mount at the root that silently returns `index.html` for a mistyped API path turns a 404 into a
200 full of HTML, which the frontend then fails to parse with a message that names the wrong
problem.

In development nothing is mounted. Vite serves the SPA on 127.0.0.1:5173 and proxies `/api` here,
so a stale `dist/` on disk must not shadow what the dev server is serving.
"""

from __future__ import annotations

import logging
from pathlib import Path

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

__all__ = [
    "INDEX_FILE",
    "RESERVED_PREFIXES",
    "SpaStaticFiles",
    "default_dist_dir",
    "mount_spa",
]

log = logging.getLogger(__name__)

INDEX_FILE = "index.html"

# Paths the SPA fallback must never answer for. Written without the leading slash because
# StaticFiles hands the path in that form.
RESERVED_PREFIXES: tuple[str, ...] = ("api/", "fyers/callback")

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Two candidates, in order. The first is a source checkout, where the built SPA sits beside the
# backend. The second is a packaged install, where the build is copied inside the package.
_DIST_CANDIDATES: tuple[Path, ...] = (
    _PACKAGE_ROOT.parent.parent / "frontend" / "dist",
    _PACKAGE_ROOT / "web",
)


def default_dist_dir() -> Path | None:
    """The built frontend, or None when there is not one.

    A directory only counts when it actually holds `index.html`. An empty `dist/` left behind by a
    failed build would otherwise mount and serve 404s for everything.
    """
    for candidate in _DIST_CANDIDATES:
        if (candidate / INDEX_FILE).is_file():
            return candidate
    return None


class SpaStaticFiles(StaticFiles):
    """`StaticFiles` with the single-page fallback, and without it for API paths."""

    async def get_response(self, path: str, scope) -> Response:  # type: ignore[no-untyped-def]
        normalised = path.lstrip("/")
        if any(normalised.startswith(prefix) for prefix in RESERVED_PREFIXES):
            # Let the API's own 404 envelope answer. Falling back to index.html here would hand a
            # fetch() a 200 full of HTML.
            raise StarletteHTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            # A real file wins; anything else is a client-side route. StaticFiles has already
            # rejected any path that escapes the directory, so this cannot serve a file outside it.
            return await super().get_response(INDEX_FILE, scope)


def mount_spa(app, dist_dir: Path | None = None) -> Path | None:  # type: ignore[no-untyped-def]
    """Mount the SPA at the root. Returns the directory mounted, or None when there was none.

    Call this after every router has been included, because a mount at `/` matches everything that
    reaches it.
    """
    directory = dist_dir if dist_dir is not None else default_dist_dir()
    if directory is None or not (directory / INDEX_FILE).is_file():
        log.info("no built frontend found, serving the api only")
        return None
    app.mount("/", SpaStaticFiles(directory=directory, html=True), name="spa")
    log.info("serving the built frontend", extra={"static_dir": str(directory)})
    return directory
