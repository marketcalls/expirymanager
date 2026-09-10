"""Contract id allocation.

``contract_id`` is the leading key of the physical sort order of ``candles``, so it is not a
surrogate that can be handed out arbitrarily. The keyspace is:

    1 ..    999   one reserved id per underlying, for its SPOT row. Underlyings therefore
                  cluster at the very front of the file and a spot scan touches a handful of
                  row groups.

    1024 .. 2^31-1  one padded contiguous block per (underlying_id, expiry_date), block size
                  ceil(contract_count / 1024) * 1024 with a minimum of 1024, ids inside the
                  block ordered by strike then option right.

Two properties are load bearing and neither can be recovered later.

Contiguity is what turns "the whole chain of this expiry" and "ATM plus or minus N strikes" into
a BETWEEN on the leading sort key rather than several hundred scattered lookups. The padding is
what lets a second discovery of the same expiry add strikes without disturbing the block.

Stability is why an id, once assigned to a symbol, is never reassigned. Renumbering means
rewriting every candle row that carries the old id, and ``candles`` is the largest table in the
system. So a re-discovery keeps every existing id and appends new symbols into the block's spare
capacity in sorted order. Strict strike order therefore holds for the ids assigned in one batch,
which is the case that matters because Get Expired Contracts returns a whole chain at once.
Only an overflow of the block forces a renumber, and that is handled explicitly by the caller
rather than happening silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Protocol, Sequence

__all__ = [
    "SPOT_ID_MIN",
    "SPOT_ID_MAX",
    "BLOCK_SIZE",
    "FIRST_BLOCK_BASE",
    "MAX_CONTRACT_ID",
    "BLOCK_SEQUENCE",
    "IdSpaceError",
    "SpotIdSpaceExhausted",
    "ContractIdSpaceExhausted",
    "BlockOverflow",
    "Orderable",
    "BlockAllocation",
    "block_size_for",
    "sort_key",
    "order_contracts",
    "allocate_spot_id",
    "reserve_block",
    "allocate_contract_block",
    "assign_contract_ids",
    "sequence_next_unit",
    "base_for_unit",
    "unit_for_base",
    "BLOCK_SEQUENCE_ORIGIN",
]

SPOT_ID_MIN = 1
SPOT_ID_MAX = 999

BLOCK_SIZE = 1024

# The first block starts at 1024 rather than 1000 so that every block base is a multiple of the
# block size. That makes an id's block derivable by integer division, which is what the
# duplicate assertion and the compaction routine use to sanity check a file.
FIRST_BLOCK_BASE = BLOCK_SIZE

# dim_contract.contract_id is INTEGER, so this is the real ceiling.
MAX_CONTRACT_ID = 2**31 - 1

BLOCK_SEQUENCE = "seq_contract_block"

# The declared START of seq_contract_block in duck_schema.sql. It is a constant here and is never
# read back from duckdb_sequences(), because DuckDB rewrites a sequence's start_value on reopen:
# measured, a sequence declared START 1000 and advanced once reports start_value 1000 in session
# and start_value 1001 after a close and reopen. Deriving the block base from the reported start
# would therefore hand the first block of every restarted process the same ids, which is the one
# corruption this whole module exists to prevent.
BLOCK_SEQUENCE_ORIGIN = 1000

# Ranks used to order contracts inside a block. Futures carry a NULL strike and there are only a
# handful of them, so they take the front of the block and leave the options strictly ordered by
# strike for the whole rest of the range.
_KIND_RANK = {"FUT": 0, "OPT": 1}
_OPTION_TYPE_RANK = {"CE": 0, "PE": 1}


class IdSpaceError(RuntimeError):
    """The id keyspace cannot satisfy a request."""


class SpotIdSpaceExhausted(IdSpaceError):
    """More than 999 underlyings. Surfaces as 507 spot_id_space_exhausted."""


class ContractIdSpaceExhausted(IdSpaceError):
    """The 2^31 contract id ceiling has been reached."""


class BlockOverflow(IdSpaceError):
    """More contracts than the already allocated block can hold.

    Raised only when the caller asked for a strictly non-renumbering allocation. The normal path
    allocates a larger block and reports the old range so the caller can remap.
    """


class Orderable(Protocol):
    """The minimum a contract needs to expose to be placed inside a block."""

    fyers_symbol: str
    kind: str
    strike: Decimal | None
    option_type: str | None
    instrument_class: str | None


@dataclass(frozen=True, slots=True)
class BlockAllocation:
    """Where one expiry's contracts live in the id keyspace."""

    lo: int
    hi: int
    created: bool
    # The range this block replaces, when the contract count outgrew the previous block. The
    # caller must remap candles, coverage and bounds from the old ids in the same transaction.
    previous: tuple[int, int] | None = None

    @property
    def size(self) -> int:
        return self.hi - self.lo + 1

    @property
    def units(self) -> int:
        return self.size // BLOCK_SIZE

    def contains(self, contract_id: int) -> bool:
        return self.lo <= contract_id <= self.hi


