"""Console entry point. `uv run expirymanager`.

The first executable statement sets the process umask. Nothing above it may open a file, which is
why the imports the rest of this module needs are deferred until after it runs: SQLite creates
its `-wal` and `-shm` sidecars itself and DuckDB creates its `.wal`, and a chmod after the fact
never touches those.
"""

import os

os.umask(0o077)

import argparse  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from expirymanager import paths as paths_module  # noqa: E402
from expirymanager.logging_setup import configure_logging  # noqa: E402
from expirymanager.version import APP_NAME, __version__  # noqa: E402

# Fixed by the registered Fyers redirect URI, https://127.0.0.1:8000/fyers/callback, which Fyers
# matches exactly. Not configurable, and not worth a setting that can only ever be wrong.
HOST = "127.0.0.1"
PORT = 8000
APP_FACTORY = "expirymanager.app:create_app"

TLS_BANNER = (
    "TLS: this server uses a self-signed certificate it generated for 127.0.0.1.\n"
    "     Your browser will warn on the first visit. That warning is expected and is not a\n"
    "     fault. Choose to proceed, once, for https://127.0.0.1:8000."
)

NO_TLS_BANNER = (
    "TLS: no certificate was found and none could be generated, so the server is starting on\n"
    "     plain HTTP. Fyers OAuth will not complete against an http redirect URI. Restart once\n"
    "     the certificate can be created."
)

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expirymanager",
        description=f"{APP_NAME}: local Fyers expired F&O data platform.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reload on source changes. Development only.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug"),
        help="Root log level. Default info.",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="Emit JSON on the console as well as in the log file.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Override the data directory. Defaults to ~/.expirymanager.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Prepare the data directory, logging and TLS, report what was found, then exit.",
    )
    return parser


def prepare(args: argparse.Namespace) -> tuple[paths_module.Paths, tuple[Path, Path] | None]:
    """Everything that must happen before uvicorn binds: umask, directory, logging, TLS."""
    paths_module.set_process_umask()
    paths = paths_module.ensure(args.data_dir, ensure_tls=False)

    configure_logging(
        log_file=paths.log_file,
        level=args.log_level.upper(),
        json_console=args.json_logs,
    )

    tls = paths_module.ensure_tls_material(paths)
    return paths, tls


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        paths, tls = prepare(args)
    except paths_module.PathsError as exc:
        print(f"{APP_NAME} cannot start.\n{exc}", file=sys.stderr)
        return 1

    print(f"{APP_NAME} {__version__}")
    print(f"Data directory: {paths.root}")
    print(TLS_BANNER if tls else NO_TLS_BANNER)

    if args.check:
        print("Check complete. Not starting the server.")
        return 0

    try:
        lock = paths_module.InstanceLock(paths.lock_file).acquire()
    except paths_module.SingleInstanceError as exc:
        print(f"{APP_NAME} cannot start.\n{exc}", file=sys.stderr)
        return 1

    try:
        return serve(tls, reload=args.reload, log_level=args.log_level)
    finally:
        lock.release()


def serve(
    tls: tuple[Path, Path] | None, *, reload: bool = False, log_level: str = "info"
) -> int:
    """Run uvicorn. One worker, loopback only, https when the certificate exists."""
    import uvicorn

    ssl_kwargs: dict[str, str] = {}
    if tls is not None:
        key_path, cert_path = tls
        ssl_kwargs = {"ssl_keyfile": str(key_path), "ssl_certfile": str(cert_path)}

    scheme = "https" if tls else "http"
    print(f"Listening on {scheme}://{HOST}:{PORT}")

    uvicorn.run(
        APP_FACTORY,
        factory=True,
        host=HOST,
        port=PORT,
        # One worker is a correctness requirement, not a tuning choice: DuckDB allows a single
        # read-write process, and a second scheduler would double every scheduled job.
        workers=1,
        reload=reload,
        log_level=log_level,
        # The redaction filters are already installed on the root handlers, and uvicorn's own
        # dictConfig would replace them.
        log_config=None,
        access_log=True,
        **ssl_kwargs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
