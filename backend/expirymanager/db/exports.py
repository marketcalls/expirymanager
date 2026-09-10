"""Exports to Parquet and CSV.

An export is denormalised and self-describing on purpose. The person who opens the file six
months from now has no access to this application's catalog, so every row carries its symbol, its
underlying, its expiry, its strike and its right, and a Hive archive carries the catalog tables,
a manifest and a schema sidecar beside the data. That turns an archive export into something that
can be restored rather than a one-way dump.

Timestamps are written twice, as a formatted IST string and as the raw UTC epoch second. One
without the other has cost every desktop data tool a timezone bug at some point, and the two
columns together cost a few bytes that ZSTD mostly removes again.

Measured at 5,000,000 rows: Parquet ZSTD at row group 122880 is 0.38 s for 37.5 MB, Parquet
SNAPPY 0.30 s for 75.7 MB, plain CSV 0.37 s for 324.3 MB and gzipped CSV 8.60 s for 78.4 MB. So a
query export uses ZSTD at 122880, an archive export uses ZSTD at 1000000, and gzipped CSV is
never a default.

Every file is written to a temporary name in the same directory and then renamed. A rename inside
one filesystem is atomic, so an interrupted export leaves a partial temp file that the next run
cleans up, and never a truncated file that looks finished.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from starlette.concurrency import run_in_threadpool

from expirymanager.db.arrow import IST_OFFSET_SECONDS

if TYPE_CHECKING:
    from expirymanager.db.duck import DuckStore
    from expirymanager.db.reader import DuckReader

__all__ = [
    "QUERY_ROW_GROUP",
    "ARCHIVE_ROW_GROUP",
    "CATALOG_TABLES",
    "DISK_MARGIN",
    "ExportScope",
    "ExportSpec",
    "ExportResult",
    "InsufficientDisk",
    "ExportSpecError",
    "candle_query",
    "estimate_rows",
    "run_export",
    "sha256_file",
]

QUERY_ROW_GROUP = 122_880
ARCHIVE_ROW_GROUP = 1_000_000

CATALOG_TABLES = ("dim_underlying", "dim_expiry", "dim_contract", "dim_resolution")

# The estimate is a model, so the guard leaves headroom rather than trusting it exactly.
DISK_MARGIN = 0.20

# Modelled bytes per exported row, measured from the benchmark above. Parquet ZSTD came out at
# roughly 7.5 bytes per row and plain CSV at roughly 65, both on the denormalised shape.
BYTES_PER_ROW = {"parquet": 8, "csv": 70}

_FORMATS = ("parquet", "csv")
_LAYOUTS = ("single", "hive")
_COMPRESSIONS = ("zstd", "snappy", "gzip", "uncompressed")


class ExportSpecError(ValueError):
    """The requested export cannot be built."""


class InsufficientDisk(RuntimeError):
    """The estimated file plus its margin does not fit. Surfaces as 507."""

    def __init__(self, needed: int, free: int) -> None:
        super().__init__(
            f"this export is estimated at {needed} bytes including a "
            f"{int(DISK_MARGIN * 100)} percent margin and only {free} bytes are free on the "
            "export volume"
        )
        self.needed = needed
        self.free = free


@dataclass(frozen=True, slots=True)
class ExportScope:
    """What to export. Every field narrows; none of them is required."""

    underlying_id: int | None = None
    expiry_from: date | None = None
    expiry_to: date | None = None
    resolutions: tuple[int, ...] = ()
    kind: str | None = None
    option_type: str | None = None
    contract_ids: tuple[int, ...] = ()
    ts_from: datetime | None = None
    ts_to: datetime | None = None
    include_catalog: bool = False


@dataclass(frozen=True, slots=True)
class ExportSpec:
    format: str = "parquet"
    layout: str = "single"
    compression: str = "zstd"
    denormalise: bool = True
    scope: ExportScope = field(default_factory=ExportScope)

    def validate(self) -> None:
        if self.format not in _FORMATS:
            raise ExportSpecError(
                f"unknown export format {self.format!r}. Allowed: " + ", ".join(_FORMATS)
            )
        if self.layout not in _LAYOUTS:
            raise ExportSpecError(
                f"unknown export layout {self.layout!r}. Allowed: " + ", ".join(_LAYOUTS)
            )
        if self.compression.lower() not in _COMPRESSIONS:
            raise ExportSpecError(
                f"unknown compression {self.compression!r}. Allowed: "
                + ", ".join(_COMPRESSIONS)
            )
        if self.layout == "hive" and self.format != "parquet":
            raise ExportSpecError(
                "a hive layout export is Parquet only, because the layout exists to be read "
                "back as a partitioned dataset"
            )
        if self.layout == "hive" and not self.denormalise:
            raise ExportSpecError(
                "a hive layout export must be denormalised, because it partitions on the "
                "underlying symbol and the expiry date and a raw export carries neither"
            )

    @property
    def row_group(self) -> int:
        return ARCHIVE_ROW_GROUP if self.layout == "hive" else QUERY_ROW_GROUP


@dataclass(frozen=True, slots=True)
class ExportResult:
    export_id: str
    path: Path
    format: str
    layout: str
    row_count: int
    byte_size: int
    sha256: str | None
    files: tuple[Path, ...]


# The denormalised projection. Column order is deliberate: identity first, then time, then the
# values, so that a human opening the CSV can read it left to right.
_DENORMALISED = f"""
SELECT c.fyers_symbol                                   AS symbol,
       u.fyers_symbol                                   AS underlying_symbol,
       u.display_name                                   AS underlying_name,
       c.kind                                           AS kind,
       c.instrument_class                               AS instrument_class,
       c.exchange                                       AS exchange,
       c.expiry_date                                    AS expiry_date,
       CAST(c.strike AS DOUBLE)                         AS strike,
       c.option_type                                    AS option_type,
       c.lot_size                                       AS lot_size,
       r.fyers_code                                     AS resolution,
       strftime(k.ts, '%Y-%m-%d %H:%M:%S')              AS ts_ist,
       epoch(k.ts) - {IST_OFFSET_SECONDS}               AS ts_utc_epoch,
       k.open, k.high, k.low, k.close, k.volume, k.oi,
       k.contract_id                                    AS contract_id,
       k.res_id                                         AS res_id
  FROM candles k
  JOIN dim_contract c USING (contract_id)
  JOIN dim_underlying u ON u.underlying_id = c.underlying_id
  LEFT JOIN dim_resolution r ON r.res_id = k.res_id
