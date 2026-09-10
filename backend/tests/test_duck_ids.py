"""Contract id allocation.

The id keyspace is fixed before the first download, because renumbering later means rewriting
every candle row. These tests pin the three properties that cannot be recovered afterwards:
contiguity, alignment and stability.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from expirymanager.db.duck import DuckStore
from expirymanager.db.ids import (
    BLOCK_SIZE,
    FIRST_BLOCK_BASE,
    SPOT_ID_MAX,
    BlockAllocation,
    SpotIdSpaceExhausted,
    allocate_contract_block,
    allocate_spot_id,
    assign_contract_ids,
    block_size_for,
    order_contracts,
    reserve_block,
    sequence_next_unit,
)


class Item:
    """The minimum db/ids.py orders on."""

    def __init__(self, symbol, kind="OPT", strike=None, option_type=None, klass="OPTIDX"):
        self.fyers_symbol = symbol
        self.kind = kind
        self.strike = None if strike is None else Decimal(str(strike))
        self.option_type = option_type
        self.instrument_class = klass


def chain(strikes, expiry="25MAR"):
    items = []
    for strike in strikes:
        for right in ("CE", "PE"):
            items.append(
                Item(f"NSE:NIFTY{expiry}{strike}{right}", strike=strike, option_type=right)
            )
    return items


@pytest.fixture
def store(tmp_path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    yield store
    store.close()


def register_underlying(store, underlying_id=1, symbol="NSE:NIFTY50-INDEX", spot_id=None):
    cur = store.connection
    if spot_id is None:
        spot_id = allocate_spot_id(cur)
    cur.execute(
        "INSERT INTO dim_underlying VALUES (?, ?, 'NIFTY', 'NSE', 10, 'CM', 10, 'INDEX', "
        "'Nifty 50', ?, NULL, DATE '2022-01-03', NULL, NULL, NULL, NULL, TRUE, now())",
        [underlying_id, symbol, spot_id],
    )
    return spot_id


# -- block sizing ----------------------------------------------------------


def test_block_size_is_padded_to_a_multiple_of_1024():
    assert block_size_for(1) == 1024
    assert block_size_for(0) == 1024
    assert block_size_for(1024) == 1024
    assert block_size_for(1025) == 2048
    assert block_size_for(482) == 1024
    assert block_size_for(3000) == 3072


def test_block_size_rejects_a_negative_count():
    with pytest.raises(ValueError):
        block_size_for(-1)


# -- spot ids --------------------------------------------------------------


def test_spot_ids_start_at_one_and_fill_the_lowest_free_slot(store):
    assert register_underlying(store, 1, "NSE:NIFTY50-INDEX") == 1
    assert register_underlying(store, 2, "NSE:NIFTYBANK-INDEX") == 2
    store.connection.execute("DELETE FROM dim_underlying WHERE underlying_id = 1")
    assert allocate_spot_id(store.connection) == 1


def test_spot_id_space_is_refused_past_999(store):
    store.connection.execute(
        "INSERT INTO dim_underlying SELECT i, 'SYM' || i, 'R', 'NSE', 10, 'CM', 10, 'INDEX', "
        "'n', i, NULL, DATE '2022-01-03', NULL, NULL, NULL, NULL, TRUE, now() "
        "FROM range(1, 1000) t(i)",
    )
    with pytest.raises(SpotIdSpaceExhausted):
        allocate_spot_id(store.connection)


def test_spot_ids_never_collide_with_the_first_contract_block(store):
    register_underlying(store)
    lo, _ = reserve_block(store.connection, 1)
    assert lo == FIRST_BLOCK_BASE
    assert lo > SPOT_ID_MAX


# -- block reservation -----------------------------------------------------


def test_reserved_blocks_are_contiguous_aligned_and_never_reused(store):
    first_lo, first_hi = reserve_block(store.connection, 1)
    second_lo, second_hi = reserve_block(store.connection, 3)
    third_lo, _ = reserve_block(store.connection, 1)

    assert first_hi - first_lo + 1 == BLOCK_SIZE
    assert second_hi - second_lo + 1 == 3 * BLOCK_SIZE
    assert second_lo == first_hi + 1
    assert third_lo == second_hi + 1
    for value in (first_lo, second_lo, third_lo):
        assert value % BLOCK_SIZE == 0


def test_reserving_zero_units_is_refused(store):
    with pytest.raises(ValueError):
        reserve_block(store.connection, 0)


# -- allocation against dim_expiry ----------------------------------------


def seed_expiry(store, underlying_id, expiry, lo=None, hi=None):
    store.connection.execute(
        "INSERT INTO dim_expiry (expiry_id, underlying_id, expiry_date, has_futures, "
        "has_options, contract_id_lo, contract_id_hi, expiry_cycle_source, discovered_at) "
        "VALUES (nextval('seq_expiry_id'), ?, ?, FALSE, TRUE, ?, ?, 'derived', now())",
        [underlying_id, expiry, lo, hi],
    )


def test_the_same_expiry_gets_the_same_block_twice(store):
    register_underlying(store)
    expiry = date(2025, 3, 27)
    seed_expiry(store, 1, expiry)
    first = allocate_contract_block(store.connection, 1, expiry, 482)
    store.connection.execute(
        "UPDATE dim_expiry SET contract_id_lo = ?, contract_id_hi = ? WHERE expiry_date = ?",
        [first.lo, first.hi, expiry],
    )
    second = allocate_contract_block(store.connection, 1, expiry, 482)
    assert (second.lo, second.hi) == (first.lo, first.hi)
    assert second.created is False


def test_two_expiries_get_disjoint_blocks(store):
    register_underlying(store)
    march, april = date(2025, 3, 27), date(2025, 4, 24)
    seed_expiry(store, 1, march)
    seed_expiry(store, 1, april)
    first = allocate_contract_block(store.connection, 1, march, 482)
    store.connection.execute(
        "UPDATE dim_expiry SET contract_id_lo = ?, contract_id_hi = ? WHERE expiry_date = ?",
        [first.lo, first.hi, march],
    )
    second = allocate_contract_block(store.connection, 1, april, 900)
    assert second.lo > first.hi
    assert second.lo == first.hi + 1


def test_an_outgrown_block_is_replaced_and_reports_the_old_range(store):
    register_underlying(store)
    expiry = date(2025, 3, 27)
    seed_expiry(store, 1, expiry, lo=1024, hi=2047)
    grown = allocate_contract_block(store.connection, 1, expiry, 1500)
    assert grown.created is True
    assert grown.previous == (1024, 2047)
    assert grown.size == 2048
    assert grown.lo > 2047


def test_a_sequence_behind_the_catalog_never_overlaps_a_recorded_block(store):
    """A restored backup can leave the sequence behind the blocks the catalog already records."""
    register_underlying(store)
    seed_expiry(store, 1, date(2025, 3, 27), lo=1024, hi=4095)
    lo, hi = reserve_block(store.connection, 1)
    assert lo == 4096
    assert hi == 5119


# -- ordering and assignment ----------------------------------------------


def test_ids_ascend_with_strike_and_pair_the_two_rights():
    allocation = BlockAllocation(lo=1024, hi=2047, created=True)
    items = chain([23100, 22900, 23000])
    assigned = assign_contract_ids(allocation, items)

    ordered = order_contracts(items)
    ids = [assigned[item.fyers_symbol] for item in ordered]
    assert ids == list(range(1024, 1024 + len(items)))
    assert [(item.strike, item.option_type) for item in ordered] == [
        (Decimal("22900"), "CE"),
        (Decimal("22900"), "PE"),
        (Decimal("23000"), "CE"),
        (Decimal("23000"), "PE"),
        (Decimal("23100"), "CE"),
        (Decimal("23100"), "PE"),
    ]


def test_a_strike_band_is_a_contiguous_id_range():
    allocation = BlockAllocation(lo=1024, hi=2047, created=True)
    strikes = list(range(22000, 24001, 100))
    assigned = assign_contract_ids(allocation, chain(strikes))
    band = sorted(
        contract_id
        for symbol, contract_id in assigned.items()
        if any(str(strike) in symbol for strike in range(22900, 23201, 100))
    )
    assert band == list(range(min(band), min(band) + len(band)))


def test_futures_take_the_front_of_the_block_so_options_stay_ordered():
    allocation = BlockAllocation(lo=1024, hi=2047, created=True)
    items = [Item("NSE:NIFTY25MARFUT", kind="FUT", klass="FUTIDX")] + chain([23000, 22900])
    assigned = assign_contract_ids(allocation, items)
    assert assigned["NSE:NIFTY25MARFUT"] == 1024
    assert assigned["NSE:NIFTY25MAR22900CE"] == 1025


def test_existing_ids_survive_a_rediscovery_that_adds_a_strike():
    allocation = BlockAllocation(lo=1024, hi=2047, created=True)
    first = chain([23000, 23100])
    original = assign_contract_ids(allocation, first)

    second = first + chain([22900])
    updated = assign_contract_ids(allocation, second, original)

    for symbol, contract_id in original.items():
        assert updated[symbol] == contract_id
    assert len(set(updated.values())) == len(updated)
    assert all(allocation.contains(value) for value in updated.values())


def test_a_fresh_block_renumbers_every_symbol():
    old = BlockAllocation(lo=1024, hi=2047, created=True)
    items = chain([23000, 23100])
    original = assign_contract_ids(old, items)

    new = BlockAllocation(lo=4096, hi=6143, created=True, previous=(1024, 2047))
    moved = assign_contract_ids(new, items, original)
    assert set(moved.values()).isdisjoint(set(original.values()))
    assert min(moved.values()) == 4096


# -- restart stability -----------------------------------------------------


def test_allocation_is_stable_across_a_close_and_reopen(tmp_path):
    path = tmp_path / "market.duckdb"
    expiry = date(2025, 3, 27)

    store = DuckStore(path, app_version="0.0.0-test")
    store.open()
    register_underlying(store)
    seed_expiry(store, 1, expiry)
    first = allocate_contract_block(store.connection, 1, expiry, 482)
    store.connection.execute(
        "UPDATE dim_expiry SET contract_id_lo = ?, contract_id_hi = ? WHERE expiry_date = ?",
        [first.lo, first.hi, expiry],
    )
    next_unit = sequence_next_unit(store.connection)
    store.close()

    reopened = DuckStore(path, app_version="0.0.0-test")
    reopened.open()
    try:
        again = allocate_contract_block(reopened.connection, 1, expiry, 482)
        assert (again.lo, again.hi) == (first.lo, first.hi)
        assert sequence_next_unit(reopened.connection) == next_unit
        fresh_lo, _ = reserve_block(reopened.connection, 1)
        assert fresh_lo == first.hi + 1
    finally:
        reopened.close()
