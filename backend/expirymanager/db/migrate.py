"""Numbered plain SQL migration runner for the operational SQLite store.

Plain SQL files rather than a framework, because the schema is authored once in DATA-MODEL.md and
the migration files are meant to be diffable against it by eye. The runner adds the three
properties a schema tool must have and a folder of .sql files does not: an applied-version ledger,
a checksum that catches an edited migration before it corrupts anything, and one transaction per
file so a failure leaves no half-applied schema.

Running the runner twice is a no-op. That is asserted by tests/test_db_schema.py rather than
merely intended, because idempotency here is what makes startup safe to repeat.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, text

__all__ = [
    "Migration",
    "MigrationError",
    "MigrationChecksumError",
    "MIGRATIONS_DIR",
    "discover_migrations",
    "split_statements",
    "applied_migrations",
    "current_version",
    "migrate",
]

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

# The runner owns this table rather than 0001, because it has to exist before the first migration
# can be recorded as applied.
_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    checksum    TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """A migration could not be discovered or applied."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration file no longer matches the checksum that was recorded.

    This means the schema on disk and the schema in the file have diverged. Editing an applied
    migration is never the fix: add a new numbered file instead.
    """


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


def _checksum(sql: str) -> str:
    # Hash the normalised text and not the raw bytes, so a checkout with different line endings
    # does not read as a tampered migration.
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover_migrations(directory: Path | str | None = None) -> list[Migration]:
    """Return every migration in `directory`, ordered by version, with no gaps allowed."""
    base = Path(directory) if directory is not None else MIGRATIONS_DIR
    if not base.is_dir():
        raise MigrationError(f"migrations directory not found: {base}")

    found: list[Migration] = []
    for path in sorted(base.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"migration filename must be NNNN_lower_snake.sql, got {path.name!r}"
            )
        sql = path.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                path=path,
                sql=sql,
                checksum=_checksum(sql),
            )
        )

    found.sort(key=lambda m: m.version)
    for position, migration in enumerate(found, start=1):
        if migration.version != position:
            raise MigrationError(
                f"migration versions must be contiguous from 1, found {migration.version} "
                f"at position {position}"
            )
    return found


def split_statements(sql: str) -> list[str]:
    """Split a migration file into individual statements.

    pysqlite refuses more than one statement per execute, and `executescript` commits first, which
    would defeat the one-transaction-per-file rule. So the file is split here, respecting single
    quoted literals, line comments and block comments. Migrations must therefore not contain a
    trigger or a compound BEGIN ... END body; none of them do, and a new one would need this
    function extended rather than quietly mis-split.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)
    in_string = False

    while index < length:
        char = sql[index]

        if in_string:
            buffer.append(char)
            if char == "'":
                # A doubled quote is an escaped quote and stays inside the literal.
                if index + 1 < length and sql[index + 1] == "'":
                    buffer.append("'")
                    index += 2
                    continue
                in_string = False
            index += 1
            continue

        if char == "'":
            in_string = True
            buffer.append(char)
            index += 1
            continue

        if sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end == -1 else end
            continue

        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                raise MigrationError("unterminated block comment in migration")
            index = end + 2
            continue

        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()
            index += 1
            continue

        buffer.append(char)
        index += 1

    if in_string:
        raise MigrationError("unterminated string literal in migration")

    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


def _ensure_ledger(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(_SCHEMA_VERSION_DDL))


def applied_migrations(engine: Engine) -> dict[int, str]:
    """Map applied version to recorded checksum."""
    _ensure_ledger(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT version, checksum FROM schema_version ORDER BY version")
        ).all()
    return {int(row[0]): str(row[1]) for row in rows}


def current_version(engine: Engine) -> int:
    """Highest applied version, or 0 on an empty database."""
    applied = applied_migrations(engine)
    return max(applied) if applied else 0


def migrate(engine: Engine, *, directory: Path | str | None = None) -> list[int]:
    """Bring the database up to the newest migration. Returns the versions applied this run.

    Each file runs inside one BEGIN IMMEDIATE transaction together with its ledger row, so a
    failure rolls the whole file back and the version is not recorded.
    """
    migrations = discover_migrations(directory)
    applied = applied_migrations(engine)

    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise MigrationChecksumError(
                f"migration {migration.version:04d}_{migration.name} has changed since it was "
                f"applied (recorded {recorded[:12]}, file {migration.checksum[:12]}). "
                "Add a new migration instead of editing an applied one."
            )

    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        return []

    done: list[int] = []
    for migration in pending:
        statements = split_statements(migration.sql)
        # The DBAPI connection is used directly here so that BEGIN IMMEDIATE is the only
        # transaction opened. Going through the SQLAlchemy Connection would fire its own BEGIN
        # first, and SQLite refuses a transaction inside a transaction.
        raw = engine.raw_connection()
        try:
            cursor = raw.driver_connection.cursor()
            try:
                # BEGIN IMMEDIATE takes the write lock up front, so two processes racing to
                # migrate cannot both read version 0 and both try to create the same tables.
                cursor.execute("BEGIN IMMEDIATE")
                for statement in statements:
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_version (version, applied_at, checksum) "
                    "VALUES (?, ?, ?)",
                    (
                        migration.version,
                        datetime.now(UTC).isoformat(timespec="milliseconds"),
                        migration.checksum,
                    ),
                )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise
            finally:
                cursor.close()
        finally:
            raw.close()
        done.append(migration.version)
        log.info(
            "applied migration",
            extra={"migration_version": migration.version, "migration_name": migration.name},
        )

    return done
