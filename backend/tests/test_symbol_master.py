"""The symbol master snapshot, its streaming reader and the type 2 dimension.

Every fixture under tests/fixtures/sym_master_*.json is a verbatim slice of the real public
files, recorded on 2026-09-10, so the shapes exercised here are the vendor's own and not an
invention. Nothing in this file touches the network: the fetch tests drive httpx through a
MockTransport.

These tests use asyncio.run rather than an async test plugin, matching test_duck_writes.py, so
they stand alone ahead of the shared conftest the verification work item owns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from expirymanager.brokers.fyers import symbol_master as sm
from expirymanager.brokers.fyers.roots import UnderlyingRoot, builtin_registry
from expirymanager.brokers.fyers.symbology import SymbolParseError, parse_symbol
from expirymanager.db.duck import DuckStore
from expirymanager.db.reader import DuckReader
from expirymanager.db.writer import DuckWriter

FIXTURES = Path(__file__).parent / "fixtures"

NSE_FO = FIXTURES / "sym_master_NSE_FO.json"
NSE_CM = FIXTURES / "sym_master_NSE_CM.json"
BSE_FO = FIXTURES / "sym_master_BSE_FO.json"
BSE_CM = FIXTURES / "sym_master_BSE_CM.json"

DAY_ONE = date(2026, 9, 10)
DAY_TWO = date(2026, 9, 11)

NIFTY_FUT = "NSE:NIFTY26SEPFUT"


@pytest.fixture
def store(tmp_path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    yield store
    store.close()


def fetched(file: str, path: Path, sha: str = "0" * 64) -> sm.FetchedMaster:
    return sm.FetchedMaster(
        file=file,
        url=sm.master_url(file),
        path=path,
        sha256=sha,
        byte_size=path.stat().st_size,
        fetched_at=datetime(2026, 9, 10, 2, 45, tzinfo=timezone.utc),
    )


def run(store, coro_factory):
    """Start the writer, run one coroutine against it, stop the writer."""

    async def main():
        writer = DuckWriter(store)
        await writer.start()
        reader = DuckReader(store)
        try:
            return await coro_factory(writer, reader)
        finally:
            await writer.stop()

    return asyncio.run(main())


def load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_variant(tmp_path: Path, name: str, members: dict) -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(members), encoding="utf-8")
    return target


def fytoken_of(ticker: str, path: Path = NSE_FO) -> str:
    return load_fixture(path)[ticker]["fyToken"]


# ---------------------------------------------------------------------------
# The public file itself
# ---------------------------------------------------------------------------


def test_the_seven_master_urls_are_the_documented_public_ones():
    assert len(sm.MASTER_FILES) == 7
    assert sm.master_url("NSE_FO") == (
        "https://public.fyers.in/sym_details/NSE_FO_sym_master.json"
    )
    for file in sm.MASTER_FILES:
        assert sm.master_url(file).startswith("https://public.fyers.in/sym_details/")
        assert sm.exchange_of(file) in ("NSE", "BSE", "MCX")
        assert sm.segment_of(file) in ("CM", "FO", "CD", "COM")


def test_an_unknown_file_name_is_refused():
    with pytest.raises(sm.MasterFileUnknown):
        sm.master_url("NSE_XX")


def test_the_fetch_sends_no_authorization_header_and_costs_no_budget(tmp_path):
    """The master is a public file. A token is not required and none must be offered."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=NSE_FO.read_bytes())

    async def main():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await sm.fetch_master("NSE_FO", dest_dir=tmp_path, client=client)

    result = asyncio.run(main())
    assert result.path.exists()
    assert result.byte_size == NSE_FO.stat().st_size
    assert result.sha256 == hashlib.sha256(NSE_FO.read_bytes()).hexdigest()

    assert len(seen) == 1
    request = seen[0]
    assert "authorization" not in {k.lower() for k in request.headers}
    assert "cookie" not in {k.lower() for k in request.headers}
    assert str(request.url) == sm.master_url("NSE_FO")


