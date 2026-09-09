"""SQLAlchemy engine for the operational SQLite store.

Everything a caller needs to open `config.sqlite3` correctly lives here, because the correctness
of this file is not observable at the call site: SQLite defaults foreign keys OFF on every new
connection, and a pool that hands out an unhardened connection produces silent orphan rows rather
than an error.

All calls into the returned engine are synchronous. The application runs them through
`run_in_threadpool`, which is why the pool is sized for concurrent threads and
`check_same_thread` is disabled.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, event, text
from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

__all__ = [
    "PRAGMAS",
    "FILE_MODE",
    "DIRECTORY_MODE",
    "default_database_path",
    "create_engine",
    "init_engine",
    "get_engine",
    "dispose_engine",
    "session_factory",
    "session_scope",
    "read_pragmas",
]

# Applied to every connection, not once at startup. foreign_keys and busy_timeout are
# per-connection by definition; journal_mode is persistent but re-asserting it is free and makes
# a hand-copied database self-correcting.
PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", "5000"),
    ("secure_delete", "ON"),
    ("trusted_schema", "OFF"),
    ("cell_size_check", "ON"),
)

FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

# SQLite creates these itself when WAL is enabled, and they inherit the process umask rather than
# the mode of the main file. A chmod on config.sqlite3 alone leaves them world readable, and the
# -wal sidecar holds committed page images including freshly written ciphertext.
_SIDECAR_SUFFIXES = ("-wal", "-shm")

_DEFAULT_RELATIVE_PATH = Path(".expirymanager") / "config.sqlite3"

_engine: Engine | None = None
_engine_lock = threading.Lock()
_session_factory: sessionmaker[Session] | None = None


def default_database_path() -> Path:
    """Resolve the operational database path.

    Prefers `expirymanager.paths` when it is importable, so a test harness or a relocated data
    directory is honoured, and falls back to the documented layout otherwise. The fallback exists
    because this module is usable on its own, for example from the migration entry point.
    """
    try:
        from expirymanager import paths as _paths  # noqa: PLC0415
    except Exception:
        return Path.home() / _DEFAULT_RELATIVE_PATH

    for attribute in ("sqlite_path", "sqlite", "config_sqlite", "config_db"):
        candidate = getattr(_paths, attribute, None)
        if callable(candidate):
            candidate = candidate()
        if isinstance(candidate, (str, Path)):
            return Path(candidate)

    data_dir = getattr(_paths, "data_dir", None) or getattr(_paths, "root", None)
    if callable(data_dir):
        data_dir = data_dir()
    if isinstance(data_dir, (str, Path)):
        return Path(data_dir) / "config.sqlite3"

    return Path.home() / _DEFAULT_RELATIVE_PATH


def _prepare_file(path: Path) -> None:
    """Create the database file and its parent with restrictive modes before SQLite opens it.

    Creating the file ourselves is what makes the mode deterministic. Relying on the process
    umask works only when `__main__` set it, and this module is also imported by tests and by
    tooling that never went through the entry point.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_IMODE(parent.stat().st_mode) & 0o077:
            parent.chmod(DIRECTORY_MODE)
    except OSError:
        pass

    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        os.close(fd)
    else:
        _chmod_quietly(path)


def _chmod_quietly(path: Path) -> None:
    try:
        if stat.S_IMODE(path.stat().st_mode) != FILE_MODE:
            path.chmod(FILE_MODE)
    except OSError:
        # A file the OS removed between the stat and the chmod is not an error worth failing a
        # connection over. The next connect re-applies it.
        pass


def _harden_sidecars(path: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            _chmod_quietly(sidecar)


def create_engine(db_path: Path | str, *, echo: bool = False) -> Engine:
    """Build a hardened engine against `db_path`. Does not touch module state."""
    path = Path(db_path).expanduser()
    _prepare_file(path)

    engine = _sa_create_engine(
        f"sqlite+pysqlite:///{path}",
        echo=echo,
        poolclass=QueuePool,
        pool_size=8,
        max_overflow=8,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )

    @event.listens_for(engine, "connect")
    def _apply_pragmas(dbapi_connection, _record) -> None:  # pragma: no branch
        # SQLAlchemy emits its own BEGIN below, so the driver must not start one implicitly.
        # Without this, pysqlite hides DDL outside transactions and PRAGMA journal_mode fails
        # because journal mode cannot change inside an open transaction.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            for name, value in PRAGMAS:
                cursor.execute(f"PRAGMA {name} = {value}")
                cursor.fetchall()
        finally:
            cursor.close()
        # WAL mode materialises the sidecars on the first connection, so this has to run after
        # the pragmas rather than before them.
        _harden_sidecars(path)

    @event.listens_for(engine, "begin")
    def _emit_begin(connection) -> None:  # pragma: no branch
        connection.exec_driver_sql("BEGIN")

    return engine


def init_engine(db_path: Path | str | None = None, *, echo: bool = False) -> Engine:
    """Create the process-wide engine, or return the existing one."""
    global _engine, _session_factory
    with _engine_lock:
        if _engine is None:
            _engine = create_engine(db_path or default_database_path(), echo=echo)
            _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
        return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("SQLite engine is not initialised, call init_engine first")
    return _engine


def dispose_engine() -> None:
    """Close every pooled connection and forget the engine. Used on shutdown and between tests."""
    global _engine, _session_factory
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


def session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("SQLite engine is not initialised, call init_engine first")
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional ORM session. Commits on a clean exit, rolls back on any exception."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def read_pragmas(engine: Engine) -> dict[str, str]:
    """Read back the pragmas that were requested, on a fresh connection from the pool.

    Used by the diagnostics endpoint and by the tests, which assert the effective values rather
    than trusting that the connect listener ran.
    """
    values: dict[str, str] = {}
    with engine.connect() as connection:
        for name, _ in PRAGMAS:
            row = connection.execute(text(f"PRAGMA {name}")).first()
            values[name] = "" if row is None else str(row[0])
    return values
