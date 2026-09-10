"""Running one export as a pipeline job.

This module is the odd one out and the reason is in the schema. `job.kind` admits `export`, but
`task.kind` does not: the check constraint on the task table lists six kinds and none of them is an
export. That is correct rather than an oversight. Every task kind is one outbound Fyers request,
which is what makes the task row simultaneously the queue entry, the retry record and the request
provenance record. An export makes no request at all: it is a local COPY off a reader cursor, it
consumes no part of the daily budget, and none of the retry, lease or backoff machinery means
anything for it.

So there is no handler registered here. What this module provides instead is the runner an export
job uses: it moves `export_job` through queued, running and ready or failed, does the work through
`db/exports.run_export`, records the manifest row through the single writer, and publishes the
`export_ready` frame the UI waits on. The API route and the maintenance schedule call it directly.

Cancellation and failure both leave the ledger accurate rather than leaving a half written file
where a download link points at it: the staging name and the atomic rename are `db/exports.py`'s,
and a failure here writes the error onto the row that the export list renders.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from expirymanager.db.exports import (
    ExportResult,
    ExportScope,
    ExportSpec,
    ExportSpecError,
    InsufficientDisk,
    run_export,
)
from expirymanager.db.writes import record_export
from expirymanager.pipeline.events import EVENT_EXPORT_READY
from expirymanager.pipeline.queue import iso_at, utc_now

__all__ = [
    "EXPORT_STATUSES",
    "ExportRun",
    "spec_from_params",
    "scope_from_params",
    "create_export_row",
    "run_export_job",
]

log = logging.getLogger(__name__)

EXPORT_STATUSES = ("queued", "running", "ready", "failed", "deleted")


@dataclass(frozen=True, slots=True)
class ExportRun:
    """What one export run did, in the shape the export list and the SSE frame both want."""

    export_id: str
    status: str
    path: str | None = None
    row_count: int | None = None
    byte_size: int | None = None
    sha256: str | None = None
    error_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ready"


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _ints(value: Any) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def scope_from_params(params: Mapping[str, Any]) -> ExportScope:
    """Build the scope from a request body or a job's params_json, ignoring what it does not know."""
    return ExportScope(
        underlying_id=int(params["underlying_id"]) if params.get("underlying_id") else None,
        expiry_from=_date(params.get("expiry_from")),
        expiry_to=_date(params.get("expiry_to")),
        resolutions=_ints(params.get("resolutions")),
        kind=str(params["kind"]).upper() if params.get("kind") else None,
        option_type=str(params["option_type"]).upper() if params.get("option_type") else None,
        contract_ids=_ints(params.get("contract_ids")),
        ts_from=_datetime(params.get("ts_from")),
        ts_to=_datetime(params.get("ts_to")),
        include_catalog=bool(params.get("include_catalog", False)),
    )


def spec_from_params(params: Mapping[str, Any]) -> ExportSpec:
    """Build and validate the spec. A bad spec is refused before any file is created."""
    spec = ExportSpec(
        format=str(params.get("format", "parquet")).lower(),
        layout=str(params.get("layout", "single")).lower(),
        compression=str(params.get("compression", "zstd")).lower(),
        denormalise=bool(params.get("denormalise", True)),
        scope=scope_from_params(params),
    )
    spec.validate()
    return spec


_INSERT_EXPORT = """
INSERT INTO export_job (export_id, job_id, format, layout, compression, scope_json, status,
                        created_at)
VALUES (:export_id, :job_id, :format, :layout, :compression, :scope_json, 'queued', :now)
"""


def create_export_row(
    engine: Any,
    spec: ExportSpec,
    *,
    export_id: str | None = None,
    job_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Write the queued export_job row and return its id.

    The row exists before the work starts so a crash mid export leaves a visible queued row rather
    than nothing at all, which is the difference between an export the user can retry and one that
    silently never happened.
    """
    identifier = export_id or uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            text(_INSERT_EXPORT),
            {
                "export_id": identifier,
                "job_id": job_id,
                "format": spec.format,
                "layout": spec.layout,
                "compression": spec.compression,
                "scope_json": json.dumps(
                    dict(params or {}), separators=(",", ":"), sort_keys=True, default=str
                ),
                "now": iso_at(utc_now()),
            },
        )
    return identifier


async def run_export_job(
    *,
    engine: Any,
    store: Any,
    writer: Any,
    exports_dir: Path,
    spec: ExportSpec,
    export_id: str,
    bus: Any = None,
    job_id: str | None = None,
) -> ExportRun:
    """Do the export and leave the ledger telling the truth either way.

    The manifest row in DuckDB and the export_job row in SQLite are both written after the file
    has landed under its final name, so nothing ever points at a staging file. The order is file,
    then manifest, then the SQLite row, then the frame: each step is only announced once the one
    it describes is durable.
    """
    _set_status(engine, export_id, "running")
    try:
        result: ExportResult = await run_export(
            store, spec, exports_dir=exports_dir, export_id=export_id
        )
    except (ExportSpecError, InsufficientDisk) as exc:
        return _fail(engine, export_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - the row must record what went wrong
        log.exception("an export failed", extra={"export_id": export_id})
        return _fail(engine, export_id, f"{type(exc).__name__}: {exc}")

    await record_export(
        writer,
        export_id=result.export_id,
        kind=f"{spec.format}_{spec.layout}",
        path=str(result.path),
        filters=_manifest_filters(spec),
        row_count=result.row_count,
        byte_size=result.byte_size,
        sha256=result.sha256,
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE export_job SET status = 'ready', file_path = :path, row_count = :rows,"
                " byte_size = :bytes, sha256 = :sha256, finished_at = :now"
                " WHERE export_id = :export_id"
            ),
            {
                "path": str(result.path),
                "rows": result.row_count,
                "bytes": result.byte_size,
                "sha256": result.sha256,
                "now": iso_at(utc_now()),
                "export_id": export_id,
            },
        )

    run = ExportRun(
        export_id=export_id,
        status="ready",
        path=str(result.path),
        row_count=result.row_count,
        byte_size=result.byte_size,
        sha256=result.sha256,
    )
    if bus is not None:
        bus.publish(
            EVENT_EXPORT_READY,
            {
                "export_id": export_id,
                "job_id": job_id,
                "row_count": result.row_count,
                "byte_size": result.byte_size,
                "format": spec.format,
                "layout": spec.layout,
            },
        )
    return run


def _manifest_filters(spec: ExportSpec) -> dict[str, Any]:
    scope = spec.scope
    return {
        "underlying_id": scope.underlying_id,
        "expiry_from": scope.expiry_from.isoformat() if scope.expiry_from else None,
        "expiry_to": scope.expiry_to.isoformat() if scope.expiry_to else None,
        "resolutions": list(scope.resolutions),
        "kind": scope.kind,
        "option_type": scope.option_type,
        "contract_ids": list(scope.contract_ids),
        "ts_from": scope.ts_from.isoformat() if scope.ts_from else None,
        "ts_to": scope.ts_to.isoformat() if scope.ts_to else None,
        "denormalise": spec.denormalise,
        "compression": spec.compression,
    }


def _set_status(engine: Any, export_id: str, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE export_job SET status = :status WHERE export_id = :export_id"),
            {"status": status, "export_id": export_id},
        )


def _fail(engine: Any, export_id: str, error_text: str) -> ExportRun:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE export_job SET status = 'failed', error_text = :error,"
                " finished_at = :now WHERE export_id = :export_id"
            ),
            {"error": error_text[:2000], "now": iso_at(utc_now()), "export_id": export_id},
        )
    return ExportRun(export_id=export_id, status="failed", error_text=error_text)
