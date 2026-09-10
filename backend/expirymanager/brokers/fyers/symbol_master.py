"""The public symbol master, its daily snapshot and the slowly changing dimension behind it.

An expired contract vanishes from the symbol master. Lot size, tick size and freeze quantity are
only knowable while the contract is still listed, and the three expired endpoints return symbol
strings and OHLCV arrays and nothing else. So the daily snapshot is the only chance this system
ever gets to record a dated lot size, and a day that is missed is missed permanently rather than
temporarily. That is why this job runs before anything else, why it does not depend on a live
token, and why it carries a truncation guard that refuses to close rows on a short file.

Three measured facts shape the implementation.

1. The seven master files are plain unauthenticated objects on https://public.fyers.in. Measured
   on 2026-09-10 they answer HTTP 200 with no Authorization header and no cookie, they serve
   gzip when it is asked for, and they carry an ETag and a Last-Modified. They are not the
   trading API, they are not rate limited by the plan, and they consume no part of the daily
   request budget. They therefore bypass the governor entirely and keep working while the token
   is dead, which is exactly what PIPELINE.md asks of the symbol_master schedule.

2. NSE_FO alone is 81 MB of JSON holding 78,585 members. A json.load of that produces a Python
   dict several times its size, so the file is streamed one member at a time and pushed into a
   staging table in batches. Nothing here ever holds the whole file.

3. The top level is an object keyed by symTicker, not an array, so DuckDB's read_json cannot
   ingest it as rows without materialising the whole object first. The streaming member reader
   below is a JSON envelope walker built on the standard library decoder, not a second symbol
   parser: the symbol strings themselves still go through symbology.parse_symbol.

The dimension is type 2. A change to any tracked attribute closes the current row with
valid_to set to the effective date and opens a new one from that date, so a position sizing query
asking what the lot size was on a given day resolves the value that was true on that day rather
than the value that is true now.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pyarrow as pa

from expirymanager.brokers.fyers.calendar import IST, exchange_data_floor
from expirymanager.brokers.fyers.roots import (
    SEGMENT_CODES,
    RootRegistry,
    UnderlyingRoot,
    builtin_registry,
    default_registry,
)
from expirymanager.brokers.fyers.symbology import EXCHANGE_CODES, parse_symbol
from expirymanager.version import user_agent

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    import duckdb

    from expirymanager.db.reader import DuckReader
    from expirymanager.db.writer import DuckWriter

__all__ = [
    "MASTER_BASE_URL",
    "MASTER_FILES",
    "FO_FILES",
    "CM_FILES",
    "FILE_EXCHANGE",
    "FILE_SEGMENT",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_STAGE_BATCH",
    "DEFAULT_SHRINK_GUARD",
    "META_ROOT_CHARCLASS",
    "META_LAST_RUN",
    "SCD_COLUMNS",
    "STAGE_COLUMNS",
    "stage_batch",
    "SymbolMasterError",
    "MasterFileUnknown",
    "MasterFileTruncated",
    "MasterRow",
    "FetchedMaster",
    "FileResult",
    "RunResult",
    "MasterFact",
    "RootCandidate",
    "RejectedRoot",
    "ObservedCharClass",
    "RootRebuild",
    "SymbolMasterDiffWrite",
    "SnapshotWrite",
    "MetaWrite",
    "master_url",
    "exchange_of",
    "segment_of",
    "iter_master_members",
    "iter_master_rows",
    "master_row",
    "row_hash",
    "expiry_from_epoch",
    "fetch_master",
    "apply_master_file",
    "last_snapshot",
    "point_in_time_sql",
    "fact_on",
    "facts_on",
    "observed_root_charclass",
    "build_root_candidates",
    "rebuild_root_registry",
    "SymbologyAudit",
    "SymbologyDisagreement",
    "audit_symbology",
    "audit_rows_against_symbology",
    "META_SYMBOLOGY_AUDIT",
    "feed_default_registry",
    "run_symbol_master_snapshot",
]

log = logging.getLogger(__name__)

MASTER_BASE_URL = "https://public.fyers.in/sym_details/"

# The seven files, in the order DATA-MODEL.md lists them. CM before FO inside an exchange is
# deliberate in the run order below, because the FO root rebuild resolves under_fytoken against
# the CM rows and wants them current.
MASTER_FILES: tuple[str, ...] = (
    "NSE_CD",
    "NSE_FO",
    "NSE_COM",
    "NSE_CM",
    "BSE_CM",
    "BSE_FO",
    "MCX_COM",
)

# The derivative files whose underlying is a listed cash instrument, and the cash file that
# resolves it. NSE_CD and NSE_COM are excluded on purpose: a currency pair and a commodity have
# no cash row to resolve against, and neither is served by the expired endpoints.
FO_TO_CM: Mapping[str, str] = {"NSE_FO": "NSE_CM", "BSE_FO": "BSE_CM"}
FO_FILES: tuple[str, ...] = tuple(FO_TO_CM)
CM_FILES: tuple[str, ...] = tuple(FO_TO_CM.values())

# Measured from every one of the seven files on 2026-09-10: each file carries exactly one
# (exchange, segment) pair and no fytoken appears in two files. That is what lets one file be
# diffed against its own slice of the dimension without touching another file's rows.
FILE_EXCHANGE: Mapping[str, str] = {
    "NSE_CD": "NSE",
    "NSE_FO": "NSE",
    "NSE_COM": "NSE",
    "NSE_CM": "NSE",
    "BSE_CM": "BSE",
    "BSE_FO": "BSE",
    "MCX_COM": "MCX",
}
FILE_SEGMENT: Mapping[str, str] = {
    "NSE_CD": "CD",
    "NSE_FO": "FO",
    "NSE_COM": "COM",
    "NSE_CM": "CM",
    "BSE_CM": "CM",
    "BSE_FO": "FO",
    "MCX_COM": "COM",
}

DEFAULT_TIMEOUT_SECONDS = 120.0

# 81 MB of JSON arrives in about two seconds over gzip, so the read buffer is sized for throughput
# rather than for latency.
READ_CHUNK_BYTES = 1 << 20

# Rows per staging insert. Large enough that the per statement overhead disappears, small enough
# that the batch itself is a bounded amount of Python memory.
DEFAULT_STAGE_BATCH = 20_000

# A file must hold at least this fraction of the previous snapshot's row count before its diff is
# allowed to close rows. A truncated or partially served file is otherwise indistinguishable from
# a mass delisting, and acting on it would close thousands of rows that are still live. Expiry
# days shrink NSE_FO by at most about a fifth, so half is a wide margin.
DEFAULT_SHRINK_GUARD = 0.5

META_ROOT_CHARCLASS = "symbol_master.root_charclass"
META_LAST_RUN = "symbol_master.last_run"
META_SYMBOLOGY_AUDIT = "symbol_master.symbology_audit"

# The dim_instrument_master business columns, in DDL order. valid_from, valid_to and row_hash are
# handled separately because they are dimension bookkeeping rather than vendor attributes.
SCD_COLUMNS: tuple[str, ...] = (
    "fytoken",
    "symbol_ticker",
    "exchange_code",
    "segment_code",
    "ex_instrument_type",
    "under_symbol",
    "under_fytoken",
    "ex_series",
    "isin",
    "min_lot_size",
    "tick_size",
    "qty_freeze",
    "qty_multiplier",
    "face_value",
    "strike_price",
    "option_type",
    "expiry_date",
    "trading_session",
    "symbol_details",
)

_STAGING_DDL = """
CREATE OR REPLACE TEMP TABLE stg_instrument_master (
    fytoken            VARCHAR NOT NULL,
    symbol_ticker      VARCHAR NOT NULL,
    exchange_code      UTINYINT,
    segment_code       UTINYINT,
    ex_instrument_type INTEGER,
    under_symbol       VARCHAR,
    under_fytoken      VARCHAR,
    ex_series          VARCHAR,
    isin               VARCHAR,
    min_lot_size       INTEGER,
    tick_size          DECIMAL(9,4),
    qty_freeze         INTEGER,
    qty_multiplier     DECIMAL(12,4),
    face_value         DECIMAL(12,4),
    strike_price       DECIMAL(12,4),
    option_type        VARCHAR,
    expiry_date        DATE,
    trading_session    VARCHAR,
    symbol_details     VARCHAR,
    row_hash           VARCHAR NOT NULL
)
"""

STAGE_COLUMNS: tuple[str, ...] = (*SCD_COLUMNS, "row_hash")

# The staging batch schema, pinned so that DuckDB casts nothing on the way in and the decimal
# scales match the dimension exactly. Batches go in through Arrow rather than through executemany:
# DuckDB opens a fresh column segment for every INSERT statement, so 78,585 single row inserts
# into NSE_FO's staging table exhausted a 4 GB memory limit before the file was half read.
# Measured, not theorised. One INSERT per batch keeps the whole file under a hundred megabytes.
_STAGE_SCHEMA = pa.schema(
    [
        pa.field("fytoken", pa.string()),
        pa.field("symbol_ticker", pa.string()),
        pa.field("exchange_code", pa.uint8()),
        pa.field("segment_code", pa.uint8()),
        pa.field("ex_instrument_type", pa.int32()),
        pa.field("under_symbol", pa.string()),
        pa.field("under_fytoken", pa.string()),
        pa.field("ex_series", pa.string()),
        pa.field("isin", pa.string()),
        pa.field("min_lot_size", pa.int32()),
        pa.field("tick_size", pa.decimal128(9, 4)),
        pa.field("qty_freeze", pa.int32()),
        pa.field("qty_multiplier", pa.decimal128(12, 4)),
        pa.field("face_value", pa.decimal128(12, 4)),
        pa.field("strike_price", pa.decimal128(12, 4)),
        pa.field("option_type", pa.string()),
        pa.field("expiry_date", pa.date32()),
        pa.field("trading_session", pa.string()),
        pa.field("symbol_details", pa.string()),
        pa.field("row_hash", pa.string()),
    ]
)

_STAGE_VIEW = "stg_instrument_master_batch"
_STAGE_INSERT = (
    f"INSERT INTO stg_instrument_master ({', '.join(STAGE_COLUMNS)}) "
    f"SELECT {', '.join(STAGE_COLUMNS)} FROM {_STAGE_VIEW}"
)


def stage_batch(rows: Sequence[MasterRow]) -> pa.RecordBatch:
    """Turn a batch of dimension rows into the pinned staging batch."""
    columns = [[getattr(row, name) for row in rows] for name in STAGE_COLUMNS]
    return pa.RecordBatch.from_arrays(
        [pa.array(values, type=field.type) for values, field in zip(columns, _STAGE_SCHEMA)],
        schema=_STAGE_SCHEMA,
    )

# A ticker whose value is not applicable comes back as the two letter filler rather than as null,
# and BSE sends an empty string where NSE sends the filler. Both mean the same thing and both must
# normalise to NULL, otherwise the two exchanges hash differently for the same fact and the vendor
# flipping between them opens a spurious version.
_NOT_APPLICABLE = frozenset({"", "XX", "NA", "N/A", "-"})

# Futures carry this sentinel in strikePrice.
_STRIKE_SENTINEL = Decimal("-1")


class SymbolMasterError(RuntimeError):
    """Base class for every failure this module raises."""


class MasterFileUnknown(SymbolMasterError):
    """A file name that is not one of the seven published masters."""


class MasterFileTruncated(SymbolMasterError):
    """The fetched file holds too few rows to be trusted against the previous snapshot.

    Carries both counts so the operator can see how far short it fell rather than guessing.
    """

    def __init__(self, file: str, staged: int, previous: int, guard: float) -> None:
        self.file = file
        self.staged = staged
        self.previous = previous
        self.guard = guard
        super().__init__(
            f"{file} returned {staged} rows against {previous} in the previous snapshot, "
            f"below the {guard:.0%} floor. Nothing was written. A short file cannot be told "
            "apart from a mass delisting, and acting on it would close live rows."
        )


def master_url(file: str) -> str:
    """The public URL of one master file."""
    name = file.strip().upper()
    if name not in FILE_EXCHANGE:
        raise MasterFileUnknown(f"unknown symbol master file {file!r}")
    return f"{MASTER_BASE_URL}{name}_sym_master.json"


def exchange_of(file: str) -> str:
    name = file.strip().upper()
    if name not in FILE_EXCHANGE:
        raise MasterFileUnknown(f"unknown symbol master file {file!r}")
    return FILE_EXCHANGE[name]


def segment_of(file: str) -> str:
    name = file.strip().upper()
    if name not in FILE_SEGMENT:
        raise MasterFileUnknown(f"unknown symbol master file {file!r}")
    return FILE_SEGMENT[name]


# ---------------------------------------------------------------------------
# Streaming the file
# ---------------------------------------------------------------------------


def iter_master_members(
    source: Path | str, *, chunk_bytes: int = READ_CHUNK_BYTES
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield one (ticker, attributes) pair at a time from a master file.

    The top level is a JSON object keyed by symTicker, so a full json.load would hold every
    member at once. This walks the object with the standard library decoder instead, keeping
    exactly one member alive, which is what makes an 81 MB file a bounded amount of memory.

    A parse failure raises json.JSONDecodeError with the real offset, which is the honest signal
    for a truncated download: a half a file is not a small file.
    """
    path = Path(source)
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as handle:
        buffer = ""
        base = 0  # characters already discarded from the front of buffer

        def refill() -> bool:
            nonlocal buffer
            chunk = handle.read(chunk_bytes)
            if not chunk:
                return False
            buffer += chunk
            return True

        def skip_space(pos: int) -> int:
            nonlocal buffer, base
            while True:
                while pos < len(buffer) and buffer[pos] in " \t\r\n":
                    pos += 1
                if pos < len(buffer):
                    return pos
                if not refill():
                    return pos

        def decode_at(pos: int) -> tuple[Any, int]:
            """raw_decode at pos, refilling until the value is complete."""
            nonlocal buffer
            while True:
                try:
                    return decoder.raw_decode(buffer, pos)
                except ValueError:
                    if not refill():
                        raise json.JSONDecodeError(
                            "truncated symbol master", buffer, min(pos, len(buffer))
                        ) from None

        refill()
        pos = skip_space(0)
        if pos >= len(buffer) or buffer[pos] != "{":
            raise json.JSONDecodeError(
                "symbol master must be a JSON object keyed by symbol ticker",
                buffer,
                min(pos, len(buffer)),
            )
        pos += 1
        pos = skip_space(pos)
        if pos < len(buffer) and buffer[pos] == "}":
            return

        while True:
            pos = skip_space(pos)
            key, pos = decode_at(pos)
            if not isinstance(key, str):
                raise json.JSONDecodeError("member key must be a string", buffer, pos)
            pos = skip_space(pos)
            if pos >= len(buffer) or buffer[pos] != ":":
                raise json.JSONDecodeError("expected member separator", buffer, pos)
            pos += 1
            pos = skip_space(pos)
            value, pos = decode_at(pos)
            if not isinstance(value, dict):
                raise json.JSONDecodeError("member value must be an object", buffer, pos)
            yield key, value

            # Drop what has been consumed so the buffer stays the size of one member plus one
            # read chunk rather than the size of the file.
            buffer = buffer[pos:]
            base += pos
            pos = 0

            pos = skip_space(pos)
            if pos >= len(buffer):
                raise json.JSONDecodeError("unterminated symbol master", buffer, pos)
            if buffer[pos] == ",":
                pos += 1
                continue
            if buffer[pos] == "}":
                return
            raise json.JSONDecodeError("expected , or } between members", buffer, pos)


