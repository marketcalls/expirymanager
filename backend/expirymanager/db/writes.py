"""Write operations built here and executed by the single writer.

Nothing in this module touches DuckDB directly from a caller's thread. Every function either
builds a ``WriteOp`` or awaits ``DuckWriter.submit``, so the serialisation guarantee the writer
exists to provide is never bypassed.

The candle path deliberately adds nothing: ``db/writer.py`` already owns the delete-then-insert
transaction, the half-open right boundary and the payload hash short circuit. ``upsert_candle_chunk``
only assembles the op and enqueues it.

The catalog path is here because it is the part that has to think. Contract ids come from
``db/ids.py`` and must be allocated, kept stable and, on the rare block overflow, remapped across
``candles``, ``candle_coverage``, ``contract_bounds`` and ``candle_greeks`` inside one
transaction. A catalog upsert that renumbered contracts without moving their bars would silently
orphan every candle row ever downloaded, so that remap is not optional and not deferrable.

Catalog rows are written delete-then-insert rather than with INSERT OR REPLACE. Every catalog
table here carries two unique keys, a primary key and a natural key, and DuckDB refuses a
conflict clause when it cannot tell which one the caller meant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Sequence

import pyarrow as pa

from expirymanager.db.ids import (
    BlockAllocation,
    allocate_contract_block,
    allocate_spot_id,
    assign_contract_ids,
)
from expirymanager.db.writer import (
    CallableWrite,
    CandleChunkResult,
    CandleChunkWrite,
    CoverageRow,
)

if TYPE_CHECKING:
    from expirymanager.db.writer import DuckWriter

__all__ = [
    "UnderlyingRow",
    "ExpiryRow",
    "ContractRow",
    "UnderlyingResult",
    "ContractBatchResult",
    "UnderlyingWrite",
    "ExpiryBatchWrite",
    "ContractBatchWrite",
    "upsert_candle_chunk",
    "upsert_underlying",
    "upsert_expiries",
    "upsert_contracts",
    "deactivate_underlying",
    "delete_underlying",
    "start_ingest_run",
    "finish_ingest_run",
    "record_export",
    "delete_export",
]

SPOT_KIND = "SPOT"

UNDERLYING_COLUMNS = (
    "underlying_id",
    "fyers_symbol",
    "root",
    "exchange",
    "exchange_code",
    "segment",
    "segment_code",
    "instrument_kind",
    "display_name",
    "spot_contract_id",
    "underlying_fytoken",
    "data_from",
    "first_expiry",
    "last_expiry",
    "expiry_count",
    "contract_count",
    "is_active",
    "synced_at",
)

EXPIRY_COLUMNS = (
    "expiry_id",
    "underlying_id",
    "expiry_date",
    "has_futures",
    "has_options",
    "futures_count",
    "options_count",
    "contract_count",
    "min_strike",
    "max_strike",
    "strike_step",
    "contract_id_lo",
    "contract_id_hi",
    "expiry_cycle_derived",
    "expiry_cycle_source",
    "is_last_of_month",
    "expiry_dow",
    "source_range_from",
    "source_range_to",
    "discovered_at",
    "contracts_discovered_at",
    "discovered_task_id",
)

CONTRACT_COLUMNS = (
    "contract_id",
    "underlying_id",
    "expiry_id",
    "fyers_symbol",
    "kind",
    "instrument_class",
    "exchange",
    "exchange_code",
    "segment",
    "segment_code",
    "ex_instrument_type",
    "root",
    "expiry_date",
    "expiry_year",
    "expiry_month",
    "expiry_day",
    "expiry_dow",
    "parsed_expiry_date",
    "symbol_expiry_encoding",
    "expiry_cycle",
    "expiry_cycle_source",
    "strike",
    "strike_raw",
    "strike_ordinal",
    "option_type",
    "fytoken",
    "exchange_token",
    "isin",
    "lot_size",
    "tick_size",
    "qty_freeze",
    "qty_multiplier",
    "trading_session",
    "symbol_description",
    "instrument_master_valid_from",
    "source_expiry_date_requested",
    "source_array",
    "source_array_index",
    "source_endpoint",
    "discovered_task_id",
    "parse_method",
    "parse_confidence",
    "parse_warnings",
    "first_seen_at",
    "last_seen_at",
    "sealed_at",
    "raw_payload",
)

# Every table that carries a contract_id and therefore has to follow a renumber.
CONTRACT_ID_REFERENCES = ("candles", "candle_coverage", "contract_bounds", "candle_greeks")


def _insert(table: str, columns: Sequence[str]) -> str:
    placeholders = ", ".join("?" for _ in columns)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"


_UNDERLYING_INSERT = _insert("dim_underlying", UNDERLYING_COLUMNS)
_EXPIRY_INSERT = _insert("dim_expiry", EXPIRY_COLUMNS)
_CONTRACT_INSERT = _insert("dim_contract", CONTRACT_COLUMNS)


# ---------------------------------------------------------------------------
# Row shapes, mirroring the DDL column for column
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnderlyingRow:
    """One row of dim_underlying, the writer maintained mirror of underlying_registry."""

    underlying_id: int
    fyers_symbol: str
    root: str
    exchange: str
    exchange_code: int
    segment: str
    segment_code: int
    instrument_kind: str
    display_name: str
    data_from: date
    # None means "allocate the next reserved spot id", which is the registration path.
    spot_contract_id: int | None = None
    underlying_fytoken: str | None = None
    is_active: bool = True
    synced_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExpiryRow:
    """One row of dim_expiry, as produced by expiry discovery.

    The counts and the strike geometry are left None here and filled by the contract batch, which
    is the only thing that knows them.
    """

    underlying_id: int
    expiry_date: date
    expiry_cycle_derived: str | None = None
    expiry_cycle_source: str = "derived"
    is_last_of_month: bool | None = None
    source_range_from: date | None = None
    source_range_to: date | None = None
    discovered_at: datetime | None = None
    discovered_task_id: int | None = None


@dataclass(frozen=True, slots=True)
class ContractRow:
    """One row of dim_contract, before an id has been allocated to it.

    The five attributes ``db/ids.py`` orders on are plain fields here, so the allocator does not
    need to know anything else about a contract.
    """

    fyers_symbol: str
    kind: str
    instrument_class: str
    exchange: str
    exchange_code: int
    segment: str
    segment_code: int
    root: str
    source_endpoint: str
    parse_method: str
    parse_confidence: str

    strike: Decimal | None = None
    strike_raw: str | None = None
    strike_ordinal: int | None = None
    option_type: str | None = None

    expiry_date: date | None = None
    expiry_year: int | None = None
    expiry_month: int | None = None
    expiry_day: int | None = None
    expiry_dow: int | None = None
    parsed_expiry_date: date | None = None
    symbol_expiry_encoding: str | None = None
    expiry_cycle: str | None = None
    expiry_cycle_source: str | None = None

    ex_instrument_type: int | None = None
    fytoken: str | None = None
    exchange_token: int | None = None
    isin: str | None = None
    lot_size: int | None = None
    tick_size: Decimal | None = None
    qty_freeze: int | None = None
    qty_multiplier: Decimal | None = None
    trading_session: str | None = None
    symbol_description: str | None = None
    instrument_master_valid_from: date | None = None

    source_expiry_date_requested: date | None = None
    source_array: str | None = None
    source_array_index: int | None = None
    discovered_task_id: int | None = None
    parse_warnings: str | None = None
    sealed_at: datetime | None = None
    raw_payload: str | None = None


@dataclass(frozen=True, slots=True)
class UnderlyingResult:
    underlying_id: int
    spot_contract_id: int
    created: bool


@dataclass(frozen=True, slots=True)
class ContractBatchResult:
    """What one contract discovery batch did to the id keyspace."""

    expiry_id: int
    allocation: BlockAllocation
    contract_ids: dict[str, int]
    renumbered: dict[int, int]
    rows_written: int


# ---------------------------------------------------------------------------
# The candle path
# ---------------------------------------------------------------------------


async def upsert_candle_chunk(
    writer: DuckWriter,
    *,
    contract_id: int,
    res_id: int,
    range_from: date,
    range_to: date,
    coverage: CoverageRow,
    batch: pa.RecordBatch | pa.Table | None = None,
    honour_payload_hash: bool = True,
) -> CandleChunkResult:
    """Make one Fyers historical-data response durable.

    A thin builder on purpose. The transaction it enqueues is ``CandleChunkWrite`` in
    db/writer.py, which owns the delete-then-insert, the half-open right boundary, the coverage
    row and the bounds refresh. Reimplementing any of that here would give the system two
    definitions of idempotence and one of them would drift.
    """
    op = CandleChunkWrite(
        contract_id=contract_id,
        res_id=res_id,
        range_from=range_from,
        range_to=range_to,
        coverage=coverage,
        batch=batch,
        honour_payload_hash=honour_payload_hash,
    )
    return await writer.submit(op)


# ---------------------------------------------------------------------------
# Catalog write operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnderlyingWrite:
    """Mirror one underlying into DuckDB, allocating its reserved spot id if it has none.

    The dim_underlying row and its SPOT dim_contract row are written together because every spot
    bars query joins the two, and an underlying whose spot contract is missing renders as an
    empty chart with no explanation.
    """

    row: UnderlyingRow
    label: str = field(default="underlying_upsert", init=False)

    def apply(self, cur: Any) -> UnderlyingResult:
        row = self.row
        cur.execute("BEGIN TRANSACTION")
        try:
            existing = cur.execute(
                "SELECT spot_contract_id FROM dim_underlying WHERE underlying_id = ?",
                [row.underlying_id],
            ).fetchone()
            if row.spot_contract_id is not None:
                spot_id = int(row.spot_contract_id)
            elif existing is not None:
                spot_id = int(existing[0])
            else:
                spot_id = allocate_spot_id(cur)
            synced_at = row.synced_at or datetime.now()

            # Both unique keys are cleared, because a rename of fyers_symbol has to displace the
            # old natural key as well as the old primary key.
            cur.execute(
                "DELETE FROM dim_underlying WHERE underlying_id = ? OR fyers_symbol = ?",
                [row.underlying_id, row.fyers_symbol],
            )
            cur.execute(
                _UNDERLYING_INSERT,
                [
                    row.underlying_id,
                    row.fyers_symbol,
                    row.root,
                    row.exchange,
                    row.exchange_code,
                    row.segment,
                    row.segment_code,
                    row.instrument_kind,
                    row.display_name,
                    spot_id,
                    row.underlying_fytoken,
                    row.data_from,
                    None,
                    None,
                    None,
                    None,
                    row.is_active,
                    synced_at,
                ],
            )
            self._write_spot_contract(cur, spot_id, synced_at)
            _refresh_underlying_rollup(cur, row.underlying_id)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return UnderlyingResult(
            underlying_id=row.underlying_id,
            spot_contract_id=spot_id,
            created=existing is None,
        )

    def _write_spot_contract(self, cur: Any, spot_id: int, synced_at: datetime) -> None:
        row = self.row
        first_seen = cur.execute(
            "SELECT first_seen_at FROM dim_contract WHERE contract_id = ?", [spot_id]
        ).fetchone()
        cur.execute(
            "DELETE FROM dim_contract WHERE contract_id = ? OR fyers_symbol = ?",
            [spot_id, row.fyers_symbol],
        )
        values: dict[str, Any] = {name: None for name in CONTRACT_COLUMNS}
        values.update(
            contract_id=spot_id,
            underlying_id=row.underlying_id,
            expiry_id=None,
            fyers_symbol=row.fyers_symbol,
            kind=SPOT_KIND,
            instrument_class=row.instrument_kind,
            exchange=row.exchange,
            exchange_code=row.exchange_code,
            segment=row.segment,
            segment_code=row.segment_code,
            root=row.root,
            fytoken=row.underlying_fytoken,
            source_endpoint="registry",
            parse_method="registry",
            parse_confidence="exact",
            first_seen_at=first_seen[0] if first_seen else synced_at,
            last_seen_at=synced_at,
        )
        cur.execute(_CONTRACT_INSERT, [values[name] for name in CONTRACT_COLUMNS])


@dataclass(frozen=True, slots=True)
class ExpiryBatchWrite:
    """Upsert the expiry dates one discovery call returned.

    Discovery repeats a lot, because the 366 day window means a multi-year backfill re-reads
    overlapping ranges. So an expiry that already exists keeps its expiry_id, its id block and
    every count the contract batch put there; only the discovery provenance is refreshed.
    """

    rows: tuple[ExpiryRow, ...]
    label: str = field(default="expiry_upsert", init=False)

    def apply(self, cur: Any) -> dict[date, int]:
        now = datetime.now()
        result: dict[date, int] = {}
        cur.execute("BEGIN TRANSACTION")
        try:
            for row in self.rows:
                existing = cur.execute(
                    "SELECT " + ", ".join(EXPIRY_COLUMNS) + " FROM dim_expiry "
                    "WHERE underlying_id = ? AND expiry_date = ?",
                    [row.underlying_id, row.expiry_date],
                ).fetchone()
                if existing is None:
                    expiry_id = int(cur.execute("SELECT nextval('seq_expiry_id')").fetchone()[0])
                    values: dict[str, Any] = {name: None for name in EXPIRY_COLUMNS}
                    values.update(
                        expiry_id=expiry_id,
                        underlying_id=row.underlying_id,
                        expiry_date=row.expiry_date,
                        has_futures=False,
                        has_options=False,
                        expiry_cycle_source=row.expiry_cycle_source,
                        discovered_at=row.discovered_at or now,
                    )
                else:
                    values = dict(zip(EXPIRY_COLUMNS, existing))
                    expiry_id = int(values["expiry_id"])

                values["expiry_cycle_derived"] = (
                    row.expiry_cycle_derived or values["expiry_cycle_derived"]
                )
                values["expiry_cycle_source"] = row.expiry_cycle_source
                values["is_last_of_month"] = (
                    row.is_last_of_month
                    if row.is_last_of_month is not None
                    else values["is_last_of_month"]
                )
                values["expiry_dow"] = row.expiry_date.isoweekday()
                values["source_range_from"] = row.source_range_from or values["source_range_from"]
                values["source_range_to"] = row.source_range_to or values["source_range_to"]
                values["discovered_task_id"] = (
                    row.discovered_task_id
                    if row.discovered_task_id is not None
                    else values["discovered_task_id"]
                )

                cur.execute(
                    "DELETE FROM dim_expiry WHERE expiry_id = ? "
                    "OR (underlying_id = ? AND expiry_date = ?)",
                    [expiry_id, row.underlying_id, row.expiry_date],
                )
                cur.execute(_EXPIRY_INSERT, [values[name] for name in EXPIRY_COLUMNS])
                result[row.expiry_date] = expiry_id

            for underlying_id in {row.underlying_id for row in self.rows}:
                _refresh_underlying_rollup(cur, underlying_id)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return result


@dataclass(frozen=True, slots=True)
class ContractBatchWrite:
    """One Get Expired Contracts response, turned into a contiguous id block.

    Everything about this operation is driven by the id keyspace. The block is allocated once and
    reused forever, existing symbols keep their ids, and if the block genuinely overflows the new
    block is allocated and every table carrying the old ids is remapped in the same transaction.

    The batch only adds and updates. A contract this response does not mention is left alone,
    because an expired option chain does not lose strikes: a short response is a partial one, and
    deleting on it would throw away contracts whose bars cost governed requests that cannot be
    recovered. Removing a contract is a separate deliberate act, not a side effect of a refetch.
    """

    underlying_id: int
    expiry_date: date
    rows: tuple[ContractRow, ...]
    discovered_at: datetime | None = None
    label: str = field(default="contract_batch_upsert", init=False)

    def apply(self, cur: Any) -> ContractBatchResult:
        now = self.discovered_at or datetime.now()
        cur.execute("BEGIN TRANSACTION")
        try:
            expiry_id = self._expiry_id(cur, now)
            existing = {
                str(symbol): int(contract_id)
                for symbol, contract_id in cur.execute(
                    "SELECT fyers_symbol, contract_id FROM dim_contract "
                    "WHERE underlying_id = ? AND expiry_date = ? AND kind <> ?",
                    [self.underlying_id, self.expiry_date, SPOT_KIND],
                ).fetchall()
            }
            allocation = allocate_contract_block(
                cur, self.underlying_id, self.expiry_date, len(self.rows)
            )
            assigned = assign_contract_ids(allocation, self.rows, existing)

            renumbered = {
                old: assigned[symbol]
                for symbol, old in existing.items()
                if symbol in assigned and assigned[symbol] != old
            }
            if renumbered:
                _remap_contract_ids(cur, renumbered)

            first_seen = {
                str(symbol): seen
                for symbol, seen in cur.execute(
                    "SELECT fyers_symbol, first_seen_at FROM dim_contract "
                    "WHERE underlying_id = ? AND expiry_date = ?",
                    [self.underlying_id, self.expiry_date],
                ).fetchall()
            }
            # One semi join rather than a delete per contract. A weekly NIFTY chain is 482 rows
            # and this loop runs once per expiry per discovery.
            keys = pa.table(
                {
                    "contract_id": pa.array(list(assigned.values()), pa.int32()),
                    "fyers_symbol": pa.array(list(assigned.keys()), pa.string()),
                }
            )
            cur.register("contract_batch_keys", keys)
            try:
                cur.execute(
                    "DELETE FROM dim_contract WHERE contract_id IN "
                    "(SELECT contract_id FROM contract_batch_keys) "
                    "OR fyers_symbol IN (SELECT fyers_symbol FROM contract_batch_keys)"
                )
            finally:
                cur.unregister("contract_batch_keys")
            cur.executemany(
                _CONTRACT_INSERT,
                [
                    self._contract_values(
                        row,
                        assigned[row.fyers_symbol],
                        expiry_id,
                        first_seen.get(row.fyers_symbol, now),
                        now,
                    )
                    for row in self.rows
                ],
            )

            _refresh_expiry_rollup(cur, expiry_id, allocation, now)
            _refresh_underlying_rollup(cur, self.underlying_id)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return ContractBatchResult(
            expiry_id=expiry_id,
            allocation=allocation,
            contract_ids=assigned,
            renumbered=renumbered,
            rows_written=len(self.rows),
        )

    def _expiry_id(self, cur: Any, now: datetime) -> int:
        row = cur.execute(
            "SELECT expiry_id FROM dim_expiry WHERE underlying_id = ? AND expiry_date = ?",
            [self.underlying_id, self.expiry_date],
        ).fetchone()
        if row is not None:
            return int(row[0])
        # Contracts can arrive for an expiry discovery never recorded, for instance when a user
        # asks for one expiry directly. Creating the parent row is better than dropping them.
        expiry_id = int(cur.execute("SELECT nextval('seq_expiry_id')").fetchone()[0])
        values: dict[str, Any] = {name: None for name in EXPIRY_COLUMNS}
        values.update(
            expiry_id=expiry_id,
            underlying_id=self.underlying_id,
            expiry_date=self.expiry_date,
            has_futures=False,
            has_options=False,
            expiry_cycle_source="derived",
            expiry_dow=self.expiry_date.isoweekday(),
            discovered_at=now,
        )
        cur.execute(_EXPIRY_INSERT, [values[name] for name in EXPIRY_COLUMNS])
        return expiry_id

    def _contract_values(
        self,
        row: ContractRow,
        contract_id: int,
        expiry_id: int,
        first_seen_at: datetime,
        now: datetime,
    ) -> list[Any]:
        expiry_date = row.expiry_date or self.expiry_date
        values: dict[str, Any] = {name: getattr(row, name, None) for name in CONTRACT_COLUMNS}
        values.update(
            contract_id=contract_id,
            underlying_id=self.underlying_id,
            expiry_id=expiry_id,
            expiry_date=expiry_date,
            expiry_year=row.expiry_year if row.expiry_year is not None else expiry_date.year,
            expiry_month=row.expiry_month if row.expiry_month is not None else expiry_date.month,
            expiry_day=row.expiry_day if row.expiry_day is not None else expiry_date.day,
            expiry_dow=(
                row.expiry_dow if row.expiry_dow is not None else expiry_date.isoweekday()
            ),
            source_expiry_date_requested=(
                row.source_expiry_date_requested or self.expiry_date
            ),
            first_seen_at=first_seen_at,
            last_seen_at=now,
        )
        return [values[name] for name in CONTRACT_COLUMNS]


def _remap_contract_ids(cur: Any, renumbered: dict[int, int]) -> None:
    """Move every row that carries an old contract id onto its new one.

    A renumber that forgot one of these tables would orphan bars that took hours of governed
    requests to fetch, so the table list is explicit and lives beside the operation that uses it.
    """
    if not renumbered:
        return
    if set(renumbered) & set(renumbered.values()):
        # A permutation inside one block would have the UPDATE overwrite an id that another
        # contract is still using. The allocator only ever renumbers into a fresh disjoint
        # block, so this is an invariant check rather than a case to handle.
        raise ValueError(
            "refusing a contract id remap whose old and new ranges overlap, because the "
            "update would collide with ids still in use"
        )
    mapping = pa.table(
        {
            "old_id": pa.array(list(renumbered.keys()), pa.int32()),
            "new_id": pa.array(list(renumbered.values()), pa.int32()),
        }
    )
    cur.register("contract_id_remap", mapping)
    try:
        for table in CONTRACT_ID_REFERENCES:
            cur.execute(
                f"UPDATE {table} SET contract_id = m.new_id "
                f"FROM contract_id_remap m WHERE {table}.contract_id = m.old_id"
            )
    finally:
        cur.unregister("contract_id_remap")


def _refresh_expiry_rollup(
    cur: Any, expiry_id: int, allocation: BlockAllocation, now: datetime
) -> None:
    """Recompute the counts and the strike geometry from the contracts that are now present.

    Derived rather than accumulated, for the same reason contract_bounds is: a re-discovery that
    returns fewer contracts has to shrink the counts, not leave stale ones behind.
    """
    row = cur.execute(
        "SELECT count(*), "
        "       count(*) FILTER (WHERE kind = 'FUT'), "
        "       count(*) FILTER (WHERE kind = 'OPT'), "
        "       min(strike), max(strike) "
        "  FROM dim_contract WHERE expiry_id = ?",
        [expiry_id],
    ).fetchone()
    total = int(row[0]) if row else 0
    futures = int(row[1]) if row else 0
    options = int(row[2]) if row else 0
    step = cur.execute(
        "WITH s AS (SELECT DISTINCT strike FROM dim_contract "
        "            WHERE expiry_id = ? AND kind = 'OPT' AND strike IS NOT NULL), "
        "     g AS (SELECT strike - lag(strike) OVER (ORDER BY strike) AS gap FROM s) "
        "SELECT gap FROM g WHERE gap > 0 "
        " GROUP BY gap ORDER BY count(*) DESC, gap LIMIT 1",
        [expiry_id],
    ).fetchone()
    cur.execute(
        "UPDATE dim_expiry SET has_futures = ?, has_options = ?, futures_count = ?, "
        "       options_count = ?, contract_count = ?, min_strike = ?, max_strike = ?, "
        "       strike_step = ?, contract_id_lo = ?, contract_id_hi = ?, "
        "       contracts_discovered_at = ? "
        " WHERE expiry_id = ?",
        [
            futures > 0,
            options > 0,
            futures,
            options,
            total,
            row[3] if row else None,
            row[4] if row else None,
            step[0] if step else None,
            allocation.lo,
            allocation.hi,
            now,
            expiry_id,
        ],
    )


def _refresh_underlying_rollup(cur: Any, underlying_id: int) -> None:
    """Keep the underlying card counts in step with the expiries below it."""
    row = cur.execute(
        "SELECT min(expiry_date), max(expiry_date), count(*), coalesce(sum(contract_count), 0) "
        "  FROM dim_expiry WHERE underlying_id = ?",
        [underlying_id],
    ).fetchone()
    if row is None:
        return
    cur.execute(
        "UPDATE dim_underlying SET first_expiry = ?, last_expiry = ?, expiry_count = ?, "
        "       contract_count = ? WHERE underlying_id = ?",
        [row[0], row[1], int(row[2]), int(row[3]), underlying_id],
    )


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------


async def upsert_underlying(writer: DuckWriter, row: UnderlyingRow) -> UnderlyingResult:
    return await writer.submit(UnderlyingWrite(row=row))


async def upsert_expiries(
    writer: DuckWriter, rows: Sequence[ExpiryRow]
) -> dict[date, int]:
    if not rows:
        return {}
    return await writer.submit(ExpiryBatchWrite(rows=tuple(rows)))


async def upsert_contracts(
    writer: DuckWriter,
    *,
    underlying_id: int,
    expiry_date: date,
    rows: Sequence[ContractRow],
    discovered_at: datetime | None = None,
) -> ContractBatchResult:
    return await writer.submit(
        ContractBatchWrite(
            underlying_id=underlying_id,
            expiry_date=expiry_date,
            rows=tuple(rows),
            discovered_at=discovered_at,
        )
    )


async def deactivate_underlying(writer: DuckWriter, underlying_id: int) -> None:
    """Hide an underlying without touching a single bar. The reversible half of delete."""

    def run(cur: Any) -> None:
        cur.execute(
            "UPDATE dim_underlying SET is_active = FALSE WHERE underlying_id = ?",
            [underlying_id],
        )

    await writer.submit(CallableWrite(label="underlying_deactivate", fn=run))


async def delete_underlying(
    writer: DuckWriter, underlying_id: int, *, purge_data: bool = False
) -> int:
    """Remove an underlying, optionally with every bar it owns.

    Returns the number of candle rows deleted. The catalog rows go either way; ``purge_data``
    decides only whether the bars follow, because re-downloading them costs governed requests
    that cannot be recovered once spent.
    """

    def run(cur: Any) -> int:
        cur.execute("BEGIN TRANSACTION")
        try:
            ids = [
                int(value[0])
                for value in cur.execute(
                    "SELECT contract_id FROM dim_contract WHERE underlying_id = ?",
                    [underlying_id],
                ).fetchall()
            ]
            deleted = 0
            if purge_data and ids:
                cur.register("purge_ids", pa.table({"contract_id": pa.array(ids, pa.int32())}))
                try:
                    row = cur.execute(
                        "DELETE FROM candles WHERE contract_id IN "
                        "(SELECT contract_id FROM purge_ids)"
                    ).fetchone()
                    deleted = int(row[0]) if row else 0
                    for table in ("candle_coverage", "contract_bounds", "candle_greeks"):
                        cur.execute(
                            f"DELETE FROM {table} WHERE contract_id IN "
                            "(SELECT contract_id FROM purge_ids)"
                        )
                finally:
                    cur.unregister("purge_ids")
            cur.execute("DELETE FROM dim_contract WHERE underlying_id = ?", [underlying_id])
            cur.execute("DELETE FROM dim_expiry WHERE underlying_id = ?", [underlying_id])
            cur.execute("DELETE FROM dim_underlying WHERE underlying_id = ?", [underlying_id])
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return deleted

    return await writer.submit(CallableWrite(label="underlying_delete", fn=run))


async def start_ingest_run(
    writer: DuckWriter,
    *,
    job_id: str,
    job_kind: str,
    app_version: str,
    underlying_id: int | None = None,
    started_at: datetime | None = None,
) -> int:
    """Open an ingest_run row and return its id."""

    def run(cur: Any) -> int:
        run_id = int(cur.execute("SELECT nextval('seq_run_id')").fetchone()[0])
        cur.execute(
            "INSERT INTO ingest_run (run_id, job_id, job_kind, underlying_id, started_at, "
            "status, app_version) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            [run_id, job_id, job_kind, underlying_id, started_at or datetime.now(), app_version],
        )
        return run_id

    return await writer.submit(CallableWrite(label="ingest_run_start", fn=run))


async def finish_ingest_run(
    writer: DuckWriter,
    run_id: int,
    *,
    status: str,
    requests_made: int = 0,
    rows_written: int = 0,
    bytes_downloaded: int = 0,
    error_text: str | None = None,
    finished_at: datetime | None = None,
) -> None:
    def run(cur: Any) -> None:
        cur.execute(
            "UPDATE ingest_run SET finished_at = ?, status = ?, requests_made = ?, "
            "       rows_written = ?, bytes_downloaded = ?, error_text = ? WHERE run_id = ?",
            [
                finished_at or datetime.now(),
                status,
                requests_made,
                rows_written,
                bytes_downloaded,
                error_text,
                run_id,
            ],
        )

    await writer.submit(CallableWrite(label="ingest_run_finish", fn=run))


async def record_export(
    writer: DuckWriter,
    *,
    export_id: str,
    kind: str,
    path: str,
    filters: dict[str, Any],
    row_count: int | None,
    byte_size: int | None,
    sha256: str | None,
    created_at: datetime | None = None,
) -> None:
    def run(cur: Any) -> None:
        cur.execute("DELETE FROM export_manifest WHERE export_id = ?", [export_id])
        cur.execute(
            "INSERT INTO export_manifest (export_id, kind, path, filters_json, row_count, "
            "byte_size, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                export_id,
                kind,
                path,
                json.dumps(filters, default=str, sort_keys=True),
                row_count,
                byte_size,
                sha256,
                created_at or datetime.now(),
            ],
        )

    await writer.submit(CallableWrite(label="export_manifest", fn=run))


async def delete_export(writer: DuckWriter, export_id: str) -> None:
    def run(cur: Any) -> None:
        cur.execute("DELETE FROM export_manifest WHERE export_id = ?", [export_id])

    await writer.submit(CallableWrite(label="export_manifest_delete", fn=run))