"""

_RAW = f"""
SELECT k.contract_id, k.res_id,
       strftime(k.ts, '%Y-%m-%d %H:%M:%S')  AS ts_ist,
       epoch(k.ts) - {IST_OFFSET_SECONDS}   AS ts_utc_epoch,
       k.open, k.high, k.low, k.close, k.volume, k.oi
  FROM candles k
  JOIN dim_contract c USING (contract_id)
"""

# Hive partitions by underlying and expiry, which are the two columns anyone slicing an archive
# filters on first. Partitioning by contract would make one directory per contract.
HIVE_PARTITION = ("underlying_symbol", "expiry_date")


def _predicates(scope: ExportScope) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if scope.underlying_id is not None:
        where.append("c.underlying_id = ?")
        params.append(scope.underlying_id)
    if scope.expiry_from is not None:
        where.append("c.expiry_date >= ?")
        params.append(scope.expiry_from)
    if scope.expiry_to is not None:
        where.append("c.expiry_date <= ?")
        params.append(scope.expiry_to)
    if scope.kind is not None:
        where.append("c.kind = ?")
        params.append(scope.kind)
    if scope.option_type is not None:
        where.append("c.option_type = ?")
        params.append(scope.option_type)
    if scope.resolutions:
        where.append("k.res_id IN (" + ", ".join("?" for _ in scope.resolutions) + ")")
        params.extend(scope.resolutions)
    if scope.contract_ids:
        where.append("k.contract_id IN (" + ", ".join("?" for _ in scope.contract_ids) + ")")
        params.extend(scope.contract_ids)
    if scope.ts_from is not None:
        where.append("k.ts >= ?")
        params.append(scope.ts_from)
    if scope.ts_to is not None:
        where.append("k.ts < ?")
        params.append(scope.ts_to)
    return (" WHERE " + " AND ".join(where) if where else "", params)


def candle_query(spec: ExportSpec) -> tuple[str, list[Any]]:
    """The SELECT the export copies out, and its parameters.

    Ordered by the physical sort key, so the COPY reads the file in order and the Parquet row
    groups come out with the same clustering the store has. An unordered export would defeat
    zone map pruning for anyone who reads the file back.
    """
    body = _DENORMALISED if spec.denormalise else _RAW
    clause, params = _predicates(spec.scope)
    return (body + clause + " ORDER BY k.contract_id, k.res_id, k.ts", params)


def _count_query(spec: ExportSpec) -> tuple[str, list[Any]]:
    clause, params = _predicates(spec.scope)
    return (
        "SELECT count(*) FROM candles k JOIN dim_contract c USING (contract_id)" + clause,
        params,
    )


async def estimate_rows(reader: DuckReader, spec: ExportSpec) -> int:
    sql, params = _count_query(spec)
    value = await reader.fetch_value(sql, params)
    return int(value or 0)


def estimate_bytes(spec: ExportSpec, row_count: int) -> int:
    """A modelled file size, used only by the free space guard."""
    per_row = BYTES_PER_ROW.get(spec.format, 8)
    if spec.format == "parquet" and spec.compression.lower() in ("uncompressed", "snappy"):
        per_row *= 2
    return int(row_count * per_row * (1 + DISK_MARGIN))


def sha256_file(path: Path) -> str:
    """Content digest of one finished file, so a copied export can be verified."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_options(spec: ExportSpec, *, partitioned: bool) -> str:
    compression = spec.compression.upper()
    if spec.format == "csv":
        # CSV compression is a file suffix concern in DuckDB, and gzipped CSV measured 23 times
        # slower than plain for a file only marginally smaller than Parquet ZSTD.
        return "FORMAT CSV, HEADER"
    options = [f"FORMAT PARQUET, COMPRESSION {compression}", f"ROW_GROUP_SIZE {spec.row_group}"]
    if partitioned:
        options.append("PARTITION_BY (" + ", ".join(HIVE_PARTITION) + ")")
        options.append("OVERWRITE_OR_IGNORE TRUE")
    return ", ".join(options)


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _directory_size(path: Path) -> tuple[int, tuple[Path, ...]]:
    files = tuple(sorted(p for p in path.rglob("*") if p.is_file()))
    return (sum(p.stat().st_size for p in files), files)