# ---------------------------------------------------------------------------
# One row of the dimension
# ---------------------------------------------------------------------------


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _code(raw: Any) -> str | None:
    """A coded string field, with the vendor's not applicable fillers folded to NULL."""
    value = _text(raw)
    if value is None or value.upper() in _NOT_APPLICABLE:
        return None
    return value


def _int(raw: Any) -> int | None:
    value = _text(raw)
    if value is None:
        return None
    try:
        return int(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


def _dec(raw: Any, places: str = "0.0001") -> Decimal | None:
    value = _text(raw)
    if value is None:
        return None
    try:
        return Decimal(value).quantize(Decimal(places))
    except (InvalidOperation, ValueError):
        return None


def expiry_from_epoch(raw: Any) -> date | None:
    """The IST calendar date an epoch expiry timestamp falls on.

    Measured: NSE_FO sends 1790676600 for the September 2026 monthly, which is 15:40 IST on
    2026-09-29, the close of the derivative session that day. Reading it as a UTC date would
    give the right answer today and the wrong answer for any expiry whose session end crosses
    midnight UTC, so it is converted through the IST zone rather than truncated.
    """
    value = _text(raw)
    if value is None:
        return None
    try:
        seconds = int(Decimal(value))
    except (InvalidOperation, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).astimezone(IST).date()


@dataclass(frozen=True, slots=True)
class MasterRow:
    """One instrument as the dimension stores it, plus the hash that versions it."""

    fytoken: str
    symbol_ticker: str
    exchange_code: int | None
    segment_code: int | None
    ex_instrument_type: int | None
    under_symbol: str | None
    under_fytoken: str | None
    ex_series: str | None
    isin: str | None
    min_lot_size: int | None
    tick_size: Decimal | None
    qty_freeze: int | None
    qty_multiplier: Decimal | None
    face_value: Decimal | None
    strike_price: Decimal | None
    option_type: str | None
    expiry_date: date | None
    trading_session: str | None
    symbol_details: str | None
    row_hash: str

    def values(self) -> tuple[Any, ...]:
        return tuple(getattr(self, name) for name in SCD_COLUMNS) + (self.row_hash,)


def row_hash(values: Mapping[str, Any]) -> str:
    """The hash that decides whether an instrument opened a new version.

    Only the columns the dimension stores take part. Everything the vendor changes every session
    (previousClose, upperPrice, lowerPrice, tradeStatus, asmGsmVal) is deliberately absent from
    both the DDL and this hash, because including it would open a new version every single day
    for every single instrument and the table would stop being a dimension.
    """
    parts = []
    for name in SCD_COLUMNS:
        value = values.get(name)
        if value is None:
            parts.append("")
        elif isinstance(value, Decimal):
            parts.append(format(value, "f"))
        elif isinstance(value, date):
            parts.append(value.isoformat())
        else:
            parts.append(str(value))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def master_row(ticker: str, raw: Mapping[str, Any]) -> MasterRow:
    """Project one vendor member onto the dimension.

    The dict key and symTicker were identical in all 156,000 rows measured across the seven
    files, but the key is authoritative because it is what the rest of the system uses as a
    symbol, and a disagreement would mean the value block belongs to a different instrument.
    """
    symbol_ticker = _text(ticker) or _text(raw.get("symTicker")) or ""
    if not symbol_ticker:
        raise SymbolMasterError("symbol master member has no ticker")
    fytoken = _text(raw.get("fyToken"))
    if not fytoken:
        raise SymbolMasterError(f"symbol master member {symbol_ticker} has no fyToken")

    strike = _dec(raw.get("strikePrice"))
    if strike is not None and strike <= _STRIKE_SENTINEL:
        strike = None

    fields: dict[str, Any] = {
        "fytoken": fytoken,
        "symbol_ticker": symbol_ticker,
        "exchange_code": _int(raw.get("exchange")),
        "segment_code": _int(raw.get("segment")),
        "ex_instrument_type": _int(raw.get("exInstType")),
        "under_symbol": _text(raw.get("underSym")),
        "under_fytoken": _text(raw.get("underFyTok")),
        "ex_series": _code(raw.get("exSeries")),
        "isin": _code(raw.get("isin")),
        "min_lot_size": _int(raw.get("minLotSize")),
        "tick_size": _dec(raw.get("tickSize")),
        "qty_freeze": _int(raw.get("qtyFreeze")),
        "qty_multiplier": _dec(raw.get("qtyMultiplier")),
        "face_value": _dec(raw.get("faceValue")),
        "strike_price": strike,
        "option_type": _code(raw.get("optType")),
        "expiry_date": expiry_from_epoch(raw.get("expiryDate")),
        "trading_session": _text(raw.get("tradingSession")),
        "symbol_details": _text(raw.get("symDetails")),
    }
    return MasterRow(**fields, row_hash=row_hash(fields))


def iter_master_rows(
    source: Path | str, *, chunk_bytes: int = READ_CHUNK_BYTES
) -> Iterator[MasterRow]:
    """Stream a master file as dimension rows."""
    for ticker, raw in iter_master_members(source, chunk_bytes=chunk_bytes):
        yield master_row(ticker, raw)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchedMaster:
    """One downloaded master file on local disk."""

    file: str
    url: str
    path: Path
    sha256: str
    byte_size: int
    fetched_at: datetime
    etag: str | None = None
    last_modified: str | None = None


def _public_client(timeout: float) -> httpx.AsyncClient:
    """A client for the public files.

    Deliberately not brokers.fyers.client.FyersClient and deliberately not routed through the
    governor. These objects are static files on a CDN, they carry no Authorization header, and
    they are not part of the plan's request budget. Putting them behind the governor would spend
    seven slots a day of a budget whose fourth violation costs the rest of the day, and would
    make the one job that must survive a dead token depend on a token.
    """
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip"},
    )


