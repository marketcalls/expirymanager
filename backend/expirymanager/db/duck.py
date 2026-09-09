"""The single DuckDB instance.

DuckDB permits exactly one process to hold a database file. A second OS process cannot open it
at all while a writer holds it, not even read-only: the file lock has an exclusive mode and a
shared mode and the two are mutually exclusive. That one measured fact shapes the whole
architecture, so it is made explicit here rather than surfacing later as an IOException that
reads like file corruption.

Inside the one process there is one cached database instance, one writer task that owns every
write, and reader cursors taken off the same instance. Cursors give readers an MVCC snapshot, so
a busy writer does not block them: measured 9,718 reader queries per second while a writer was
committing delete plus insert in a loop, statistically indistinguishable from the same readers
with no writer at all.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import duckdb
from starlette.concurrency import run_in_threadpool

from expirymanager.db.arrow import IST_OFFSET_SECONDS, PRICE_PRECISION, PRICE_SCALE

if TYPE_CHECKING:
    from expirymanager.db.reader import DuckReader
    from expirymanager.db.writer import DuckWriter

SCHEMA_FILE = Path(__file__).with_name("duck_schema.sql")
MACROS_FILE = Path(__file__).with_name("duck_macros.sql")

# Bumped whenever duck_schema.sql changes in a way a reader must know about.
DUCK_SCHEMA_VERSION = 1

DEFAULT_THREADS = 6
DEFAULT_MEMORY_LIMIT = "4GB"
DEFAULT_CHECKPOINT_THRESHOLD = "64MB"

# Bounds concurrent reader threads. Measured reader throughput plateaus around six.
READER_CONCURRENCY = 6

_ATTACH_TARGET = re.compile(
    r"""^\s*ATTACH\s+(?:DATABASE\s+)?(?:IF\s+NOT\s+EXISTS\s+)?['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


class DuckStoreError(RuntimeError):
    """Base class for every failure this module raises."""


class DuckStoreLockedError(DuckStoreError):
    """The database file is held by something else."""


class DuckStoreUsageError(DuckStoreError):
    """A caller asked for something the single writer model forbids."""


def locked_message(db_path: Path, detail: str) -> str:
    """The plain-text explanation shown when the file is already held.

    Naming the three real causes is the whole point. The raw DuckDB message says only
    "Conflicting lock", which every user reads as a corrupted database.
    """
    return (
        f"ExpiryManager cannot open the market database at {db_path}.\n"
        "DuckDB allows exactly one process to hold this file at a time, and something else is "
        "holding it right now.\n"
        "The three likely causes are:\n"
        "  1. A second copy of ExpiryManager is already running. Quit it and try again.\n"
        "  2. A duckdb command line session is open against this file. Type .quit in it.\n"
        "  3. A notebook, DBeaver or another database browser has the file open. Close the "
        "connection there.\n"
        "The database itself is fine. Nothing needs to be repaired.\n"
        f"DuckDB reported: {detail}"
    )


def duck_config(
    temp_directory: Path,
    *,
    threads: int = DEFAULT_THREADS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    checkpoint_threshold: str = DEFAULT_CHECKPOINT_THRESHOLD,
) -> dict[str, Any]:
    """The pinned connection configuration.

    Every value here overrides a DuckDB default that is wrong for this workload. threads is 6
    and not the host core count so that one export cannot starve the chart queries. memory_limit
    is explicit because the default is 80 percent of RAM. temp_directory is explicit because the
    default is relative to the process working directory, which for a desktop app is wherever the
    user happened to launch it from.
    """
    return {
        "TimeZone": "Asia/Kolkata",
        "threads": threads,
        "memory_limit": memory_limit,
        "temp_directory": str(temp_directory),
        "preserve_insertion_order": "false",
        "checkpoint_threshold": checkpoint_threshold,
    }


def guard_statement(sql: str, db_path: Path) -> None:
    """Refuse a statement that would break the single instance rule.

    ATTACH of a new file is legitimate: it is how the compaction routine rewrites the store.
    ATTACH of the live file is not, and DuckDB reports it as a unique file handle conflict that
    is hard to trace back to the statement that caused it.
    """
    match = _ATTACH_TARGET.match(sql)
    if match is None:
        return
    target = Path(match.group(1)).expanduser()
    try:
        same = target.resolve() == db_path.resolve()
    except OSError:
        same = str(target) == str(db_path)
    if same:
        raise DuckStoreUsageError(
            "Refusing to ATTACH the live market database. There is already one open instance "
            "in this process and DuckDB rejects a second handle on the same file. Use the "
            "existing connection, or ATTACH a different file as the compaction routine does."
        )


class DuckStore:
    """Owns the one DuckDB connection, its schema and its writer.

    Nothing else in the application calls ``duckdb.connect``.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        temp_directory: Path | str | None = None,
        app_version: str = "unknown",
        threads: int = DEFAULT_THREADS,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        checkpoint_threshold: str = DEFAULT_CHECKPOINT_THRESHOLD,
        read_only: bool = False,
    ) -> None:
        if read_only:
            # Verified: once a read-write connection exists in this process, opening the same
            # file read-only fails with a ConnectionException about differing configuration. The
            # API and the writer share one instance instead.
            raise DuckStoreUsageError(
                "The market database is never opened read-only. A read-only handle conflicts "
                "with the read-write instance this process already holds. Read through "
                "DuckStore.reader, which takes a cursor off the same instance."
            )
        self.db_path = Path(db_path).expanduser()
        self.temp_directory = (
            Path(temp_directory).expanduser()
            if temp_directory is not None
            else self.db_path.parent / "tmp"
        )
        self.app_version = app_version
        self.config = duck_config(
            self.temp_directory,
            threads=threads,
            memory_limit=memory_limit,
            checkpoint_threshold=checkpoint_threshold,
        )
        self._con: duckdb.DuckDBPyConnection | None = None
        self._reader: DuckReader | None = None
        self._writer: DuckWriter | None = None

    # -- connection lifecycle ------------------------------------------------

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            raise DuckStoreUsageError("The market database is not open.")
        return self._con

    @property
    def is_open(self) -> bool:
        return self._con is not None

    def open(self) -> None:
        """Connect and bring the schema up to date. Safe to call on an existing database."""
        if self._con is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.temp_directory.mkdir(parents=True, exist_ok=True)
        try:
            self._con = duckdb.connect(str(self.db_path), config=self.config)
        except duckdb.IOException as exc:
            raise DuckStoreLockedError(locked_message(self.db_path, str(exc))) from exc
        try:
            self.apply_schema()
        except Exception:
            self._con.close()
            self._con = None
            raise
        # The file is created 0600 by the umask the process sets at startup, but a database
        # restored from a backup or copied in by hand can arrive world readable.
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def apply_schema(self) -> None:
        """Apply the DDL, the macros and the meta seeds. Idempotent by construction."""
        con = self.connection
        con.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        con.execute(MACROS_FILE.read_text(encoding="utf-8"))
        self._seed_meta(con)

    def _seed_meta(self, con: duckdb.DuckDBPyConnection) -> None:
        """Record the conventions that a future reader cannot infer from the DDL alone."""
        duckdb_version = con.execute("SELECT version()").fetchone()[0]
        facts: Mapping[str, str] = {
            "schema_version": str(DUCK_SCHEMA_VERSION),
            "app_version": self.app_version,
            "duckdb_version": str(duckdb_version),
            "candles.ts.timezone": "Asia/Kolkata",
            "candles.ts.semantics": "naive local wall clock at bar open",
            "ist_offset_seconds": str(IST_OFFSET_SECONDS),
            "candles.price.type": f"DECIMAL({PRICE_PRECISION},{PRICE_SCALE})",
        }
        for key, value in facts.items():
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])
        # created_at records when this file was first made, so it must survive later startups.
        con.execute(
            "INSERT INTO meta VALUES ('created_at', ?) ON CONFLICT (key) DO NOTHING",
            [datetime.now(timezone.utc).isoformat(timespec="seconds")],
        )

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A fresh cursor on the shared instance, with its own transaction context."""
        return self.connection.cursor()

    def checkpoint(self) -> None:
        """Fold the write ahead log into the main file.

        A backup taken without this, or without the .wal sidecar, restores a database missing
        the most recent writes.
        """
        self.connection.execute("CHECKPOINT")

    def close(self) -> None:
        if self._con is None:
            return
        try:
            self._con.execute("CHECKPOINT")
        finally:
            self._con.close()
            self._con = None
            self._reader = None
            self._writer = None

    # -- reader and writer handles ------------------------------------------

    @property
    def reader(self) -> DuckReader:
        from expirymanager.db.reader import DuckReader

        if self._reader is None:
            self._reader = DuckReader(self, concurrency=READER_CONCURRENCY)
        return self._reader

    @property
    def writer(self) -> DuckWriter:
        from expirymanager.db.writer import DuckWriter

        if self._writer is None:
            self._writer = DuckWriter(self)
        return self._writer

    # -- async lifecycle used by the FastAPI lifespan ------------------------

    async def start(self) -> None:
        """Open the database and start the one writer task."""
        await run_in_threadpool(self.open)
        await self.writer.start()

    async def aclose(self) -> None:
        """Drain the writer, checkpoint and close."""
        if self._writer is not None:
            await self._writer.stop()
        await run_in_threadpool(self.close)
