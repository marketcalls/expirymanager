"""Checkpoint, health assertions, size reporting and the compaction rewrite.

Four measured facts drive everything here.

A DuckDB file only grows. Ten delete-then-insert rewrite cycles took a 75.5 MB file to 77.6 MB,
and VACUUM followed by CHECKPOINT left it at 77.6 MB. VACUUM recomputes statistics; it does not
reclaim space, so it is never used.

The only real reclaim is a full rewrite: ATTACH a new file, CREATE TABLE AS SELECT ordered by the
sort key, fsync, rename, reopen. Measured 48.2 MB down to 34.1 MB. Because it closes and swaps
the live file it is a manual action with a free space guard, never a scheduled job.

A backup without CHECKPOINT first, or without the .wal sidecar, restores a database missing the
most recent writes. So the checkpoint action reports the WAL size on both sides, which is the
only visible evidence that it did anything.

``candles`` has no primary key and no unique constraint, and that is a deliberate measured
decision. The duplicate assertion here is what a constraint would otherwise have done, run on
demand rather than paid for on every insert.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.concurrency import run_in_threadpool

from expirymanager.db.ids import (
    BLOCK_SIZE,
    FIRST_BLOCK_BASE,
    SPOT_ID_MAX,
    sequence_next_unit,
)

if TYPE_CHECKING:
    from expirymanager.db.duck import DuckStore
    from expirymanager.db.reader import DuckReader

logger = logging.getLogger(__name__)

__all__ = [
    "BYTES_PER_CANDLE_ROW",
    "BLOAT_SUGGEST_RATIO",
    "COMPACTION_MARGIN",
    "SEQUENCES",
    "CheckpointResult",
    "StorageReport",
    "CompactionResult",
    "InsufficientDisk",
    "checkpoint",
    "storage_report",
    "health_checks",
    "duplicate_rows",
    "reconcile_coverage",
    "refresh_trading_days",
    "compact",
]

# Measured: nine columns of DECIMAL(11,4) and BIGINT compress to roughly 15 bytes a row in the
# DuckDB block format. Used only to model expected size, never to decide anything on its own.
BYTES_PER_CANDLE_ROW = 15.2

# Suggest a compaction once the file is half again as large as the model says it should be. Below
# that the rewrite costs more downtime than it reclaims.
BLOAT_SUGGEST_RATIO = 1.5

# A compaction writes a second full copy before it swaps, so it needs the current file plus a
# margin free at once.
COMPACTION_MARGIN = 0.20

SEQUENCES = ("seq_contract_block", "seq_expiry_id", "seq_run_id")

# Every table that is copied by a compaction. Listed explicitly rather than discovered, so that a
# table added later fails a test here rather than silently disappearing from a user's database.
COMPACTED_TABLES = (
    ("candles", "contract_id, res_id, ts"),
    ("dim_underlying", "underlying_id"),
    ("dim_expiry", "expiry_id"),
    ("dim_contract", "contract_id"),
    ("dim_resolution", "res_id"),
    ("dim_trading_day", "exchange, trade_date"),
    ("candle_coverage", "contract_id, res_id, range_from"),
    ("contract_bounds", "contract_id, res_id"),
    ("ingest_run", "run_id"),
    ("export_manifest", "created_at"),
    ("meta", "key"),
    ("candle_greeks", "contract_id, res_id, ts"),
    ("chain_snapshot", "snapshot_ts"),
    ("dim_instrument_master", "fytoken, valid_from"),
    ("symbol_master_snapshot", "snapshot_date, file"),
)

# Tables the schema seeds on open, so the target already holds rows that would collide with the
# copy. Cleared immediately before the copy rather than skipped, because the live values are the
# authority and dim_resolution is re-seeded again on the next open anyway.
PRESEEDED_TABLES = frozenset({"dim_resolution", "meta"})


class InsufficientDisk(RuntimeError):
    """Not enough free space for the operation. Surfaces as 507."""

    def __init__(self, needed: int, free: int) -> None:
        super().__init__(
            f"this operation needs {needed} bytes free and only {free} bytes are available"
        )
        self.needed = needed
        self.free = free


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    wal_bytes_before: int
    wal_bytes_after: int


@dataclass(frozen=True, slots=True)
class StorageReport:
    duckdb_bytes: int
    duckdb_wal_bytes: int
    sqlite_bytes: int
    exports_bytes: int
    raw_payload_bytes: int
    candle_rows: int
    bytes_per_row: float
    modelled_bytes: int
    bloat_ratio: float
    compaction_suggested: bool
    free_disk_bytes: int


@dataclass(frozen=True, slots=True)
class CompactionResult:
    bytes_before: int
    bytes_after: int
    tables_copied: int
    rows_copied: int
    duration_seconds: float

    @property
    def bytes_reclaimed(self) -> int:
        return self.bytes_before - self.bytes_after


def _wal_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".wal")


def _size(path: Path) -> int:
    try:
        if path.is_dir():
            return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        return path.stat().st_size
    except OSError:
        return 0


def _checkpoint_sync(store: DuckStore) -> CheckpointResult:
    wal = _wal_path(store.db_path)
    before = _size(wal)
    store.connection.execute("CHECKPOINT")
    return CheckpointResult(wal_bytes_before=before, wal_bytes_after=_size(wal))


async def checkpoint(store: DuckStore) -> CheckpointResult:
    """Fold the write ahead log into the main file and report both sizes.

    Run before any backup. A copy of the .duckdb taken without this, or taken without its .wal
    sidecar, restores a database that is missing the most recent writes, and nothing about the
    restored file says so.
    """
    return await run_in_threadpool(_checkpoint_sync, store)


async def storage_report(
    reader: DuckReader,
    *,
    db_path: Path,
    sqlite_path: Path | None = None,
    exports_dir: Path | None = None,
    raw_dir: Path | None = None,
) -> StorageReport:
    """The numbers the storage screen renders, including the bloat heuristic.

    The bloat ratio compares the real file against the modelled size of the rows it holds. It is
    a heuristic and it says so: it drives a suggestion, not an automatic rewrite, because the
    rewrite requires closing the live database.
    """
    rows = int(await reader.fetch_value("SELECT count(*) FROM candles") or 0)
    duckdb_bytes = _size(db_path)
    modelled = int(rows * BYTES_PER_CANDLE_ROW)
    ratio = (duckdb_bytes / modelled) if modelled > 0 else 1.0
    try:
        free = shutil.disk_usage(db_path.parent).free
    except OSError:
        free = 0
    return StorageReport(
        duckdb_bytes=duckdb_bytes,
        duckdb_wal_bytes=_size(_wal_path(db_path)),
        sqlite_bytes=_size(sqlite_path) if sqlite_path else 0,
        exports_bytes=_size(exports_dir) if exports_dir else 0,
        raw_payload_bytes=_size(raw_dir) if raw_dir else 0,
        candle_rows=rows,
        bytes_per_row=(duckdb_bytes / rows) if rows else 0.0,
        modelled_bytes=modelled,
        bloat_ratio=round(ratio, 4),
        compaction_suggested=rows > 0 and ratio >= BLOAT_SUGGEST_RATIO,
        free_disk_bytes=free,
    )


async def health_checks(reader: DuckReader) -> list[dict[str, Any]]:
    """The shipped v_data_health rows, plus the id keyspace assertions.

    The keyspace checks belong here rather than in the view because the rules they enforce live
    in db/ids.py: spot ids stay under 1000, every other id sits inside its expiry's declared
    block, and no two expiries share a block. A violation of any of them means a query that
    prunes on contract_id silently returns the wrong rows, which is the worst failure mode this
    system has.
    """
    columns, rows = await reader.fetch_columns("SELECT * FROM v_data_health")
    results = [dict(zip(columns, row)) for row in rows]

    stray_spot = await reader.fetch_value(
        "SELECT count(*) FROM dim_contract WHERE kind = 'SPOT' AND contract_id > ?",
        [SPOT_ID_MAX],
    )
    results.append({"check_name": "spot_id_out_of_reserved_range", "offending": int(stray_spot or 0)})

    low_contract = await reader.fetch_value(
        "SELECT count(*) FROM dim_contract WHERE kind <> 'SPOT' AND contract_id < ?",
        [FIRST_BLOCK_BASE],
    )
    results.append({"check_name": "contract_id_below_first_block", "offending": int(low_contract or 0)})

    outside = await reader.fetch_value(
        "SELECT count(*) FROM dim_contract c JOIN dim_expiry e USING (expiry_id) "
        " WHERE c.kind <> 'SPOT' AND e.contract_id_lo IS NOT NULL "
        "   AND (c.contract_id < e.contract_id_lo OR c.contract_id > e.contract_id_hi)"
    )
    results.append({"check_name": "contract_id_outside_block", "offending": int(outside or 0)})

    overlapping = await reader.fetch_value(
        "SELECT count(*) FROM dim_expiry a JOIN dim_expiry b "
        "    ON a.expiry_id < b.expiry_id "
        "   AND a.contract_id_lo <= b.contract_id_hi "
        "   AND b.contract_id_lo <= a.contract_id_hi "
        " WHERE a.contract_id_lo IS NOT NULL AND b.contract_id_lo IS NOT NULL"
    )
    results.append({"check_name": "overlapping_id_blocks", "offending": int(overlapping or 0)})

    unaligned = await reader.fetch_value(
        "SELECT count(*) FROM dim_expiry WHERE contract_id_lo IS NOT NULL "
        "   AND contract_id_lo % ? <> 0",
        [BLOCK_SIZE],
    )
    results.append({"check_name": "unaligned_id_blocks", "offending": int(unaligned or 0)})

    orphan = await reader.fetch_value(
        "SELECT count(*) FROM (SELECT DISTINCT contract_id FROM candles) k "
        " WHERE NOT EXISTS (SELECT 1 FROM dim_contract c WHERE c.contract_id = k.contract_id)"
    )
    results.append({"check_name": "candles_without_a_contract", "offending": int(orphan or 0)})

    return results


async def duplicate_rows(reader: DuckReader, *, limit: int = 50) -> list[tuple[Any, ...]]:
    """The duplicate keys a PRIMARY KEY on candles would have prevented.

    The key was measured and rejected: it took a 5,000,000 row file from 75.5 MB to 341.6 MB and
    the load from 0.45 s to 2.02 s while point lookups were unchanged. This assertion is the
    cheap half of that trade, and it must stay green.
    """
    return await reader.fetch_all(
        "SELECT contract_id, res_id, ts, count(*) AS copies FROM candles "
        " GROUP BY 1, 2, 3 HAVING count(*) > 1 ORDER BY copies DESC, contract_id LIMIT ?",
        [max(1, int(limit))],
    )


async def reconcile_coverage(reader: DuckReader, *, limit: int = 200) -> list[dict[str, Any]]:
    """Where the coverage ledger and the fact table disagree about a row count.

    A disagreement means either a chunk was written and its rows were not, or rows were removed
    without their ledger entry. Both make the planner ask for the wrong thing, so this is
    reported rather than repaired: the repair is to re-request the chunk.
    """
    # The comparison sits outside the aggregate, because DuckDB cannot yet reference an alias
    # carrying a correlated subquery from HAVING or ORDER BY.
    columns, rows = await reader.fetch_columns(
        "SELECT * FROM ("
        "  SELECT cov.contract_id, cov.res_id, sum(cov.row_count) AS claimed, "
        "         (SELECT count(*) FROM candles k "
        "           WHERE k.contract_id = cov.contract_id AND k.res_id = cov.res_id) AS actual "
        "    FROM candle_coverage cov WHERE cov.status = 'ok' "
        "   GROUP BY cov.contract_id, cov.res_id) "
        " WHERE claimed <> actual ORDER BY abs(claimed - actual) DESC LIMIT ?",
        [max(1, int(limit))],
    )
    return [dict(zip(columns, row)) for row in rows]


def _refresh_trading_days_sync(cur: Any, res_id: int) -> int:
    """Rebuild dim_trading_day from the spot bars actually observed.

    Derived from data and never from a weekday or holiday rule. NSE and BSE expiry weekdays have
    changed several times since 2022, and a hardcoded rule corrupts history silently rather than
    failing.
    """
    cur.execute("BEGIN TRANSACTION")
    try:
        cur.execute("DELETE FROM dim_trading_day WHERE derived_from = 'spot_bars'")
        cur.execute(
            "INSERT INTO dim_trading_day "
            "(exchange, trade_date, session_open, session_close, bar_count, contract_count, "
            " derived_from) "
            "SELECT u.exchange, CAST(k.ts AS DATE), min(k.ts), max(k.ts), count(*), "
            "       count(DISTINCT k.contract_id), 'spot_bars' "
            "  FROM candles k JOIN dim_underlying u ON u.spot_contract_id = k.contract_id "
            " WHERE k.res_id = ? "
            " GROUP BY u.exchange, CAST(k.ts AS DATE)",
            [res_id],
        )
        row = cur.execute(
            "SELECT count(*) FROM dim_trading_day WHERE derived_from = 'spot_bars'"
        ).fetchone()
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    return int(row[0]) if row else 0


async def refresh_trading_days(writer: Any, *, res_id: int = 2) -> int:
    """Rebuild the observed trading day calendar. Goes through the single writer."""
    from expirymanager.db.writer import CallableWrite

    return await writer.submit(
        CallableWrite(
            label="trading_day_refresh", fn=lambda cur: _refresh_trading_days_sync(cur, res_id)
        )
    )


def _sequence_positions(cur: Any) -> dict[str, int]:
    """Where each sequence has got to, so a rewritten file resumes rather than restarts.

    Losing this would hand a second expiry the same id block as an existing one, and every query
    that prunes on contract_id would then return another expiry's rows.
    """
    positions: dict[str, int] = {}
    for name in SEQUENCES:
        row = cur.execute(
            "SELECT start_value, last_value FROM duckdb_sequences() WHERE sequence_name = ?",
            [name],
        ).fetchone()
        if row is None:
            continue
        start, last = int(row[0]), row[1]
        positions[name] = start if last is None else int(last) + 1
    # The block sequence is asserted through db/ids.py, so the two cannot disagree.
    positions["seq_contract_block"] = sequence_next_unit(cur)
    return positions


def _compact_sync(store: DuckStore, *, keep_backup: bool) -> CompactionResult:
    started = datetime.now()
    db_path = store.db_path
    before = _size(db_path) + _size(_wal_path(db_path))

    free = shutil.disk_usage(db_path.parent).free
    needed = int(before * (1 + COMPACTION_MARGIN))
    if needed > free:
        raise InsufficientDisk(needed=needed, free=free)

    target = db_path.with_name(db_path.name + ".compact")
    if target.exists():
        target.unlink()
    _remove_quietly(_wal_path(target))

    con = store.connection
    con.execute("CHECKPOINT")
    positions = _sequence_positions(con)

    # The target is built by the ordinary schema path rather than by CREATE TABLE AS SELECT, so
    # it keeps every primary key, unique constraint, check, view and macro. A CTAS copy would
    # reclaim the space and quietly drop all of them, and nothing would notice until a duplicate
    # underlying appeared months later.
    _build_empty_target(store, target)

    rows_copied = 0
    tables_copied = 0
    # The live connection runs with preserve_insertion_order off, which is right for ordinary
    # queries and wrong for this one: the ORDER BY below is the physical clustering being
    # restored, so it has to survive into the new file.
    con.execute("SET preserve_insertion_order = true")
    con.execute(f"ATTACH '{target}' AS compacted")
    try:
        for name, order_by in COMPACTED_TABLES:
            exists = con.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database() "
                "AND table_name = ?",
                [name],
            ).fetchone()
            if not exists or not exists[0]:
                continue
            if name in PRESEEDED_TABLES:
                con.execute(f"DELETE FROM compacted.{name}")
            con.execute(
                f"INSERT INTO compacted.{name} SELECT * FROM {name} ORDER BY {order_by}"
            )
            counted = con.execute(f"SELECT count(*) FROM compacted.{name}").fetchone()
            rows_copied += int(counted[0]) if counted else 0
            tables_copied += 1
        for name, position in positions.items():
            con.execute(f"DROP SEQUENCE IF EXISTS compacted.{name}")
            con.execute(f"CREATE SEQUENCE compacted.{name} START {position}")
    finally:
        con.execute("DETACH compacted")
        con.execute("SET preserve_insertion_order = false")

    store.close()

    backup = db_path.with_name(db_path.name + ".before-compaction")
    _remove_quietly(backup)
    _remove_quietly(_wal_path(backup))
    os.replace(db_path, backup)
    stale_wal = _wal_path(db_path)
    if stale_wal.exists():
        os.replace(stale_wal, _wal_path(backup))
    os.replace(target, db_path)

    store.open()
    # The schema is re-applied on open, which rebuilds the views and the macros that CTAS did
    # not copy, and re-seeds dim_resolution.
    after = _size(db_path) + _size(_wal_path(db_path))
    if not keep_backup:
        _remove_quietly(backup)
        _remove_quietly(_wal_path(backup))
    return CompactionResult(
        bytes_before=before,
        bytes_after=after,
        tables_copied=tables_copied,
        rows_copied=rows_copied,
        duration_seconds=round((datetime.now() - started).total_seconds(), 3),
    )


def _build_empty_target(store: DuckStore, target: Path) -> None:
    """Create the compaction target with the full schema, then let go of it.

    A second DuckStore on a different file is legal: the one instance rule is per file, and this
    handle is closed before the live connection ever attaches the file.
    """
    from expirymanager.db.duck import DuckStore as _DuckStore

    seed = _DuckStore(
        target, temp_directory=store.temp_directory, app_version=store.app_version
    )
    seed.open()
    seed.close()


def _remove_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("could not remove %s during compaction", path)


async def compact(store: DuckStore, *, keep_backup: bool = True) -> CompactionResult:
    """Rewrite the database into a fresh file, reclaiming space and restoring the sort order.

    The caller must have quiesced the writer first. This closes and reopens the live database, so
    it cannot run while anything is mid transaction, and it is a manual Settings action rather
    than a scheduled job for exactly that reason.

    The previous file is kept beside the new one by default. The rewrite is the only operation in
    this system that replaces the user's whole dataset in one step, and a rename is cheap
    insurance against it going wrong.
    """
    return await run_in_threadpool(_compact_sync, store, keep_backup=keep_backup)