async def fetch_master(
    file: str,
    *,
    dest_dir: Path,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    now: Callable[[], datetime] | None = None,
) -> FetchedMaster:
    """Download one master file to dest_dir, hashing it as it streams.

    The body is never held in memory: it goes to disk chunk by chunk while sha256 and the byte
    count accumulate, so an 81 MB file costs one read buffer.
    """
    name = file.strip().upper()
    url = master_url(name)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{name}_sym_master.json"
    staging = target.with_suffix(".json.part")

    owned = client is None
    active = client or _public_client(timeout)
    digest = hashlib.sha256()
    size = 0
    headers: httpx.Headers | None = None
    try:
        async with active.stream("GET", url) as response:
            response.raise_for_status()
            headers = response.headers
            with staging.open("wb") as handle:
                async for chunk in response.aiter_bytes(READ_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
    finally:
        if owned:
            await active.aclose()

    # Rename only once the whole body is on disk, so a partial download can never be read as a
    # short file and trip the truncation guard into a false alarm.
    staging.replace(target)
    stamp = (now() if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)
    return FetchedMaster(
        file=name,
        url=url,
        path=target,
        sha256=digest.hexdigest(),
        byte_size=size,
        fetched_at=stamp,
        etag=None if headers is None else headers.get("etag"),
        last_modified=None if headers is None else headers.get("last-modified"),
    )


# ---------------------------------------------------------------------------
# The type 2 diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileResult:
    """What one file's diff did to the dimension."""

    file: str
    as_of: date
    staged: int
    inserted: int
    versioned: int
    closed: int
    amended: int
    unchanged: int
    sha256: str
    byte_size: int
    skipped: bool = False
    previous_sha256: str | None = None

    @property
    def new_rows(self) -> int:
        """Rows the dimension gained. A quiet reload must answer zero."""
        return self.inserted + self.versioned


@dataclass(frozen=True, slots=True)
class MetaWrite:
    """Set one meta key. The escape hatch used for the observed root character class."""

    key: str
    value: str
    label: str = "meta.set"

    def apply(self, cur: "duckdb.DuckDBPyConnection") -> None:
        cur.execute("BEGIN")
        try:
            cur.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [self.key, self.value])
        except Exception:
            cur.execute("ROLLBACK")
            raise
        cur.execute("COMMIT")