def block_size_for(contract_count: int) -> int:
    """Padded block size for a contract count. Always a positive multiple of 1024."""
    if contract_count < 0:
        raise ValueError("contract_count cannot be negative")
    units = max(1, -(-contract_count // BLOCK_SIZE))
    return units * BLOCK_SIZE


def sort_key(item: Orderable) -> tuple[Any, ...]:
    """The order ids are handed out in inside a block.

    Strike ascending is the property the chain band query depends on. option_type breaks the tie
    so a strike's call and put are adjacent, instrument_class and the symbol break the remaining
    ties so that two runs over the same input produce the same order on any machine.
    """
    strike = item.strike
    return (
        _KIND_RANK.get(item.kind, 2),
        Decimal(0) if strike is None else Decimal(strike),
        _OPTION_TYPE_RANK.get(item.option_type or "", 2),
        item.instrument_class or "",
        item.fyers_symbol,
    )


def order_contracts(items: Iterable[Orderable]) -> list[Orderable]:
    """Contracts in id assignment order."""
    return sorted(items, key=sort_key)


def allocate_spot_id(cur: Any) -> int:
    """The lowest free reserved id in 1..999.

    Lowest free rather than next highest, so that deleting an underlying and adding another one
    keeps the spot rows packed at the front of the file.
    """
    row = cur.execute(
        "SELECT min(i) FROM range(?, ?) t(i) "
        "WHERE i NOT IN (SELECT spot_contract_id FROM dim_underlying)",
        [SPOT_ID_MIN, SPOT_ID_MAX + 1],
    ).fetchone()
    if row is None or row[0] is None:
        raise SpotIdSpaceExhausted(
            f"All {SPOT_ID_MAX} reserved spot contract ids are in use. ExpiryManager supports "
            f"at most {SPOT_ID_MAX} underlyings, because ids 1 to {SPOT_ID_MAX} are reserved "
            "for spot rows so that they cluster at the front of the candles sort order."
        )
    return int(row[0])


def base_for_unit(unit: int) -> int:
    """The first contract id of a sequence unit."""
    return FIRST_BLOCK_BASE + (unit - BLOCK_SEQUENCE_ORIGIN) * BLOCK_SIZE


def unit_for_base(base: int) -> int:
    """The inverse, for the floor below and for the compaction routine."""
    return BLOCK_SEQUENCE_ORIGIN + (base - FIRST_BLOCK_BASE) // BLOCK_SIZE


def sequence_next_unit(cur: Any) -> int:
    """The unit a fresh allocation must not start below.

    Derived from the blocks that are actually recorded, not from ``duckdb_sequences()``. That
    view cannot answer the question: in session it reports the last value handed out, and after a
    reopen it reports the value that will be handed out next, and the two shapes are identical.
    The recorded blocks are unambiguous, and the block sequence and dim_expiry are advanced in
    the same transaction, so they cannot disagree.
    """
    row = cur.execute(
        "SELECT max(contract_id_hi) FROM dim_expiry WHERE contract_id_hi IS NOT NULL"
    ).fetchone()
    highest = None if row is None else row[0]
    derived = FIRST_BLOCK_BASE if highest is None else int(highest) + 1
    reported = cur.execute(
        "SELECT last_value FROM duckdb_sequences() WHERE sequence_name = ?", [BLOCK_SEQUENCE]
    ).fetchone()
    floor_unit = unit_for_base(derived)
    if reported is not None and reported[0] is not None:
        floor_unit = max(floor_unit, int(reported[0]))
    return floor_unit


def reserve_block(cur: Any, units: int) -> tuple[int, int]:
    """Take ``units`` consecutive units off the block sequence and return (lo, hi).

    Consecutive is guaranteed because every write in this process goes through the single writer,
    so no other transaction can interleave a nextval.

    The sequence is fast forwarded past every block already recorded before anything is taken
    from it. Without that, a database whose sequence is behind its catalog, which is what a
    restored backup or an aborted allocation leaves, would hand a new expiry a block that
    overlaps an existing one, and every query that prunes on contract_id would then silently
    return another expiry's rows.
    """
    if units < 1:
        raise ValueError("a block is at least one unit")
    floor_unit = sequence_next_unit(cur)
    first = int(cur.execute(f"SELECT nextval('{BLOCK_SEQUENCE}')").fetchone()[0])
    if first < floor_unit:
        cur.execute(
            f"SELECT nextval('{BLOCK_SEQUENCE}') FROM range(?)", [floor_unit - first]
        ).fetchall()
        first = floor_unit
    if units > 1:
        values = [
            int(row[0])
            for row in cur.execute(
                f"SELECT nextval('{BLOCK_SEQUENCE}') FROM range(?)", [units - 1]
            ).fetchall()
        ]
        if len(values) != units - 1 or values[-1] - first != units - 1:
            raise IdSpaceError(
                f"{BLOCK_SEQUENCE} handed out non consecutive values starting at {first} for "
                f"{units} units, so a contiguous block cannot be formed"
            )
    lo = base_for_unit(first)
    hi = lo + units * BLOCK_SIZE - 1
    if hi > MAX_CONTRACT_ID:
        raise ContractIdSpaceExhausted(
            f"contract id block {lo} to {hi} exceeds the {MAX_CONTRACT_ID} ceiling of the "
            "INTEGER contract_id column"
        )
    return (lo, hi)


def allocate_contract_block(
    cur: Any,
    underlying_id: int,
    expiry_date: date,
    contract_count: int,
    *,
    allow_renumber: bool = True,
) -> BlockAllocation:
    """The id block for one (underlying, expiry), allocating it on first sight.

    Idempotent: a second call for the same expiry returns the same range as long as the block
    still holds the contract count, which is what makes ids stable across restarts and across a
    repeated discovery.
    """
    row = cur.execute(
        "SELECT contract_id_lo, contract_id_hi FROM dim_expiry "
        "WHERE underlying_id = ? AND expiry_date = ?",
        [underlying_id, expiry_date],
    ).fetchone()
    needed = block_size_for(contract_count)

    if row is not None and row[0] is not None and row[1] is not None:
        lo, hi = int(row[0]), int(row[1])
        if hi - lo + 1 >= needed:
            return BlockAllocation(lo=lo, hi=hi, created=False)
        if not allow_renumber:
            raise BlockOverflow(
                f"expiry {expiry_date} of underlying {underlying_id} holds block {lo} to {hi} "
                f"but {contract_count} contracts need {needed} ids"
            )
        new_lo, new_hi = reserve_block(cur, needed // BLOCK_SIZE)
        return BlockAllocation(lo=new_lo, hi=new_hi, created=True, previous=(lo, hi))

    new_lo, new_hi = reserve_block(cur, needed // BLOCK_SIZE)
    return BlockAllocation(lo=new_lo, hi=new_hi, created=True)


def assign_contract_ids(
    allocation: BlockAllocation,
    items: Sequence[Orderable],
    existing: dict[str, int] | None = None,
) -> dict[str, int]:
    """Map every symbol to its contract id inside the block.

    ``existing`` is the symbol to id map already recorded for this expiry. Those ids are kept
    untouched whenever they still fall inside the block, because reassigning one means rewriting
    every candle row that carries it. New symbols take the lowest free slots in the block, in
    sorted order, which keeps a first discovery in perfect strike order and keeps a later
    addition merely inside the block.
    """
    held = dict(existing or {})
    assigned: dict[str, int] = {}
    used: set[int] = set()

    for item in items:
        current = held.get(item.fyers_symbol)
        if current is not None and allocation.contains(current) and current not in used:
            assigned[item.fyers_symbol] = current
            used.add(current)

    cursor = allocation.lo
    for item in order_contracts(items):
        if item.fyers_symbol in assigned:
            continue
        while cursor in used:
            cursor += 1
        if cursor > allocation.hi:
            raise BlockOverflow(
                f"block {allocation.lo} to {allocation.hi} cannot hold {len(items)} contracts"
            )
        assigned[item.fyers_symbol] = cursor
        used.add(cursor)
        cursor += 1

    return assigned