def test_a_partial_download_never_lands_under_the_final_name(tmp_path):
    """The rename is what stops a half file being read as a short file."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped")

    async def main():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await sm.fetch_master("NSE_FO", dest_dir=tmp_path, client=client)

    with pytest.raises(httpx.ReadError):
        asyncio.run(main())
    assert not (tmp_path / "NSE_FO_sym_master.json").exists()


# ---------------------------------------------------------------------------
# Streaming and projection
# ---------------------------------------------------------------------------


def test_the_streaming_reader_returns_every_member_the_file_holds():
    members = load_fixture(NSE_FO)
    streamed = dict(sm.iter_master_members(NSE_FO))
    assert streamed == members


def test_the_streaming_reader_is_correct_at_a_chunk_size_of_one_byte():
    """Member boundaries must not depend on where the read buffer happens to break."""
    members = load_fixture(BSE_CM)
    streamed = dict(sm.iter_master_members(BSE_CM, chunk_bytes=1))
    assert streamed == members


def test_a_truncated_file_raises_rather_than_yielding_a_short_master(tmp_path):
    body = NSE_FO.read_text(encoding="utf-8")
    cut = tmp_path / "cut.json"
    cut.write_text(body[: len(body) // 2], encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        list(sm.iter_master_members(cut))


def test_an_empty_object_streams_as_no_members(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert list(sm.iter_master_members(empty)) == []


def test_a_future_row_projects_the_sizing_facts_and_drops_the_strike_sentinel():
    raw = load_fixture(NSE_FO)[NIFTY_FUT]
    row = sm.master_row(NIFTY_FUT, raw)
    assert row.symbol_ticker == NIFTY_FUT
    assert row.fytoken == raw["fyToken"]
    assert row.min_lot_size == raw["minLotSize"]
    assert row.tick_size == Decimal(str(raw["tickSize"])).quantize(Decimal("0.0001"))
    assert row.qty_freeze == int(raw["qtyFreeze"])
    assert row.under_symbol == "NIFTY"
    # Futures carry -1.0 in strikePrice and the filler in optType. Both mean not applicable.
    assert row.strike_price is None
    assert row.option_type is None
    assert row.ex_series is None


def test_an_option_row_keeps_its_strike_and_right():
    members = load_fixture(NSE_FO)
    ticker, raw = next((k, v) for k, v in members.items() if v["optType"] == "CE")
    row = sm.master_row(ticker, raw)
    assert row.option_type == "CE"
    assert row.strike_price == Decimal(str(raw["strikePrice"])).quantize(Decimal("0.0001"))
    assert row.expiry_date is not None


def test_the_expiry_timestamp_is_read_as_an_ist_calendar_date():
    """1790676600 is 15:40 IST on 2026-09-29, the close of that session."""
    assert sm.expiry_from_epoch("1790676600") == date(2026, 9, 29)
    assert sm.expiry_from_epoch("") is None
    assert sm.expiry_from_epoch("0") is None
    assert sm.expiry_from_epoch(None) is None


def test_the_row_hash_ignores_the_fields_that_move_every_session():
    raw = load_fixture(NSE_FO)[NIFTY_FUT]
    quiet = dict(raw, previousClose=1.0, upperPrice=2.0, lowerPrice=3.0, tradeStatus=0)
    assert sm.master_row(NIFTY_FUT, quiet).row_hash == sm.master_row(NIFTY_FUT, raw).row_hash
    loud = dict(raw, minLotSize=int(raw["minLotSize"]) + 5)
    assert sm.master_row(NIFTY_FUT, loud).row_hash != sm.master_row(NIFTY_FUT, raw).row_hash


def test_the_two_not_applicable_spellings_hash_the_same():
    """NSE sends the two letter filler where BSE sends an empty string."""
    raw = load_fixture(NSE_FO)[NIFTY_FUT]
    nse = sm.master_row(NIFTY_FUT, dict(raw, exSeries="XX", optType="XX"))
    bse = sm.master_row(NIFTY_FUT, dict(raw, exSeries="", optType=""))
    assert nse.row_hash == bse.row_hash


def test_a_member_with_no_fytoken_is_refused():
    with pytest.raises(sm.SymbolMasterError):
        sm.master_row("NSE:X26SEPFUT", {"symTicker": "NSE:X26SEPFUT"})


# ---------------------------------------------------------------------------
# The type 2 dimension
# ---------------------------------------------------------------------------


def test_a_first_load_opens_one_current_row_per_instrument(store):
    async def go(writer, reader):
        result = await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        total = await reader.fetch_value("SELECT count(*) FROM dim_instrument_master")
        open_rows = await reader.fetch_value(
            "SELECT count(*) FROM dim_instrument_master WHERE valid_to IS NULL"
        )
        starts = await reader.fetch_all(
            "SELECT DISTINCT valid_from FROM dim_instrument_master"
        )
        return result, total, open_rows, starts

    result, total, open_rows, starts = run(store, go)
    expected = len(load_fixture(NSE_FO))
    assert result.staged == expected
    assert result.inserted == expected
    assert result.versioned == result.closed == result.amended == 0
    assert total == open_rows == expected
    assert starts == [(DAY_ONE,)]


def test_staging_is_batched_and_a_small_batch_size_changes_nothing_but_the_flush_count(store):
    """Guards a measured regression.

    Staging row by row through executemany opened a fresh DuckDB column segment per statement
    and exhausted a 4 GB memory limit part way through the real 78,585 row NSE_FO file. Rows now
    go in as Arrow batches, one INSERT per batch, so the batch size must not be able to change
    the result.
    """

    async def go(writer, reader):
        one = await sm.apply_master_file(
            writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE, batch_size=7
        )
        rows = await reader.fetch_value("SELECT count(*) FROM dim_instrument_master")
        return one, rows

    result, rows = run(store, go)
    expected = len(load_fixture(NSE_FO))
    assert result.staged == result.inserted == expected
    assert rows == expected


def test_the_staging_batch_carries_the_pinned_schema():
    rows = [sm.master_row(k, v) for k, v in list(load_fixture(NSE_FO).items())[:3]]
    batch = sm.stage_batch(rows)
    assert batch.num_rows == 3
    assert batch.schema.names == list(sm.STAGE_COLUMNS)
    # The decimal scales have to match the dimension exactly, otherwise DuckDB rounds on the
    # way in and a tick size that never moved looks like a change.
    assert str(batch.schema.field("tick_size").type) == "decimal128(9, 4)"
    assert str(batch.schema.field("strike_price").type) == "decimal128(12, 4)"


def test_the_first_load_records_a_snapshot_row(store):
    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO, "a" * 64), as_of=DAY_ONE)
        return await reader.fetch_one(
            "SELECT snapshot_date, file, url, sha256, row_count, byte_size "
            "FROM symbol_master_snapshot"
        )

    row = run(store, go)
    assert row[0] == DAY_ONE
    assert row[1] == "NSE_FO"
    assert row[2] == sm.master_url("NSE_FO")
    assert row[3] == "a" * 64
    assert row[4] == len(load_fixture(NSE_FO))
    assert row[5] == NSE_FO.stat().st_size


def test_a_reload_with_no_changes_adds_no_rows(store):
    async def go(writer, reader):
        first = await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        second = await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_TWO)
        total = await reader.fetch_value("SELECT count(*) FROM dim_instrument_master")
        closed = await reader.fetch_value(
            "SELECT count(*) FROM dim_instrument_master WHERE valid_to IS NOT NULL"
        )
        return first, second, total, closed

    first, second, total, closed = run(store, go)
    assert second.new_rows == 0
    assert second.inserted == second.versioned == second.closed == second.amended == 0
    assert second.unchanged == second.staged == first.staged
    assert total == first.staged
    assert closed == 0


def test_a_third_reload_is_still_quiet(store):
    """Idempotency has to hold past the second run, not only on the first repeat."""

    async def go(writer, reader):
        for day in (DAY_ONE, DAY_TWO, date(2026, 9, 12)):
            await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=day)
        return await reader.fetch_value("SELECT count(*) FROM dim_instrument_master")

    assert run(store, go) == len(load_fixture(NSE_FO))


def test_a_lot_size_change_opens_exactly_one_new_row_and_closes_the_old_one(store, tmp_path):
    members = load_fixture(NSE_FO)
    token = members[NIFTY_FUT]["fyToken"]
    before = members[NIFTY_FUT]["minLotSize"]
    members[NIFTY_FUT] = dict(members[NIFTY_FUT], minLotSize=before + 10)
    day_two_file = write_variant(tmp_path, "NSE_FO_day2.json", members)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        result = await sm.apply_master_file(
            writer, fetched("NSE_FO", day_two_file, "b" * 64), as_of=DAY_TWO
        )
        versions = await reader.fetch_all(
            "SELECT valid_from, valid_to, min_lot_size FROM dim_instrument_master "
            "WHERE fytoken = ? ORDER BY valid_from",
            [token],
        )
        untouched = await reader.fetch_value(
            "SELECT count(*) FROM dim_instrument_master WHERE fytoken <> ?", [token]
        )
        return result, versions, untouched

    result, versions, untouched = run(store, go)
    assert result.versioned == 1
    assert result.inserted == 0
    assert result.closed == 0
    assert result.unchanged == result.staged - 1
    assert versions == [
        (DAY_ONE, DAY_TWO, before),
        (DAY_TWO, None, before + 10),
    ]
    # One changed instrument must cost exactly one new row across the whole file.
    assert untouched == len(members) - 1


def test_a_tick_size_change_versions_the_same_way(store, tmp_path):
    members = load_fixture(NSE_FO)
    token = members[NIFTY_FUT]["fyToken"]
    members[NIFTY_FUT] = dict(members[NIFTY_FUT], tickSize=0.25)
    variant = write_variant(tmp_path, "NSE_FO_tick.json", members)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_FO", variant, "c" * 64), as_of=DAY_TWO)
        return await reader.fetch_all(
            "SELECT valid_from, valid_to, tick_size FROM dim_instrument_master "
            "WHERE fytoken = ? ORDER BY valid_from",
            [token],
        )

    versions = run(store, go)
    assert len(versions) == 2
    assert versions[0][1] == DAY_TWO
    assert versions[1][2] == Decimal("0.2500")


def test_a_contract_that_leaves_the_file_has_its_row_closed(store, tmp_path):
    """This is the expiry path. A contract that expired is gone from the master forever."""
    members = load_fixture(NSE_FO)
    gone_ticker = next(k for k in members if k != NIFTY_FUT)
    gone_token = members[gone_ticker]["fyToken"]
    del members[gone_ticker]
    variant = write_variant(tmp_path, "NSE_FO_gone.json", members)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        result = await sm.apply_master_file(
            writer, fetched("NSE_FO", variant, "d" * 64), as_of=DAY_TWO
        )
        rows = await reader.fetch_all(
            "SELECT valid_from, valid_to FROM dim_instrument_master WHERE fytoken = ?",
            [gone_token],
        )
        still_open = await reader.fetch_value(
            "SELECT count(*) FROM dim_instrument_master WHERE valid_to IS NULL"
        )
        return result, rows, still_open

    result, rows, still_open = run(store, go)
    assert result.closed == 1
    assert result.new_rows == 0
    assert rows == [(DAY_ONE, DAY_TWO)]
    assert still_open == len(members)


def test_one_file_never_closes_another_file_s_rows(store):
    """The diff is scoped to the exchange and segment the staged rows carry."""

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_CM", NSE_CM), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("BSE_FO", BSE_FO), as_of=DAY_ONE)
        # A second day of NSE_FO alone must leave the CM and BSE rows current.
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_TWO)
        return await reader.fetch_all(
            "SELECT exchange_code, segment_code, count(*) FILTER (WHERE valid_to IS NULL) "
            "FROM dim_instrument_master GROUP BY 1, 2 ORDER BY 1, 2"
        )

    rows = run(store, go)
    counts = {(r[0], r[1]): r[2] for r in rows}
    assert counts[(10, 11)] == len(load_fixture(NSE_FO))
    assert counts[(10, 10)] == len(load_fixture(NSE_CM))
    assert counts[(12, 11)] == len(load_fixture(BSE_FO))


def test_a_change_on_the_same_day_amends_in_place_rather_than_opening_a_zero_width_row(
    store, tmp_path
):
    members = load_fixture(NSE_FO)
    token = members[NIFTY_FUT]["fyToken"]
    members[NIFTY_FUT] = dict(members[NIFTY_FUT], minLotSize=999)
    variant = write_variant(tmp_path, "NSE_FO_sameday.json", members)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        result = await sm.apply_master_file(
            writer, fetched("NSE_FO", variant, "e" * 64), as_of=DAY_ONE
        )
        versions = await reader.fetch_all(
            "SELECT valid_from, valid_to, min_lot_size FROM dim_instrument_master "
            "WHERE fytoken = ? ORDER BY valid_from",
            [token],
        )
        return result, versions

    result, versions = run(store, go)
    assert result.amended == 1
    assert result.versioned == 0
    assert result.new_rows == 0
    assert versions == [(DAY_ONE, None, 999)]


def test_a_relisted_instrument_opens_a_fresh_version(store, tmp_path):
    members = load_fixture(NSE_FO)
    ticker = NIFTY_FUT
    token = members[ticker]["fyToken"]
    without = {k: v for k, v in members.items() if k != ticker}
    absent = write_variant(tmp_path, "NSE_FO_absent.json", without)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_FO", absent, "f" * 64), as_of=DAY_TWO)
        await sm.apply_master_file(
            writer, fetched("NSE_FO", NSE_FO, "g" * 64), as_of=date(2026, 9, 12)
        )
        return await reader.fetch_all(
            "SELECT valid_from, valid_to FROM dim_instrument_master WHERE fytoken = ? "
            "ORDER BY valid_from",
            [token],
        )

    assert run(store, go) == [(DAY_ONE, DAY_TWO), (date(2026, 9, 12), None)]


def test_a_short_file_is_refused_and_writes_nothing(store, tmp_path):
    members = load_fixture(NSE_FO)
    kept = dict(list(members.items())[:5])
    short = write_variant(tmp_path, "NSE_FO_short.json", kept)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        with pytest.raises(sm.MasterFileTruncated) as excinfo:
            await sm.apply_master_file(writer, fetched("NSE_FO", short, "h" * 64), as_of=DAY_TWO)
        open_rows = await reader.fetch_value(
            "SELECT count(*) FROM dim_instrument_master WHERE valid_to IS NULL"
        )
        snapshots = await reader.fetch_value("SELECT count(*) FROM symbol_master_snapshot")
        return excinfo.value, open_rows, snapshots

    error, open_rows, snapshots = run(store, go)
    assert error.staged == 5
    assert error.previous == len(load_fixture(NSE_FO))
    # The transaction rolled back whole: no row closed, no snapshot recorded.
    assert open_rows == len(load_fixture(NSE_FO))
    assert snapshots == 1


def test_the_shrink_guard_does_not_block_a_genuine_first_load(store, tmp_path):
    tiny = write_variant(tmp_path, "tiny.json", dict(list(load_fixture(NSE_FO).items())[:2]))

    async def go(writer, reader):
        return await sm.apply_master_file(writer, fetched("NSE_FO", tiny), as_of=DAY_ONE)

    assert run(store, go).inserted == 2


# ---------------------------------------------------------------------------
# Point in time
# ---------------------------------------------------------------------------


def test_a_point_in_time_lookup_returns_the_lot_size_true_on_that_date(store, tmp_path):
    members = load_fixture(NSE_FO)
    token = members[NIFTY_FUT]["fyToken"]
    before = members[NIFTY_FUT]["minLotSize"]
    members[NIFTY_FUT] = dict(members[NIFTY_FUT], minLotSize=before + 10)
    day_two_file = write_variant(tmp_path, "NSE_FO_pit.json", members)
    members[NIFTY_FUT] = dict(members[NIFTY_FUT], minLotSize=before + 25)
    day_five_file = write_variant(tmp_path, "NSE_FO_pit2.json", members)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        await sm.apply_master_file(
            writer, fetched("NSE_FO", day_two_file, "i" * 64), as_of=DAY_TWO
        )
        await sm.apply_master_file(
            writer, fetched("NSE_FO", day_five_file, "j" * 64), as_of=date(2026, 9, 15)
        )
        return {
            day: await sm.fact_on(reader, token, day)
            for day in (
                date(2026, 9, 9),
                DAY_ONE,
                DAY_TWO,
                date(2026, 9, 14),
                date(2026, 9, 15),
                date(2026, 12, 31),
            )
        }

    facts = run(store, go)
    # Before the dimension knew about the instrument there is deliberately no answer, rather
    # than today's value pretending to have been true then.
    assert facts[date(2026, 9, 9)] is None
    assert facts[DAY_ONE].min_lot_size == before
    assert facts[DAY_TWO].min_lot_size == before + 10
    assert facts[date(2026, 9, 14)].min_lot_size == before + 10
    assert facts[date(2026, 9, 15)].min_lot_size == before + 25
    assert facts[date(2026, 12, 31)].min_lot_size == before + 25


def test_valid_to_is_exclusive_so_exactly_one_version_answers_any_date(store, tmp_path):
    members = load_fixture(NSE_FO)
    token = members[NIFTY_FUT]["fyToken"]
    members[NIFTY_FUT] = dict(members[NIFTY_FUT], minLotSize=1)
    variant = write_variant(tmp_path, "NSE_FO_edge.json", members)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_FO", variant, "k" * 64), as_of=DAY_TWO)
        counts = []
        for day in (DAY_ONE, DAY_TWO, date(2026, 9, 20)):
            rows = await reader.fetch_all(sm.point_in_time_sql(), [token, day, day])
            counts.append(len(rows))
        return counts

    assert run(store, go) == [1, 1, 1]


def test_a_batch_lookup_answers_many_instruments_at_one_date(store):
    tickers = list(load_fixture(NSE_FO))[:4]
    tokens = [fytoken_of(t) for t in tickers]

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        return await sm.facts_on(reader, tokens, DAY_ONE)

    facts = run(store, go)
    assert set(facts) == set(tokens)
    assert all(f.min_lot_size is not None for f in facts.values())


def test_an_expired_contract_still_answers_for_the_days_it_was_listed(store, tmp_path):
    """The whole reason the dimension exists: the value survives the contract."""
    members = load_fixture(NSE_FO)
    token = members[NIFTY_FUT]["fyToken"]
    lot = members[NIFTY_FUT]["minLotSize"]
    without = {k: v for k, v in members.items() if k != NIFTY_FUT}
    variant = write_variant(tmp_path, "NSE_FO_expired.json", without)

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_FO", variant, "l" * 64), as_of=DAY_TWO)
        return (
            await sm.fact_on(reader, token, DAY_ONE),
            await sm.fact_on(reader, token, DAY_TWO),
        )

    while_listed, after_expiry = run(store, go)
    assert while_listed.min_lot_size == lot
    assert after_expiry is None


# ---------------------------------------------------------------------------
# Root registry rebuild and the character class
# ---------------------------------------------------------------------------


def test_the_rebuild_resolves_each_root_to_the_cash_ticker_the_endpoints_accept(store):
    async def go(writer, reader):
        for name, path in (
            ("NSE_CM", NSE_CM),
            ("NSE_FO", NSE_FO),
            ("BSE_CM", BSE_CM),
            ("BSE_FO", BSE_FO),
        ):
            await sm.apply_master_file(writer, fetched(name, path), as_of=DAY_ONE)
        return await sm.rebuild_root_registry(reader)

    rebuild = run(store, go)
    by_root = {c.root: c for c in rebuild.candidates}
    # Nothing in the symbol string connects BANKNIFTY to NSE:NIFTYBANK-INDEX. Only the
    # under_fytoken join does.
    assert by_root["BANKNIFTY"].fyers_symbol == "NSE:NIFTYBANK-INDEX"
    assert by_root["BANKNIFTY"].instrument_kind == "INDEX"
    assert by_root["NIFTY"].fyers_symbol == "NSE:NIFTY50-INDEX"
    assert by_root["RELIANCE"].fyers_symbol == "NSE:RELIANCE-EQ"
    assert by_root["RELIANCE"].instrument_kind == "EQUITY"
    assert by_root["BANKEX"].exchange == "BSE"
    assert by_root["BANKEX"].fyers_symbol == "BSE:BANKEX-INDEX"


def test_the_rebuild_adds_the_new_roots_without_disturbing_the_builtin_seeds(store):
    async def go(writer, reader):
        for name, path in (
            ("NSE_CM", NSE_CM),
            ("NSE_FO", NSE_FO),
            ("BSE_CM", BSE_CM),
            ("BSE_FO", BSE_FO),
        ):
            await sm.apply_master_file(writer, fetched(name, path), as_of=DAY_ONE)
        return await sm.rebuild_root_registry(reader)

    rebuild = run(store, go)
    seeds = builtin_registry()
    for entry in seeds:
        rebuilt = rebuild.registry.get(entry.root)
        assert rebuilt is not None
        assert rebuilt.fyers_symbol == entry.fyers_symbol
        assert rebuilt.is_builtin
    assert "BANKEX" in rebuild.added
    assert "M&M" in rebuild.added
    assert "NIFTY" not in rebuild.added


def test_hyphen_and_ampersand_roots_survive_the_round_trip(store):
    """The BUILD-PLAN open question, answered from the file rather than assumed."""

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_CM", NSE_CM), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        return await sm.rebuild_root_registry(reader)

    rebuild = run(store, go)
    for root in ("BAJAJ-AUTO", "NAM-INDIA", "M&M", "GVT&D"):
        entry = rebuild.registry.get(root)
        assert entry is not None, root
        # A registered root is what makes the parser pick the right decomposition.
        parsed = parse_symbol(f"NSE:{root}26SEPFUT", registry=rebuild.registry)
        assert parsed.root == root
        assert parsed.kind == "FUT"


def test_a_digit_leading_root_is_recorded_as_rejected_rather_than_crashing_the_run(store):
    """360ONE is a real NSE_FO root and neither roots.py nor symbology.py accepts it."""

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_CM", NSE_CM), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        return await sm.rebuild_root_registry(reader)

    rebuild = run(store, go)
    rejected = {r.root for r in rebuild.rejected}
    assert "360ONE" in rejected
    assert rebuild.registry.get("360ONE") is None
    # The rest of the file still landed.
    assert len(rebuild.candidates) >= 6
    with pytest.raises((SymbolParseError, ValueError)):
        parse_symbol("NSE:360ONE26SEPFUT", registry=rebuild.registry)


def test_the_observed_character_class_names_the_outliers():
    observed = sm.observed_root_charclass(
        ["NIFTY", "BAJAJ-AUTO", "M&M", "360ONE"], source_file="NSE_FO"
    )
    assert observed.source_file == "NSE_FO"
    assert observed.root_count == 4
    assert "&" in observed.body_chars
    assert "-" in observed.body_chars
    assert observed.outliers == ("360ONE",)
    assert observed.accepted_pattern == sm.ACCEPTED_ROOT_PATTERN
    assert "3" in observed.first_chars


def test_feeding_the_default_registry_leaves_the_seeds_alone(store):
    from expirymanager.brokers.fyers.roots import default_registry

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_CM", NSE_CM), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        return await sm.rebuild_root_registry(reader)

    rebuild = run(store, go)
    registry = default_registry()
    before = registry.get("NIFTY")
    added = sm.feed_default_registry(rebuild)
    try:
        assert "M&M" in added
        assert registry.get("M&M") is not None
        assert registry.get("NIFTY") == before
    finally:
        for root in added:
            registry.unregister(root)


# ---------------------------------------------------------------------------
# The scheduled entry point
# ---------------------------------------------------------------------------


def _mock_client(bodies: dict[str, bytes], calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1].replace("_sym_master.json", "")
        if calls is not None:
            calls.append(name)
        body = bodies.get(name)
        if body is None:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


FOUR_FILES = ("NSE_CM", "NSE_FO", "BSE_CM", "BSE_FO")


def _bodies() -> dict[str, bytes]:
    return {
        "NSE_CM": NSE_CM.read_bytes(),
        "NSE_FO": NSE_FO.read_bytes(),
        "BSE_CM": BSE_CM.read_bytes(),
        "BSE_FO": BSE_FO.read_bytes(),
    }


def test_the_daily_entry_point_loads_every_file_and_rebuilds_the_roots(store, tmp_path):
    calls: list[str] = []

    async def go(writer, reader):
        async with _mock_client(_bodies(), calls) as client:
            return await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_ONE,
                client=client,
            )

    result = run(store, go)
    assert result.ok
    assert calls == list(FOUR_FILES)
    assert {f.file for f in result.files} == set(FOUR_FILES)
    assert result.rows_staged == sum(
        len(load_fixture(p)) for p in (NSE_CM, NSE_FO, BSE_CM, BSE_FO)
    )
    assert result.new_rows == result.rows_staged
    assert result.rebuild is not None
    assert "BANKEX" in result.rebuild.added
    # The downloaded copies are cleaned up: they are 81 MB each in production.
    assert not list((tmp_path / "work").glob("*_sym_master.json"))


def test_the_entry_point_records_the_root_character_class_in_meta(store, tmp_path):
    async def go(writer, reader):
        async with _mock_client(_bodies()) as client:
            await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_ONE,
                client=client,
            )
        return await reader.fetch_value(
            "SELECT value FROM meta WHERE key = ?", [sm.META_ROOT_CHARCLASS]
        )

    recorded = json.loads(run(store, go))
    assert recorded["source_file"] == "NSE_FO"
    assert recorded["accepted_pattern"] == sm.ACCEPTED_ROOT_PATTERN
    assert "360ONE" in recorded["outliers"]
    assert "BAJAJ-AUTO" in recorded["examples"]


def test_a_second_run_on_unchanged_files_skips_the_diff_but_still_records_the_day(
    store, tmp_path
):
    async def go(writer, reader):
        async with _mock_client(_bodies()) as client:
            first = await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_ONE,
                client=client,
            )
            second = await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_TWO,
                client=client,
            )
        days = await reader.fetch_all(
            "SELECT snapshot_date, count(*) FROM symbol_master_snapshot GROUP BY 1 ORDER BY 1"
        )
        total = await reader.fetch_value("SELECT count(*) FROM dim_instrument_master")
        return first, second, days, total

    first, second, days, total = run(store, go)
    assert all(f.skipped for f in second.files)
    assert second.new_rows == 0
    assert days == [(DAY_ONE, 4), (DAY_TWO, 4)]
    assert total == first.rows_staged


def test_running_the_diff_on_unchanged_content_is_also_quiet(store, tmp_path):
    """The sha256 skip is an optimisation, not the thing that makes a reload quiet."""

    async def go(writer, reader):
        async with _mock_client(_bodies()) as client:
            await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_ONE,
                client=client,
                skip_unchanged=False,
            )
            second = await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_TWO,
                client=client,
                skip_unchanged=False,
            )
        return second

    second = run(store, go)
    assert not any(f.skipped for f in second.files)
    assert second.new_rows == 0
    assert second.closed_rows == 0


def test_one_failing_file_does_not_cost_the_others(store, tmp_path):
    """A day that is missed is missed permanently, so six files beat none."""
    bodies = _bodies()
    del bodies["BSE_FO"]

    async def go(writer, reader):
        async with _mock_client(bodies) as client:
            return await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_ONE,
                client=client,
            )

    result = run(store, go)
    assert not result.ok
    assert [name for name, _ in result.failures] == ["BSE_FO"]
    assert {f.file for f in result.files} == {"NSE_CM", "NSE_FO", "BSE_CM"}
    assert result.rows_staged > 0


def test_the_run_summary_lands_in_meta(store, tmp_path):
    async def go(writer, reader):
        async with _mock_client(_bodies()) as client:
            await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_ONE,
                client=client,
            )
        return await reader.fetch_value(
            "SELECT value FROM meta WHERE key = ?", [sm.META_LAST_RUN]
        )

    summary = json.loads(run(store, go))
    assert summary["as_of"] == DAY_ONE.isoformat()
    assert len(summary["files"]) == 4
    assert summary["failures"] == []
    assert any(r["root"] == "360ONE" for r in summary["roots_rejected"])


def test_mcx_is_not_offered_as_an_underlying_by_the_rebuild(store, tmp_path):
    """MCX answered 422 on every expired form probed on 2026-09-09."""

    async def go(writer, reader):
        async with _mock_client(_bodies()) as client:
            result = await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_ONE,
                client=client,
            )
        return result

    result = run(store, go)
    assert result.rebuild is not None
    assert all(c.exchange != "MCX" for c in result.rebuild.candidates)


def test_the_run_still_works_with_no_token_present(store, tmp_path):
    """Nothing in this path reads a token, a session or the governor."""

    async def go(writer, reader):
        async with _mock_client(_bodies()) as client:
            return await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=("NSE_FO",),
                as_of=DAY_ONE,
                client=client,
                rebuild_roots=False,
            )

    result = run(store, go)
    assert result.ok
    assert result.files[0].inserted == len(load_fixture(NSE_FO))


def test_a_candidate_projects_onto_a_registry_entry(store):
    candidate = sm.RootCandidate(
        root="BANKEX",
        fyers_symbol="BSE:BANKEX-INDEX",
        display_name="BANKEX",
        exchange="BSE",
        instrument_kind="INDEX",
        derivative_segment="FO",
        under_fytoken="121000000012",
        contract_count=5,
    )
    entry = candidate.as_underlying_root()
    assert isinstance(entry, UnderlyingRoot)
    assert entry.segment_code == 11
    assert entry.data_from.year >= 2015


# ---------------------------------------------------------------------------
# The parser cross check
# ---------------------------------------------------------------------------


def test_the_parser_audit_confirms_the_vendor_fields_for_every_root_it_can_read(store):
    """The master is the only answer key the symbology parser ever gets."""

    async def go(writer, reader):
        await sm.apply_master_file(writer, fetched("NSE_CM", NSE_CM), as_of=DAY_ONE)
        await sm.apply_master_file(writer, fetched("NSE_FO", NSE_FO), as_of=DAY_ONE)
        rebuild = await sm.rebuild_root_registry(reader)
        return await sm.audit_symbology(reader, rebuild.registry)

    audit = run(store, go)
    assert audit.checked >= 6
    # No root that parses at all may disagree with the vendor on root, right or strike.
    assert audit.disagreements == ()
    # 360ONE is the one root the parser cannot read, and it must be named rather than hidden.
    assert any(t.startswith("NSE:360ONE") for t in audit.unparseable)


# Two real members of the recorded NSE_FO slice: one weekly coded, one monthly coded.
WEEKLY_CE = ("NIFTY", "NSE:NIFTY2691519050CE", Decimal("19050"), "CE", date(2026, 9, 15))
MONTHLY_CE = ("BANKNIFTY", "NSE:BANKNIFTY26SEP72600CE", Decimal("72600"), "CE", date(2026, 9, 29))


def test_the_audit_is_clean_for_both_symbol_encodings():
    audit = sm.audit_rows_against_symbology([WEEKLY_CE, MONTHLY_CE], builtin_registry())
    assert audit.clean
    assert audit.checked == 2


def test_the_audit_reports_a_strike_that_does_not_match_the_symbol():
    wrong = (WEEKLY_CE[0], WEEKLY_CE[1], Decimal("18950"), WEEKLY_CE[3], WEEKLY_CE[4])
    audit = sm.audit_rows_against_symbology([wrong], builtin_registry())
    assert not audit.clean
    assert [d.field_name for d in audit.disagreements] == ["strike"]


def test_the_audit_reports_a_right_that_does_not_match_the_symbol():
    wrong = (WEEKLY_CE[0], WEEKLY_CE[1], WEEKLY_CE[2], "PE", WEEKLY_CE[4])
    audit = sm.audit_rows_against_symbology([wrong], builtin_registry())
    assert [d.field_name for d in audit.disagreements] == ["option_type"]


def test_the_entry_point_records_the_parser_audit_in_meta(store, tmp_path):
    async def go(writer, reader):
        async with _mock_client(_bodies()) as client:
            result = await sm.run_symbol_master_snapshot(
                writer=writer,
                reader=reader,
                work_dir=tmp_path / "work",
                files=FOUR_FILES,
                as_of=DAY_ONE,
                client=client,
            )
        stored = await reader.fetch_value(
            "SELECT value FROM meta WHERE key = ?", [sm.META_SYMBOLOGY_AUDIT]
        )
        return result, json.loads(stored)

    result, stored = run(store, go)
    assert result.audit is not None
    assert stored["checked"] == result.audit.checked
    assert stored["disagreements"] == []
