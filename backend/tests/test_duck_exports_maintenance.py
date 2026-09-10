"""Exports and maintenance.

The export tests are round trips: what DuckDB wrote is read back through DuckDB and compared
value for value against the rows that went in. A byte size assertion would prove only that a file
exists, and the risk being tested for is a price or a timestamp that changed on the way out.

The maintenance tests exercise the operations that touch the whole database at once, so each one
runs against a temporary file of its own.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import duckdb
import pytest

from expirymanager.db import exports as exports_m
from expirymanager.db import maintenance as maintenance_m
from expirymanager.db.arrow import IST_OFFSET_SECONDS, candles_to_arrow
from expirymanager.db.duck import DuckStore
from expirymanager.db.writer import CoverageRow
from expirymanager.db.writes import (
    ContractRow,
    UnderlyingRow,
    record_export,
    upsert_candle_chunk,
    upsert_contracts,
    upsert_underlying,
)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
COLUMNS_JSON = '["timestamp","open","high","low","close","volume","open_interest"]'

EXPIRY = date(2025, 3, 27)
DAY = date(2025, 3, 26)
RES_ID = 2
STRIKES = (22900, 23000)
BARS = 5

# Four decimal places, which is the whole reason prices are DECIMAL(11,4) rather than
# DECIMAL(9,2). A currency derivative ticks at 0.0025 and two places would truncate it.
PRICES = ("145.2025", "152.0075", "141.0525", "149.3075")


def utc_epoch_for_ist(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp()) - IST_OFFSET_SECONDS


def bar_time(index: int) -> datetime:
    minute = 15 + index
    return datetime(DAY.year, DAY.month, DAY.day, 9 + minute // 60, minute % 60)


def payload():
    return [
        [
            utc_epoch_for_ist(bar_time(index)),
            PRICES[0],
            PRICES[1],
            PRICES[2],
            PRICES[3],
            1000 + index,
            50000 + index,
        ]
        for index in range(BARS)
    ]


def underlying_row() -> UnderlyingRow:
    return UnderlyingRow(
        underlying_id=1,
        fyers_symbol="NSE:NIFTY50-INDEX",
        root="NIFTY",
        exchange="NSE",
        exchange_code=10,
        segment="CM",
        segment_code=10,
        instrument_kind="INDEX",
        display_name="Nifty 50",
        data_from=date(2022, 1, 3),
    )


def option_row(strike: int, right: str) -> ContractRow:
    return ContractRow(
        fyers_symbol=f"NSE:NIFTY25MAR{strike}{right}",
        kind="OPT",
        instrument_class="OPTIDX",
        exchange="NSE",
        exchange_code=10,
        segment="FO",
        segment_code=11,
        root="NIFTY",
        source_endpoint="expired_contracts",
        parse_method="regex",
        parse_confidence="exact",
        strike=Decimal(strike),
        strike_raw=str(strike),
        option_type=right,
        expiry_date=EXPIRY,
        lot_size=75,
    )


async def build(store) -> dict:
    await store.writer.start()
    try:
        writer = store.writer
        await upsert_underlying(writer, underlying_row())
        contracts = await upsert_contracts(
            writer,
            underlying_id=1,
            expiry_date=EXPIRY,
            rows=[option_row(strike, right) for strike in STRIKES for right in ("CE", "PE")],
        )
        task_id = 1
        for symbol, contract_id in sorted(contracts.contract_ids.items()):
            await upsert_candle_chunk(
                writer,
                contract_id=contract_id,
                res_id=RES_ID,
                range_from=DAY,
                range_to=DAY,
                coverage=CoverageRow(
                    status="ok",
                    include_oi=True,
                    columns_json=COLUMNS_JSON,
                    task_id=task_id,
                ),
                batch=candles_to_arrow(payload(), COLUMNS, contract_id, RES_ID),
            )
            task_id += 1
        return {"contracts": contracts}
    finally:
        await store.writer.stop()


@pytest.fixture
def loaded(tmp_path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    context = asyncio.run(build(store))
    yield (store, context, tmp_path / "exports")
    if store.is_open:
        store.close()


def export(store, spec, exports_dir, export_id="e1"):
    return asyncio.run(
        exports_m.run_export(store, spec, exports_dir=exports_dir, export_id=export_id)
    )


TOTAL_ROWS = len(STRIKES) * 2 * BARS


# -- spec validation -------------------------------------------------------


def test_an_unknown_format_is_refused():
    with pytest.raises(exports_m.ExportSpecError):
        exports_m.ExportSpec(format="feather").validate()


def test_a_hive_csv_export_is_refused():
    with pytest.raises(exports_m.ExportSpecError):
        exports_m.ExportSpec(format="csv", layout="hive").validate()


def test_a_hive_export_must_be_denormalised():
    with pytest.raises(exports_m.ExportSpecError):
        exports_m.ExportSpec(layout="hive", denormalise=False).validate()


def test_row_group_size_follows_the_layout():
    assert exports_m.ExportSpec().row_group == exports_m.QUERY_ROW_GROUP
    assert exports_m.ExportSpec(layout="hive").row_group == exports_m.ARCHIVE_ROW_GROUP


# -- parquet round trip ----------------------------------------------------


def test_a_parquet_export_round_trips_every_value(loaded):
    store, _, exports_dir = loaded
    result = export(store, exports_m.ExportSpec(), exports_dir)

    assert result.row_count == TOTAL_ROWS
    assert result.path.suffix == ".parquet"
    assert result.sha256 is not None
    assert result.byte_size > 0

    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT symbol, underlying_symbol, expiry_date, strike, option_type, lot_size, "
            f"       resolution, ts_ist, ts_utc_epoch, open, high, low, close, volume, oi "
            f"  FROM read_parquet('{result.path}') "
            " ORDER BY contract_id, ts_utc_epoch"
        ).fetchall()
        total = con.execute(
            f"SELECT count(*) FROM read_parquet('{result.path}')"
        ).fetchone()[0]
    finally:
        con.close()

    assert total == TOTAL_ROWS
    first = rows[0]
    assert first[0] == "NSE:NIFTY25MAR22900CE"
    assert first[1] == "NSE:NIFTY50-INDEX"
    assert first[2] == EXPIRY
    assert first[3] == pytest.approx(22900.0)
    assert first[4] == "CE"
    assert first[5] == 75
    assert first[6] == "1"
    assert first[7] == "2025-03-26 09:15:00"
    assert first[8] == utc_epoch_for_ist(bar_time(0))
    assert [first[9], first[10], first[11], first[12]] == [Decimal(p) for p in PRICES]
    assert first[13] == 1000
    assert first[14] == 50000


def test_the_export_is_written_in_the_physical_sort_order(loaded):
    store, _, exports_dir = loaded
    result = export(store, exports_m.ExportSpec(), exports_dir)
    con = duckdb.connect()
    try:
        keys = con.execute(
            f"SELECT contract_id, res_id, ts_utc_epoch FROM read_parquet('{result.path}')"
        ).fetchall()
    finally:
        con.close()
    assert keys == sorted(keys)


def test_the_parquet_sidecar_records_the_timestamp_conventions(loaded):
    store, _, exports_dir = loaded
    result = export(store, exports_m.ExportSpec(), exports_dir)
    sidecar = result.path.with_name(result.path.name + ".schema.json")
    described = json.loads(sidecar.read_text())
    assert described["row_count"] == TOTAL_ROWS
    assert described["row_group_size"] == exports_m.QUERY_ROW_GROUP
    assert described["conventions"]["ist_offset_seconds"] == IST_OFFSET_SECONDS
    names = [column["name"] for column in described["columns"]]
    assert "ts_ist" in names and "ts_utc_epoch" in names


# -- csv round trip --------------------------------------------------------


def test_a_csv_export_round_trips_and_carries_both_timestamps(loaded):
    store, _, exports_dir = loaded
    result = export(store, exports_m.ExportSpec(format="csv"), exports_dir, export_id="csv1")
    assert result.path.suffix == ".csv"

    con = duckdb.connect()
    try:
        row = con.execute(
            f"SELECT ts_ist, ts_utc_epoch, close, oi FROM read_csv_auto('{result.path}') "
            " ORDER BY contract_id, ts_utc_epoch LIMIT 1"
        ).fetchone()
        total = con.execute(
            f"SELECT count(*) FROM read_csv_auto('{result.path}')"
        ).fetchone()[0]
    finally:
        con.close()

    assert total == TOTAL_ROWS
    assert str(row[0]) == "2025-03-26 09:15:00"
    assert row[1] == utc_epoch_for_ist(bar_time(0))
    assert float(row[2]) == pytest.approx(float(PRICES[3]))
    assert row[3] == 50000


# -- scoping and the hive archive -----------------------------------------


def test_a_scoped_export_writes_only_the_matching_rows(loaded):
    store, _, exports_dir = loaded
    spec = exports_m.ExportSpec(
        scope=exports_m.ExportScope(underlying_id=1, option_type="PE", resolutions=(RES_ID,))
    )
    result = export(store, spec, exports_dir, export_id="scoped")
    assert result.row_count == len(STRIKES) * BARS

    con = duckdb.connect()
    try:
        rights = con.execute(
            f"SELECT DISTINCT option_type FROM read_parquet('{result.path}')"
        ).fetchall()
    finally:
        con.close()
    assert rights == [("PE",)]


def test_a_hive_archive_partitions_and_carries_the_catalog(loaded):
    store, _, exports_dir = loaded
    spec = exports_m.ExportSpec(
        layout="hive", scope=exports_m.ExportScope(include_catalog=True)
    )
    result = export(store, spec, exports_dir, export_id="archive")

    assert result.path.is_dir()
    assert (result.path / "manifest.json").exists()
    assert (result.path / "schema.json").exists()
    for table in exports_m.CATALOG_TABLES:
        assert (result.path / f"{table}.parquet").exists()

    partitions = sorted(
        p.name for p in (result.path / "candles").iterdir() if p.is_dir()
    )
    assert partitions == ["underlying_symbol=NSE%3ANIFTY50-INDEX"]

    con = duckdb.connect()
    try:
        total = con.execute(
            f"SELECT count(*) FROM read_parquet('{result.path}/candles/**/*.parquet', "
            "hive_partitioning = true)"
        ).fetchone()[0]
        contracts = con.execute(
            f"SELECT count(*) FROM read_parquet('{result.path}/dim_contract.parquet')"
        ).fetchone()[0]
    finally:
        con.close()
    assert total == TOTAL_ROWS
    assert contracts == len(STRIKES) * 2 + 1


def test_an_export_replaces_a_previous_file_with_the_same_id(loaded):
    store, _, exports_dir = loaded
    first = export(store, exports_m.ExportSpec(), exports_dir)
    spec = exports_m.ExportSpec(scope=exports_m.ExportScope(option_type="CE"))
    second = export(store, spec, exports_dir)
    assert second.path == first.path
    assert second.row_count == len(STRIKES) * BARS
    leftovers = [p.name for p in exports_dir.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_an_export_that_will_not_fit_is_refused_before_it_writes(loaded, monkeypatch):
    store, _, exports_dir = loaded
    monkeypatch.setattr(exports_m, "BYTES_PER_ROW", {"parquet": 10**15, "csv": 10**15})
    with pytest.raises(exports_m.InsufficientDisk):
        export(store, exports_m.ExportSpec(), exports_dir, export_id="toobig")
    assert not (exports_dir / "toobig.parquet").exists()


def test_the_manifest_row_goes_through_the_writer(loaded):
    store, _, exports_dir = loaded
    result = export(store, exports_m.ExportSpec(), exports_dir)

    async def body():
        await store.writer.start()
        try:
            await record_export(
                store.writer,
                export_id=result.export_id,
                kind="query",
                path=str(result.path),
                filters={"underlying_id": 1},
                row_count=result.row_count,
                byte_size=result.byte_size,
                sha256=result.sha256,
            )
        finally:
            await store.writer.stop()

    asyncio.run(body())
    row = store.connection.execute(
        "SELECT kind, row_count, sha256 FROM export_manifest WHERE export_id = ?",
        [result.export_id],
    ).fetchone()
    assert row == ("query", result.row_count, result.sha256)


# -- maintenance -----------------------------------------------------------


def test_checkpoint_reports_the_wal_on_both_sides(loaded):
    store, _, _ = loaded
    result = asyncio.run(maintenance_m.checkpoint(store))
    assert result.wal_bytes_after <= result.wal_bytes_before


def test_the_storage_report_counts_rows_and_models_the_size(loaded, tmp_path):
    store, _, exports_dir = loaded
    export(store, exports_m.ExportSpec(), exports_dir)
    asyncio.run(maintenance_m.checkpoint(store))
    report = asyncio.run(
        maintenance_m.storage_report(
            store.reader, db_path=store.db_path, exports_dir=exports_dir
        )
    )
    assert report.candle_rows == TOTAL_ROWS
    assert report.duckdb_bytes > 0
    assert report.exports_bytes > 0
    assert report.modelled_bytes == int(TOTAL_ROWS * maintenance_m.BYTES_PER_CANDLE_ROW)
    assert report.bloat_ratio > 0


def test_the_health_checks_are_all_clean_on_a_well_formed_store(loaded):
    store, _, _ = loaded
    checks = {row["check_name"]: row["offending"] for row in asyncio.run(
        maintenance_m.health_checks(store.reader)
    )}
    assert checks["duplicate_keys"] == 0
    assert checks["coverage_row_mismatch"] == 0
    assert checks["spot_id_out_of_reserved_range"] == 0
    assert checks["contract_id_outside_block"] == 0
    assert checks["overlapping_id_blocks"] == 0
    assert checks["unaligned_id_blocks"] == 0
    assert checks["candles_without_a_contract"] == 0


def test_a_contract_id_outside_its_block_is_reported(loaded):
    store, context, _ = loaded
    contract_id = min(context["contracts"].contract_ids.values())
    store.connection.execute(
        "UPDATE dim_contract SET contract_id = 999999 WHERE contract_id = ?", [contract_id]
    )
    checks = {row["check_name"]: row["offending"] for row in asyncio.run(
        maintenance_m.health_checks(store.reader)
    )}
    assert checks["contract_id_outside_block"] == 1


def test_a_duplicate_bar_is_found_by_the_assertion(loaded):
    store, context, _ = loaded
    contract_id = min(context["contracts"].contract_ids.values())
    store.connection.execute(
        "INSERT INTO candles SELECT * FROM candles WHERE contract_id = ? LIMIT 1", [contract_id]
    )
    duplicates = asyncio.run(maintenance_m.duplicate_rows(store.reader))
    assert len(duplicates) == 1
    assert duplicates[0][0] == contract_id
    assert duplicates[0][3] == 2


def test_a_coverage_row_count_mismatch_is_reported(loaded):
    store, context, _ = loaded
    contract_id = min(context["contracts"].contract_ids.values())
    store.connection.execute(
        "UPDATE candle_coverage SET row_count = 99 WHERE contract_id = ?", [contract_id]
    )
    mismatches = asyncio.run(maintenance_m.reconcile_coverage(store.reader))
    assert [row["contract_id"] for row in mismatches] == [contract_id]
    assert mismatches[0]["claimed"] == 99
    assert mismatches[0]["actual"] == BARS


def test_trading_days_are_derived_from_the_spot_bars(loaded):
    store, _, _ = loaded

    async def body():
        await store.writer.start()
        try:
            spot_id = store.connection.execute(
                "SELECT spot_contract_id FROM dim_underlying WHERE underlying_id = 1"
            ).fetchone()[0]
            await upsert_candle_chunk(
                store.writer,
                contract_id=spot_id,
                res_id=RES_ID,
                range_from=DAY,
                range_to=DAY,
                coverage=CoverageRow(
                    status="ok", include_oi=False, columns_json=COLUMNS_JSON, task_id=500
                ),
                batch=candles_to_arrow(payload(), COLUMNS, spot_id, RES_ID),
            )
            return await maintenance_m.refresh_trading_days(store.writer, res_id=RES_ID)
        finally:
            await store.writer.stop()

    assert asyncio.run(body()) == 1
    row = store.connection.execute(
        "SELECT exchange, trade_date, bar_count, derived_from FROM dim_trading_day"
    ).fetchone()
    assert row == ("NSE", DAY, BARS, "spot_bars")


def test_compaction_preserves_every_row_the_constraints_and_the_id_sequence(loaded):
    store, context, _ = loaded
    before_rows = store.connection.execute("SELECT count(*) FROM candles").fetchone()[0]
    before_hi = store.connection.execute(
        "SELECT max(contract_id_hi) FROM dim_expiry"
    ).fetchone()[0]

    result = asyncio.run(maintenance_m.compact(store, keep_backup=False))
    assert result.tables_copied >= 5
    assert result.rows_copied >= before_rows

    con = store.connection
    assert con.execute("SELECT count(*) FROM candles").fetchone()[0] == before_rows
    assert con.execute("SELECT count(*) FROM dim_contract").fetchone()[0] == (
        len(STRIKES) * 2 + 1
    )
    assert con.execute("SELECT count(*) FROM dim_resolution").fetchone()[0] == 15

    # The primary key survived, which a plain CREATE TABLE AS SELECT copy would have dropped.
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO dim_contract SELECT * FROM dim_contract LIMIT 1"
        )

    # The next block starts past every block already recorded, so a fresh expiry cannot be
    # handed ids that already carry bars.
    from expirymanager.db.ids import reserve_block

    lo, _ = reserve_block(con, 1)
    assert lo == before_hi + 1


def test_compaction_restores_the_physical_sort_order(loaded):
    store, _, _ = loaded
    # Insert one bar for the lowest contract id last, so the file is out of order beforehand.
    lowest = store.connection.execute(
        "SELECT min(contract_id) FROM candles"
    ).fetchone()[0]
    store.connection.execute(
        "INSERT INTO candles VALUES (?, ?, TIMESTAMP '2025-03-25 09:15:00', 1, 1, 1, 1, 1, 1)",
        [lowest, RES_ID],
    )
    asyncio.run(maintenance_m.compact(store, keep_backup=False))
    keys = store.connection.execute(
        "SELECT contract_id, res_id, ts FROM candles"
    ).fetchall()
    assert keys == sorted(keys)


def test_compaction_is_refused_when_the_disk_cannot_hold_a_second_copy(loaded, monkeypatch):
    store, _, _ = loaded
    monkeypatch.setattr(maintenance_m, "COMPACTION_MARGIN", 10**9)
    with pytest.raises(maintenance_m.InsufficientDisk):
        asyncio.run(maintenance_m.compact(store))
    assert store.is_open
    assert store.connection.execute("SELECT count(*) FROM candles").fetchone()[0] == TOTAL_ROWS