def _schema_sidecar(cur: Any, sql: str, params: Sequence[Any], spec: ExportSpec) -> dict[str, Any]:
    """The column names and types of what was written, recorded beside it.

    Parquet carries its own schema and CSV does not, and neither carries the conventions: that
    ts_ist is naive IST wall clock and ts_utc_epoch is the same instant in UTC seconds. Those two
    facts are exactly what a reader six months from now needs and cannot infer.
    """
    described = cur.execute(f"DESCRIBE {sql}", list(params)).fetchall()
    return {
        "format": spec.format,
        "layout": spec.layout,
        "compression": spec.compression,
        "denormalised": spec.denormalise,
        "row_group_size": spec.row_group if spec.format == "parquet" else None,
        "columns": [{"name": row[0], "type": row[1]} for row in described],
        "conventions": {
            "ts_ist": "naive India Standard Time wall clock at bar open",
            "ts_utc_epoch": "the same instant as seconds since the Unix epoch in UTC",
            "ist_offset_seconds": IST_OFFSET_SECONDS,
            "prices": "exact decimal with four decimal places",
        },
    }


def _run_export_sync(
    store: DuckStore,
    spec: ExportSpec,
    export_id: str,
    exports_dir: Path,
    created_at: datetime,
) -> ExportResult:
    spec.validate()
    exports_dir.mkdir(parents=True, exist_ok=True)
    sql, params = candle_query(spec)

    sidecar_path: Path | None = None
    sidecar_text: str | None = None
    cur = store.cursor()
    try:
        row_count = int(cur.execute(*_count_query(spec)).fetchone()[0])
        free = shutil.disk_usage(exports_dir).free
        needed = estimate_bytes(spec, row_count)
        if needed > free:
            raise InsufficientDisk(needed=needed, free=free)

        suffix = ".parquet" if spec.format == "parquet" else ".csv"
        if spec.layout == "hive":
            final = exports_dir / export_id
            staging = exports_dir / f".{export_id}.partial"
        else:
            final = exports_dir / f"{export_id}{suffix}"
            staging = exports_dir / f".{export_id}{suffix}.partial"
        _remove(staging)

        if spec.layout == "hive":
            staging.mkdir(parents=True)
            cur.execute(
                f"COPY ({sql}) TO {_quote(staging / 'candles')} "
                f"({_copy_options(spec, partitioned=True)})",
                list(params),
            )
            if spec.scope.include_catalog:
                for table in CATALOG_TABLES:
                    cur.execute(
                        f"COPY (SELECT * FROM {table}) TO "
                        f"{_quote(staging / (table + '.parquet'))} "
                        f"({_copy_options(spec, partitioned=False)})"
                    )
            sidecar = _schema_sidecar(cur, sql, params, spec)
            sidecar["row_count"] = row_count
            sidecar["partitioned_by"] = list(HIVE_PARTITION)
            sidecar["catalog_tables"] = (
                list(CATALOG_TABLES) if spec.scope.include_catalog else []
            )
            (staging / "schema.json").write_text(
                json.dumps(sidecar, indent=2, default=str), encoding="utf-8"
            )
            (staging / "manifest.json").write_text(
                json.dumps(
                    {
                        "export_id": export_id,
                        "created_at": created_at.isoformat(timespec="seconds"),
                        "row_count": row_count,
                        "scope": _scope_json(spec.scope),
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        else:
            cur.execute(
                f"COPY ({sql}) TO {_quote(staging)} "
                f"({_copy_options(spec, partitioned=False)})",
                list(params),
            )
            sidecar = _schema_sidecar(cur, sql, params, spec)
            sidecar["row_count"] = row_count
            sidecar["export_id"] = export_id
            sidecar["created_at"] = created_at.isoformat(timespec="seconds")
            sidecar["scope"] = _scope_json(spec.scope)
            # Written only once the data file is in place, so a failed export never leaves a
            # sidecar describing a file that does not exist.
            sidecar_path = final.with_name(final.name + ".schema.json")
            sidecar_text = json.dumps(sidecar, indent=2, default=str)
    finally:
        cur.close()

    _remove(final)
    _fsync(staging)
    os.replace(staging, final)
    if sidecar_path is not None and sidecar_text is not None:
        _fsync_write(sidecar_path, sidecar_text)
    _fsync_dir(exports_dir)

    if final.is_dir():
        byte_size, files = _directory_size(final)
        digest = None
    else:
        byte_size = final.stat().st_size
        files = (final,)
        digest = sha256_file(final)

    return ExportResult(
        export_id=export_id,
        path=final,
        format=spec.format,
        layout=spec.layout,
        row_count=row_count,
        byte_size=byte_size,
        sha256=digest,
        files=files,
    )


def _scope_json(scope: ExportScope) -> dict[str, Any]:
    return {
        "underlying_id": scope.underlying_id,
        "expiry_from": scope.expiry_from,
        "expiry_to": scope.expiry_to,
        "resolutions": list(scope.resolutions),
        "kind": scope.kind,
        "option_type": scope.option_type,
        "contract_ids": list(scope.contract_ids),
        "include_catalog": scope.include_catalog,
    }


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _fsync_write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    _fsync(path)


def _fsync(path: Path) -> None:
    """Force the bytes down before the rename, so a crash cannot leave an empty finished file."""
    if path.is_dir():
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            _fsync(child)
        _fsync_dir(path)
        return
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _fsync_dir(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    except OSError:
        # Some filesystems refuse fsync on a directory handle. The rename is still atomic.
        pass
    finally:
        os.close(handle)


async def run_export(
    store: DuckStore,
    spec: ExportSpec,
    *,
    exports_dir: Path,
    export_id: str | None = None,
    created_at: datetime | None = None,
) -> ExportResult:
    """Run one export off a reader cursor, in the thread pool.

    Not through the writer queue. A COPY of five million rows takes a third of a second and holds
    nothing the writer needs, so putting it in the write queue would stall ingest for no reason.
    The manifest row that records the result is a write and does go through the writer, via
    db/writes.record_export.
    """
    return await run_in_threadpool(
        _run_export_sync,
        store,
        spec,
        export_id or uuid.uuid4().hex,
        exports_dir,
        created_at or datetime.now(),
    )