def _snapshot_values(
    as_of: date, file: str, url: str, sha256: str, row_count: int, byte_size: int, at: datetime
) -> list[Any]:
    stamp = at if at.tzinfo is None else at.astimezone(timezone.utc).replace(tzinfo=None)
    return [as_of, file, url, sha256, row_count, byte_size, stamp]


_SNAPSHOT_INSERT = (
    "INSERT OR REPLACE INTO symbol_master_snapshot "
    "(snapshot_date, file, url, sha256, row_count, byte_size, fetched_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True, slots=True)
class SnapshotWrite:
    """The snapshot row on its own, for a file whose content had not moved."""

    fetched: FetchedMaster
    as_of: date
    row_count: int
    label: str = "symbol_master.snapshot"

    def apply(self, cur: "duckdb.DuckDBPyConnection") -> None:
        cur.execute("BEGIN")
        try:
            cur.execute(
                _SNAPSHOT_INSERT,
                _snapshot_values(
                    self.as_of,
                    self.fetched.file,
                    self.fetched.url,
                    self.fetched.sha256,
                    self.row_count,
                    self.fetched.byte_size,
                    self.fetched.fetched_at,
                ),
            )
        except Exception:
            cur.execute("ROLLBACK")
            raise
        cur.execute("COMMIT")


@dataclass(frozen=True, slots=True)
class SymbolMasterDiffWrite:
    """One file, staged and diffed into dim_instrument_master inside one transaction.

    Everything happens on the writer cursor because the whole point of the single writer is that
    no second connection can interleave a half applied dimension. The staging fill is inside the
    transaction too: a diff that read a staging table filled by an earlier committed statement
    could see a partially replaced file if the process died between the two.
    """

    file: str
    source: Path
    as_of: date
    sha256: str
    byte_size: int
    url: str
    fetched_at: datetime
    batch_size: int = DEFAULT_STAGE_BATCH
    shrink_guard: float = DEFAULT_SHRINK_GUARD
    rows: Sequence[MasterRow] | None = None
    label: str = "symbol_master.diff"

    def _iter_rows(self) -> Iterable[MasterRow]:
        if self.rows is not None:
            return self.rows
        return iter_master_rows(self.source)

    def _stage(self, cur: "duckdb.DuckDBPyConnection") -> int:
        cur.execute(_STAGING_DDL)
        staged = 0
        batch: list[MasterRow] = []

        def flush() -> None:
            nonlocal staged
            if not batch:
                return
            record = stage_batch(batch)
            cur.register(_STAGE_VIEW, pa.Table.from_batches([record], schema=_STAGE_SCHEMA))
            cur.execute(_STAGE_INSERT)
            staged += len(batch)
            batch.clear()

        try:
            for row in self._iter_rows():
                batch.append(row)
                if len(batch) >= self.batch_size:
                    flush()
            flush()
        finally:
            cur.unregister(_STAGE_VIEW)
        return staged

    def apply(self, cur: "duckdb.DuckDBPyConnection") -> FileResult:
        cur.execute("BEGIN")
        try:
            result = self._apply(cur)
        except Exception:
            cur.execute("ROLLBACK")
            cur.execute("DROP TABLE IF EXISTS stg_instrument_master")
            raise
        cur.execute("COMMIT")
        cur.execute("DROP TABLE IF EXISTS stg_instrument_master")
        return result

    def _apply(self, cur: "duckdb.DuckDBPyConnection") -> FileResult:
        staged = self._stage(cur)

        previous = cur.execute(
            "SELECT row_count FROM symbol_master_snapshot WHERE file = ? "
            "ORDER BY snapshot_date DESC LIMIT 1",
            [self.file],
        ).fetchone()
        previous_rows = int(previous[0]) if previous else 0
        if previous_rows and staged < previous_rows * self.shrink_guard:
            raise MasterFileTruncated(self.file, staged, previous_rows, self.shrink_guard)

        # The slice of the dimension this file owns. Taken from the staged rows rather than from
        # a table of constants, so a file that starts carrying a second segment cannot silently
        # close the rows of a segment it no longer covers.
        scope = [
            (int(a), int(b))
            for a, b in cur.execute(
                "SELECT DISTINCT exchange_code, segment_code FROM stg_instrument_master "
                "WHERE exchange_code IS NOT NULL AND segment_code IS NOT NULL"
            ).fetchall()
        ]

        # 1. Same day amendment. An instrument whose open row was already opened today is
        # corrected in place instead of versioned, because a zero width row (valid_from equal to
        # valid_to) answers no point in time query and would collide on the primary key.
        set_clause = ", ".join(f"{c} = s.{c}" for c in SCD_COLUMNS if c != "fytoken")
        amended = _changes(
            cur,
            f"""
            UPDATE dim_instrument_master AS d
               SET {set_clause}, row_hash = s.row_hash
              FROM stg_instrument_master AS s
             WHERE d.fytoken = s.fytoken
               AND d.valid_to IS NULL
               AND d.valid_from = ?
               AND d.row_hash <> s.row_hash
            """,
            [self.as_of],
        )

        # 2. Close the version that a tracked attribute changed out from under.
        versioned = _changes(
            cur,
            """
            UPDATE dim_instrument_master AS d
               SET valid_to = ?
              FROM stg_instrument_master AS s
             WHERE d.fytoken = s.fytoken
               AND d.valid_to IS NULL
               AND d.valid_from < ?
               AND d.row_hash <> s.row_hash
            """,
            [self.as_of, self.as_of],
        )

        # 3. Close what the file no longer carries. This is the expiry path: a contract that has
        # expired is gone from the master, and its row must stop being current on the day it
        # left rather than staying open forever.
        closed = 0
        if scope:
            pairs = ", ".join("(?, ?)" for _ in scope)
            params: list[Any] = [self.as_of, self.as_of]
            for exchange_code, segment_code in scope:
                params.extend((exchange_code, segment_code))
            closed = _changes(
                cur,
                f"""
                UPDATE dim_instrument_master AS d
                   SET valid_to = ?
                 WHERE d.valid_to IS NULL
                   AND d.valid_from < ?
                   AND (d.exchange_code, d.segment_code) IN ({pairs})
                   AND NOT EXISTS (
                         SELECT 1 FROM stg_instrument_master AS s WHERE s.fytoken = d.fytoken
                       )
                """,
                params,
            )

        # 4. Open a version for anything staged that has no current row: a genuinely new
        # instrument, the second half of a change closed at step 2, or a relisting whose previous
        # row was closed on an earlier day. The conflict clause covers the one remaining collision,
        # a relisting on the same date the old version was closed.
        columns = ", ".join(SCD_COLUMNS)
        conflict = ", ".join(f"{c} = excluded.{c}" for c in SCD_COLUMNS if c != "fytoken")
        opened = _changes(
            cur,
            f"""
            INSERT INTO dim_instrument_master ({columns}, valid_from, valid_to, row_hash)
            SELECT {", ".join(f"s.{c}" for c in SCD_COLUMNS)}, ?, NULL, s.row_hash
              FROM stg_instrument_master AS s
             WHERE NOT EXISTS (
                     SELECT 1 FROM dim_instrument_master AS d
                      WHERE d.fytoken = s.fytoken AND d.valid_to IS NULL
                   )
            ON CONFLICT (fytoken, valid_from) DO UPDATE
               SET {conflict}, valid_to = NULL, row_hash = excluded.row_hash
            """,
            [self.as_of],
        )
        inserted = max(opened - versioned, 0)

        cur.execute(
            _SNAPSHOT_INSERT,
            _snapshot_values(
                self.as_of,
                self.file,
                self.url,
                self.sha256,
                staged,
                self.byte_size,
                self.fetched_at,
            ),
        )

        return FileResult(
            file=self.file,
            as_of=self.as_of,
            staged=staged,
            inserted=inserted,
            versioned=versioned,
            closed=closed,
            amended=amended,
            unchanged=staged - inserted - versioned - amended,
            sha256=self.sha256,
            byte_size=self.byte_size,
        )


def _changes(cur: "duckdb.DuckDBPyConnection", sql: str, params: Sequence[Any]) -> int:
    """Run a DML statement and return the row count DuckDB reports."""
    row = cur.execute(sql, list(params)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def apply_master_file(
    writer: "DuckWriter",
    fetched: FetchedMaster,
    *,
    as_of: date,
    batch_size: int = DEFAULT_STAGE_BATCH,
    shrink_guard: float = DEFAULT_SHRINK_GUARD,
    rows: Sequence[MasterRow] | None = None,
) -> FileResult:
    """Stage and diff one fetched file through the single writer."""
    return await writer.submit(
        SymbolMasterDiffWrite(
            file=fetched.file,
            source=fetched.path,
            as_of=as_of,
            sha256=fetched.sha256,
            byte_size=fetched.byte_size,
            url=fetched.url,
            fetched_at=fetched.fetched_at,
            batch_size=batch_size,
            shrink_guard=shrink_guard,
            rows=rows,
        )
    )


# ---------------------------------------------------------------------------
# Reading the dimension
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MasterFact:
    """The dated sizing facts for one instrument."""

    fytoken: str
    symbol_ticker: str
    min_lot_size: int | None
    tick_size: Decimal | None
    qty_freeze: int | None
    qty_multiplier: Decimal | None
    valid_from: date
    valid_to: date | None


def point_in_time_sql(where: str = "fytoken = ?") -> str:
    """The version resolving predicate, as DATA-MODEL section 3.6 states it.

    valid_to is exclusive, so a row closed on the day being asked about no longer answers for
    that day: the version that replaced it does. That is what makes a change and the day it took
    effect the same fact.
    """
    return (
        "SELECT fytoken, symbol_ticker, min_lot_size, tick_size, qty_freeze, qty_multiplier, "
        "valid_from, valid_to FROM dim_instrument_master "
        f"WHERE {where} AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
    )


def _fact(row: Sequence[Any]) -> MasterFact:
    return MasterFact(
        fytoken=row[0],
        symbol_ticker=row[1],
        min_lot_size=None if row[2] is None else int(row[2]),
        tick_size=None if row[3] is None else Decimal(str(row[3])),
        qty_freeze=None if row[4] is None else int(row[4]),
        qty_multiplier=None if row[5] is None else Decimal(str(row[5])),
        valid_from=row[6],
        valid_to=row[7],
    )


async def fact_on(reader: "DuckReader", fytoken: str, on: date) -> MasterFact | None:
    """The lot size and tick size that were true for this instrument on a given date."""
    row = await reader.fetch_one(point_in_time_sql(), [fytoken, on, on])
    return None if row is None else _fact(row)


async def facts_on(
    reader: "DuckReader", fytokens: Sequence[str], on: date
) -> dict[str, MasterFact]:
    """The same lookup for many instruments at once."""
    if not fytokens:
        return {}
    placeholders = ", ".join("?" for _ in fytokens)
    sql = point_in_time_sql(f"fytoken IN ({placeholders})")
    rows = await reader.fetch_all(sql, [*fytokens, on, on])
    return {row[0]: _fact(row) for row in rows}


async def last_snapshot(reader: "DuckReader", file: str) -> tuple[date, str, int] | None:
    """The most recent (snapshot_date, sha256, row_count) recorded for one file."""
    row = await reader.fetch_one(
        "SELECT snapshot_date, sha256, row_count FROM symbol_master_snapshot "
        "WHERE file = ? ORDER BY snapshot_date DESC LIMIT 1",
        [file],
    )
    return None if row is None else (row[0], row[1], int(row[2]))


# ---------------------------------------------------------------------------
# The root registry rebuild
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RootCandidate:
    """One derivative root, resolved to the cash ticker the expired endpoints accept."""

    root: str
    fyers_symbol: str
    display_name: str
    exchange: str
    instrument_kind: str
    derivative_segment: str
    under_fytoken: str
    contract_count: int

    def as_underlying_root(self) -> UnderlyingRoot:
        return UnderlyingRoot(
            root=self.root,
            fyers_symbol=self.fyers_symbol,
            display_name=self.display_name,
            exchange=self.exchange,
            instrument_kind=self.instrument_kind,
            derivative_segment=self.derivative_segment,
            data_from=exchange_data_floor(self.exchange),
        )


@dataclass(frozen=True, slots=True)
class RejectedRoot:
    """A root the registry refused, and why."""

    root: str
    exchange: str
    reason: str
    contract_count: int = 0


@dataclass(frozen=True, slots=True)
class ObservedCharClass:
    """The character class the vendor actually uses for derivative roots.

    BUILD-PLAN leaves the hyphenated root question open. This answers it from the file rather
    than from an assumption, and records the answer in meta so the next person reads a measurement
    instead of re-deriving it.
    """

    source_file: str
    root_count: int
    first_chars: str
    body_chars: str
    pattern: str
    examples: tuple[str, ...]
    outliers: tuple[str, ...]
    accepted_pattern: str
    observed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "root_count": self.root_count,
            "first_chars": self.first_chars,
            "body_chars": self.body_chars,
            "pattern": self.pattern,
            "examples": list(self.examples),
            "outliers": list(self.outliers),
            "accepted_pattern": self.accepted_pattern,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class RootRebuild:
    """The rebuilt registry plus everything that did not make it in."""

    registry: RootRegistry
    candidates: tuple[RootCandidate, ...]
    rejected: tuple[RejectedRoot, ...]
    char_class: ObservedCharClass | None
    added: tuple[str, ...] = field(default=())


# The class roots.py and symbology.py will accept. Kept here as a string rather than imported so
# that a change on either side shows up as a recorded mismatch instead of a silent agreement.
ACCEPTED_ROOT_PATTERN = r"[A-Z][A-Z0-9&_.\-]*"
_ACCEPTED_ROOT_RE = re.compile(ACCEPTED_ROOT_PATTERN)


def observed_root_charclass(
    roots: Iterable[str],
    *,
    source_file: str = "NSE_FO",
    observed_at: str | None = None,
) -> ObservedCharClass:
    """Derive the root character class from the roots a file actually carries."""
    values = sorted({r.strip().upper() for r in roots if r and r.strip()})
    first = sorted({r[0] for r in values})
    body = sorted({c for r in values for c in r[1:]})
    outliers = tuple(r for r in values if not _ACCEPTED_ROOT_RE.fullmatch(r))
    examples = tuple(r for r in values if not r.isalnum())[:8]
    return ObservedCharClass(
        source_file=source_file,
        root_count=len(values),
        first_chars="".join(first),
        body_chars="".join(body),
        pattern=f"[{_class_body(first)}][{_class_body(body)}]*",
        examples=examples,
        outliers=outliers,
        accepted_pattern=ACCEPTED_ROOT_PATTERN,
        observed_at=observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _class_body(chars: Sequence[str]) -> str:
    """Collapse a character set into a readable class body, ranges and all."""
    letters = [c for c in chars if c.isalpha()]
    digits = [c for c in chars if c.isdigit()]
    other = [c for c in chars if not c.isalnum()]
    parts = []
    if letters:
        parts.append("A-Z" if len(letters) > 4 else "".join(letters))
    if digits:
        parts.append("0-9" if len(digits) > 4 else "".join(digits))
    for symbol in other:
        parts.append("\\-" if symbol == "-" else symbol)
    return "".join(parts)


_KIND_BY_INSTRUMENT_TYPE: Mapping[int, str] = {10: "INDEX"}


def _instrument_kind(symbol_ticker: str, ex_instrument_type: int | None) -> str:
    """INDEX or EQUITY for a cash row.

    The ticker suffix is authoritative because it is what the caller sends to the expired
    endpoints; exInstType 10 is the corroborating signal and only decides an unsuffixed row.
    """
    upper = symbol_ticker.upper()
    if upper.endswith("-INDEX"):
        return "INDEX"
    if ex_instrument_type is not None and _KIND_BY_INSTRUMENT_TYPE.get(ex_instrument_type):
        return _KIND_BY_INSTRUMENT_TYPE[ex_instrument_type]
    return "EQUITY"


# symDetails on a cash equity row is the company name, which is a good display name. On an index
# row it is the literal word INDEX for every index the exchange lists, which is not a name at all,
# so the root is the better label there.
_DEGENERATE_NAMES = frozenset({"INDEX", "IDX", "EQ", "XX"})


def _display_name(root: str, symbol_details: Any) -> str:
    value = _text(symbol_details)
    if value is None or value.upper() in _DEGENERATE_NAMES:
        return root
    return value


_SEGMENT_BY_CODE: Mapping[int, str] = {code: name for name, code in SEGMENT_CODES.items()}
_EXCHANGE_BY_CODE: Mapping[int, str] = {code: name for name, code in EXCHANGE_CODES.items()}


def build_root_candidates(
    derivative_rows: Iterable[Mapping[str, Any]],
    cash_rows: Iterable[Mapping[str, Any]],
) -> tuple[tuple[RootCandidate, ...], tuple[RejectedRoot, ...]]:
    """Group derivative rows by root and resolve each one to its cash ticker.

    Measured on 2026-09-10, all 216 NSE_FO roots and all 17 BSE_FO roots resolved through
    under_fytoken into the matching cash file, which is why under_fytoken is the join and not the
    root string. Nothing in a derivative symbol names its cash ticker: BANKNIFTY is quoted as
    NSE:NIFTYBANK-INDEX, and only this join knows that.
    """
    cash: dict[str, Mapping[str, Any]] = {}
    for row in cash_rows:
        token = row.get("fytoken")
        if token:
            cash[str(token)] = row

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in derivative_rows:
        root = (row.get("under_symbol") or "").strip().upper()
        under = row.get("under_fytoken")
        exchange_code = row.get("exchange_code")
        exchange = _EXCHANGE_BY_CODE.get(int(exchange_code)) if exchange_code is not None else None
        if not root or not exchange:
            continue
        key = (exchange, root)
        bucket = grouped.setdefault(
            key,
            {
                "count": 0,
                "under_fytoken": str(under) if under else None,
                "segment_code": row.get("segment_code"),
            },
        )
        bucket["count"] += int(row.get("contract_count") or 1)
        if bucket["under_fytoken"] is None and under:
            bucket["under_fytoken"] = str(under)

    candidates: list[RootCandidate] = []
    rejected: list[RejectedRoot] = []
    for (exchange, root), bucket in sorted(grouped.items()):
        token = bucket["under_fytoken"]
        cash_row = cash.get(token) if token else None
        if cash_row is None:
            rejected.append(
                RejectedRoot(root, exchange, "no cash row for under_fytoken", bucket["count"])
            )
            continue
        segment_code = bucket["segment_code"]
        segment = (
            _SEGMENT_BY_CODE.get(int(segment_code)) if segment_code is not None else None
        ) or "FO"
        ticker = str(cash_row.get("symbol_ticker") or "")
        candidate = RootCandidate(
            root=root,
            fyers_symbol=ticker,
            display_name=_display_name(root, cash_row.get("symbol_details")),
            exchange=exchange,
            instrument_kind=_instrument_kind(ticker, cash_row.get("ex_instrument_type")),
            derivative_segment=segment,
            under_fytoken=str(token),
            contract_count=int(bucket["count"]),
        )
        try:
            candidate.as_underlying_root()
        except ValueError as exc:
            # The one measured case is 360ONE, whose root starts with a digit while both
            # roots.py and symbology.py require a leading letter. Recorded rather than raised,
            # because one unparseable root must not cost the other 215.
            rejected.append(RejectedRoot(root, exchange, str(exc), candidate.contract_count))
            continue
        candidates.append(candidate)
    return tuple(candidates), tuple(rejected)


_DERIVATIVE_ROOT_SQL = """
SELECT under_symbol, any_value(under_fytoken) AS under_fytoken,
       any_value(exchange_code) AS exchange_code, any_value(segment_code) AS segment_code,
       count(*) AS contract_count
  FROM dim_instrument_master
 WHERE valid_to IS NULL AND segment_code = ? AND exchange_code = ?
   AND under_symbol IS NOT NULL AND under_fytoken IS NOT NULL
 GROUP BY under_symbol
"""

_CASH_ROW_SQL = """
SELECT fytoken, symbol_ticker, ex_instrument_type, symbol_details
  FROM dim_instrument_master
 WHERE valid_to IS NULL AND segment_code = ? AND exchange_code = ?
"""


async def rebuild_root_registry(
    reader: "DuckReader",
    *,
    base: RootRegistry | None = None,
    exchanges: Sequence[str] = ("NSE", "BSE"),
    char_class_file: str = "NSE_FO",
) -> RootRebuild:
    """Rebuild the root registry from the current dimension.

    It reads the dimension rather than the downloaded files, so a file whose sha256 matched
    yesterday and was therefore not re-diffed still contributes its roots.

    MCX is not in the default exchange list. The 2026-09-09 probe run found every MCX form
    answering 422 on the expired endpoints while BSE:SENSEX-INDEX answered 200 in the same run,
    so an MCX root offered as an underlying would be an underlying that can never be downloaded.
    """
    base_registry = base if base is not None else builtin_registry()
    registry = RootRegistry(base_registry)

    candidates: list[RootCandidate] = []
    rejected: list[RejectedRoot] = []
    char_class: ObservedCharClass | None = None

    for exchange in exchanges:
        exchange_code = EXCHANGE_CODES.get(exchange.strip().upper())
        if exchange_code is None:
            continue
        derivative = await reader.fetch_all(
            _DERIVATIVE_ROOT_SQL, [SEGMENT_CODES["FO"], exchange_code]
        )
        cash = await reader.fetch_all(_CASH_ROW_SQL, [SEGMENT_CODES["CM"], exchange_code])
        derivative_rows = [
            {
                "under_symbol": row[0],
                "under_fytoken": row[1],
                "exchange_code": row[2],
                "segment_code": row[3],
                "contract_count": row[4],
            }
            for row in derivative
        ]
        cash_rows = [
            {
                "fytoken": row[0],
                "symbol_ticker": row[1],
                "ex_instrument_type": row[2],
                "symbol_details": row[3],
            }
            for row in cash
        ]
        found, refused = build_root_candidates(derivative_rows, cash_rows)
        candidates.extend(found)
        rejected.extend(refused)
        if f"{exchange.strip().upper()}_FO" == char_class_file.strip().upper():
            char_class = observed_root_charclass(
                (row["under_symbol"] for row in derivative_rows), source_file=char_class_file
            )

    added: list[str] = []
    for candidate in candidates:
        entry = candidate.as_underlying_root()
        existing = registry.get(candidate.root)
        if existing is None:
            registry.register(entry)
            added.append(candidate.root)
        elif not existing.is_builtin and existing.fyers_symbol != entry.fyers_symbol:
            # A builtin seed is never overwritten from the master: its cash ticker is the one
            # the expired endpoints were probed against. Anything else follows the vendor.
            registry.register(entry, replace_existing=True)

    return RootRebuild(
        registry=registry,
        candidates=tuple(candidates),
        rejected=tuple(rejected),
        char_class=char_class,
        added=tuple(added),
    )


# ---------------------------------------------------------------------------
# Cross checking the parser against the vendor's own fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbologyDisagreement:
    """One symbol where the parser and the vendor's own fields do not agree."""

    symbol_ticker: str
    field_name: str
    parsed: str | None
    vendor: str | None


@dataclass(frozen=True, slots=True)
class SymbologyAudit:
    """What symbology.parse_symbol made of a representative contract per root.

    The master is the one place in this system where a symbol string arrives alongside the
    vendor's own decomposition of it, so it is the only place the parser can be marked against
    an answer key. One contract per root rather than all 78,585 because a root the parser cannot
    handle costs its whole family, and a root it can handle it handles for every strike.
    """

    checked: int
    unparseable: tuple[str, ...]
    disagreements: tuple[SymbologyDisagreement, ...]
    observed_at: str

    @property
    def clean(self) -> bool:
        return not self.unparseable and not self.disagreements

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "unparseable": list(self.unparseable),
            "disagreements": [
                {
                    "symbol_ticker": d.symbol_ticker,
                    "field": d.field_name,
                    "parsed": d.parsed,
                    "vendor": d.vendor,
                }
                for d in self.disagreements
            ],
            "observed_at": self.observed_at,
        }


_AUDIT_SAMPLE_SQL = """
SELECT under_symbol, any_value(symbol_ticker), any_value(strike_price),
       any_value(option_type), any_value(expiry_date)
  FROM dim_instrument_master
 WHERE valid_to IS NULL AND segment_code = ? AND option_type IS NOT NULL
   AND under_symbol IS NOT NULL
 GROUP BY under_symbol
"""


def audit_rows_against_symbology(
    rows: Iterable[Sequence[Any]],
    registry: RootRegistry,
    *,
    observed_at: str | None = None,
) -> SymbologyAudit:
    """Parse one option per root and compare the result with the vendor's own fields."""
    unparseable: list[str] = []
    disagreements: list[SymbologyDisagreement] = []
    checked = 0
    for under_symbol, ticker, strike, option_type, expiry in rows:
        checked += 1
        try:
            parsed = parse_symbol(ticker, registry=registry, expected_expiry=expiry)
        except Exception:
            unparseable.append(ticker)
            continue
        if parsed.root != (under_symbol or "").strip().upper():
            disagreements.append(
                SymbologyDisagreement(ticker, "root", parsed.root, under_symbol)
            )
        if parsed.option_type != option_type:
            disagreements.append(
                SymbologyDisagreement(ticker, "option_type", parsed.option_type, option_type)
            )
        if strike is not None and parsed.strike is not None:
            if Decimal(str(parsed.strike)) != Decimal(str(strike)):
                disagreements.append(
                    SymbologyDisagreement(
                        ticker, "strike", str(parsed.strike), str(strike)
                    )
                )
    return SymbologyAudit(
        checked=checked,
        unparseable=tuple(unparseable),
        disagreements=tuple(disagreements),
        observed_at=observed_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


async def audit_symbology(reader: "DuckReader", registry: RootRegistry) -> SymbologyAudit:
    """Run the parser over one option per root in the current dimension."""
    rows = await reader.fetch_all(_AUDIT_SAMPLE_SQL, [SEGMENT_CODES["FO"]])
    return audit_rows_against_symbology(rows, registry)


def feed_default_registry(rebuild: RootRebuild) -> tuple[str, ...]:
    """Push the rebuilt roots into the process wide parser registry.

    This is the "feeding roots.py" half of the deliverable, done by registering into
    roots.default_registry() rather than by editing roots.py, which this work item does not own.
    It is not called automatically by the daily run: knowing more real roots strictly improves
    weekly split disambiguation, but it is a process wide side effect and the caller should be
    the one that decides to take it.
    """
    registry = default_registry()
    added: list[str] = []
    for candidate in rebuild.candidates:
        existing = registry.get(candidate.root)
        if existing is not None and existing.is_builtin:
            continue
        registry.register(candidate.as_underlying_root(), replace_existing=True)
        if existing is None:
            added.append(candidate.root)
    return tuple(added)


# ---------------------------------------------------------------------------
# The daily entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunResult:
    """One symbol_master job run."""

    as_of: date
    files: tuple[FileResult, ...]
    failures: tuple[tuple[str, str], ...]
    rebuild: RootRebuild | None
    started_at: datetime
    finished_at: datetime
    audit: SymbologyAudit | None = None

    @property
    def rows_staged(self) -> int:
        return sum(f.staged for f in self.files)

    @property
    def new_rows(self) -> int:
        return sum(f.new_rows for f in self.files)

    @property
    def closed_rows(self) -> int:
        return sum(f.closed + f.versioned for f in self.files)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds"),
            "files": [
                {
                    "file": f.file,
                    "staged": f.staged,
                    "inserted": f.inserted,
                    "versioned": f.versioned,
                    "closed": f.closed,
                    "amended": f.amended,
                    "unchanged": f.unchanged,
                    "skipped": f.skipped,
                    "sha256": f.sha256,
                    "byte_size": f.byte_size,
                }
                for f in self.files
            ],
            "failures": [{"file": name, "error": text} for name, text in self.failures],
            "roots_added": list(self.rebuild.added) if self.rebuild else [],
            "roots_rejected": (
                [
                    {"root": r.root, "exchange": r.exchange, "reason": r.reason}
                    for r in self.rebuild.rejected
                ]
                if self.rebuild
                else []
            ),
            "symbology_audit": None if self.audit is None else self.audit.as_dict(),
        }


# CM before FO inside each exchange so the root rebuild joins against cash rows that were
# refreshed in the same run. The two files with no cash counterpart go last.
DEFAULT_RUN_ORDER: tuple[str, ...] = (
    "NSE_CM",
    "NSE_FO",
    "BSE_CM",
    "BSE_FO",
    "NSE_CD",
    "NSE_COM",
    "MCX_COM",
)


async def run_symbol_master_snapshot(
    *,
    writer: "DuckWriter",
    reader: "DuckReader",
    work_dir: Path,
    files: Sequence[str] = DEFAULT_RUN_ORDER,
    as_of: date | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    skip_unchanged: bool = True,
    keep_files: bool = False,
    rebuild_roots: bool = True,
    audit_parser: bool = True,
    base_registry: RootRegistry | None = None,
    batch_size: int = DEFAULT_STAGE_BATCH,
    shrink_guard: float = DEFAULT_SHRINK_GUARD,
    now: Callable[[], datetime] | None = None,
) -> RunResult:
    """The daily snapshot, which is what the scheduler and the symbol_master task both call.

    One file failing does not stop the others. Every file is an independent slice of the
    dimension, and a partial snapshot of six files is worth strictly more than no snapshot at
    all, because the day cannot be recaptured later.

    skip_unchanged compares the fetched sha256 against the previous snapshot for that file and
    skips the diff when they match. It is only an optimisation: running the diff on identical
    content produces no new rows either way, which the tests assert directly.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    started = clock().astimezone(timezone.utc)
    day = as_of or datetime.now(IST).date()
    work_dir = Path(work_dir)

    owned = client is None
    active = client or _public_client(timeout)
    results: list[FileResult] = []
    failures: list[tuple[str, str]] = []
    try:
        for name in files:
            file = name.strip().upper()
            try:
                fetched = await fetch_master(
                    file, dest_dir=work_dir, client=active, timeout=timeout, now=clock
                )
            except Exception as exc:  # one bad file must not cost the other six
                log.warning("symbol master fetch failed", extra={"file": file, "error": str(exc)})
                failures.append((file, f"{type(exc).__name__}: {exc}"))
                continue
            try:
                previous = await last_snapshot(reader, file)
                if skip_unchanged and previous is not None and previous[1] == fetched.sha256:
                    results.append(
                        FileResult(
                            file=file,
                            as_of=day,
                            staged=previous[2],
                            inserted=0,
                            versioned=0,
                            closed=0,
                            amended=0,
                            unchanged=previous[2],
                            sha256=fetched.sha256,
                            byte_size=fetched.byte_size,
                            skipped=True,
                            previous_sha256=previous[1],
                        )
                    )
                    await writer.submit(_snapshot_only(fetched, day, previous[2]))
                    continue
                result = await apply_master_file(
                    writer,
                    fetched,
                    as_of=day,
                    batch_size=batch_size,
                    shrink_guard=shrink_guard,
                )
                results.append(
                    replace(result, previous_sha256=None if previous is None else previous[1])
                )
            except Exception as exc:
                log.warning("symbol master diff failed", extra={"file": file, "error": str(exc)})
                failures.append((file, f"{type(exc).__name__}: {exc}"))
            finally:
                if not keep_files:
                    fetched.path.unlink(missing_ok=True)
    finally:
        if owned:
            await active.aclose()

    rebuild: RootRebuild | None = None
    audit: SymbologyAudit | None = None
    if rebuild_roots:
        rebuild = await rebuild_root_registry(reader, base=base_registry)
        if rebuild.char_class is not None:
            await writer.submit(
                MetaWrite(
                    META_ROOT_CHARCLASS,
                    json.dumps(rebuild.char_class.as_dict(), separators=(",", ":")),
                )
            )
            if rebuild.char_class.outliers:
                # Visible rather than silent. A root the parser refuses is a whole family of
                # contracts this system can never download, and it must not be discovered later
                # as a mysterious gap in the coverage grid.
                log.warning(
                    "symbol master roots outside the accepted character class",
                    extra={
                        "roots": list(rebuild.char_class.outliers),
                        "pattern": rebuild.char_class.pattern,
                        "accepted": rebuild.char_class.accepted_pattern,
                    },
                )

        # The master is the only place a symbol string arrives next to the vendor's own
        # decomposition of it, so it is the only place symbology.parse_symbol can be marked
        # against an answer key. One option per root: a root the parser cannot read costs its
        # whole family, and a root it can read it reads at every strike.
        if audit_parser:
            audit = await audit_symbology(reader, rebuild.registry)
            await writer.submit(
                MetaWrite(
                    META_SYMBOLOGY_AUDIT,
                    json.dumps(audit.as_dict(), separators=(",", ":")),
                )
            )
            if not audit.clean:
                log.warning(
                    "symbol master parser audit found symbols the parser cannot confirm",
                    extra={
                        "checked": audit.checked,
                        "unparseable": list(audit.unparseable),
                        "disagreements": len(audit.disagreements),
                    },
                )

    finished = clock().astimezone(timezone.utc)
    run = RunResult(
        as_of=day,
        files=tuple(results),
        failures=tuple(failures),
        rebuild=rebuild,
        started_at=started,
        finished_at=finished,
        audit=audit,
    )
    await writer.submit(
        MetaWrite(META_LAST_RUN, json.dumps(run.as_dict(), separators=(",", ":")))
    )
    return run


def _snapshot_only(fetched: FetchedMaster, as_of: date, row_count: int) -> "SnapshotWrite":
    """Record that the file was seen today even though its content had not moved.

    Without this the snapshot table would have a hole on every quiet day and the truncation
    guard would compare against a stale row count.
    """
    return SnapshotWrite(fetched=fetched, as_of=as_of, row_count=row_count)
