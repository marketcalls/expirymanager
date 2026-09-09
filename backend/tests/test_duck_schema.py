"""Schema, configuration and single-holder tests for the DuckDB store."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from expirymanager.db.duck import (
    DUCK_SCHEMA_VERSION,
    DuckStore,
    DuckStoreLockedError,
    DuckStoreUsageError,
    guard_statement,
    locked_message,
)

EXPECTED_TABLES = {
    "candles",
    "candle_coverage",
    "candle_greeks",
    "chain_snapshot",
    "contract_bounds",
    "dim_contract",
    "dim_expiry",
    "dim_instrument_master",
    "dim_resolution",
    "dim_trading_day",
    "dim_underlying",
    "export_manifest",
    "ingest_run",
    "meta",
    "symbol_master_snapshot",
}

EXPECTED_VIEWS = {"v_candle", "v_candle_daily", "v_contract_full", "v_coverage_gaps",
                  "v_data_health"}

EXPECTED_MACROS = {"bars", "spot_at", "atm_strike", "chain_at", "chain_window"}


@pytest.fixture
def store(tmp_path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    yield store
    store.close()


def test_fresh_database_has_every_table(store):
    names = {
        row[0]
        for row in store.connection.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
        ).fetchall()
    }
    assert EXPECTED_TABLES <= names


def test_fresh_database_has_every_view_and_macro(store):
    views = {
        row[0]
        for row in store.connection.execute(
            "SELECT view_name FROM duckdb_views() WHERE internal = false"
        ).fetchall()
    }
    assert EXPECTED_VIEWS <= views
    macros = {
        row[0]
        for row in store.connection.execute(
            "SELECT function_name FROM duckdb_functions() WHERE internal = false"
        ).fetchall()
    }
    assert EXPECTED_MACROS <= macros


def test_candle_prices_are_decimal_eleven_four(store):
    types = dict(
        store.connection.execute(
            "SELECT column_name, data_type FROM duckdb_columns() "
            "WHERE table_name = 'candles'"
        ).fetchall()
    )
    assert types["contract_id"] == "INTEGER"
    assert types["res_id"] == "UTINYINT"
    assert types["ts"] == "TIMESTAMP"
    for column in ("open", "high", "low", "close"):
        assert types[column] == "DECIMAL(11,4)", column
    assert types["volume"] == "BIGINT"
    assert types["oi"] == "BIGINT"
    assert len(types) == 9


def test_candles_has_no_constraint_and_no_index(store):
    # This is a measured decision, not an oversight. A PRIMARY KEY on 5,000,000 rows grew the
    # file 4.5x and slowed the load 4.5x for no lookup benefit. If this test ever fails because
    # somebody added an index, read the comment at the top of duck_schema.sql before changing it.
    kinds = [
        row[0]
        for row in store.connection.execute(
            "SELECT constraint_type FROM duckdb_constraints() WHERE table_name = 'candles'"
        ).fetchall()
    ]
    assert set(kinds) == {"NOT NULL"}
    assert len(kinds) == 8
    indexes = store.connection.execute(
        "SELECT count(*) FROM duckdb_indexes() WHERE table_name = 'candles'"
    ).fetchone()[0]
    assert indexes == 0


def test_catalog_tables_do_keep_their_primary_keys(store):
    for table in ("dim_contract", "dim_underlying", "candle_coverage", "contract_bounds"):
        count = store.connection.execute(
            "SELECT count(*) FROM duckdb_constraints() "
            "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
            [table],
        ).fetchone()[0]
        assert count == 1, table


def test_meta_records_the_conventions(store):
    meta = dict(store.connection.execute("SELECT key, value FROM meta").fetchall())
    assert meta["candles.price.type"] == "DECIMAL(11,4)"
    assert meta["candles.ts.timezone"] == "Asia/Kolkata"
    assert meta["ist_offset_seconds"] == "19800"
    assert meta["schema_version"] == str(DUCK_SCHEMA_VERSION)
    assert meta["app_version"] == "0.0.0-test"
    assert meta["created_at"]


def test_connection_config_is_pinned(store):
    settings = dict(
        store.connection.execute(
            "SELECT name, value FROM duckdb_settings() "
            "WHERE name IN ('TimeZone', 'threads', 'preserve_insertion_order')"
        ).fetchall()
    )
    assert settings["TimeZone"] == "Asia/Kolkata"
    assert str(settings["threads"]) == "6"
    assert str(settings["preserve_insertion_order"]).lower() == "false"


def test_resolution_reference_is_seeded(store):
    row = store.connection.execute(
        "SELECT fyers_code, seconds, max_days_per_request FROM dim_resolution WHERE res_id = 2"
    ).fetchone()
    assert row == ("1", 60, 95)
    assert store.connection.execute(
        "SELECT availability_window_days FROM dim_resolution WHERE fyers_code = '5S'"
    ).fetchone()[0] == 30


def test_schema_application_is_idempotent(tmp_path):
    path = tmp_path / "market.duckdb"
    first = DuckStore(path, app_version="0.0.0-test")
    first.open()
    first.connection.execute(
        "INSERT INTO candles VALUES (1, 2, TIMESTAMP '2025-03-26 09:15:00', 1, 1, 1, 1, 1, NULL)"
    )
    first.close()

    second = DuckStore(path, app_version="0.0.0-test")
    second.open()
    try:
        assert second.connection.execute("SELECT count(*) FROM candles").fetchone()[0] == 1
        assert second.connection.execute("SELECT count(*) FROM dim_resolution").fetchone()[0] == 15
    finally:
        second.close()


def test_data_health_view_runs_on_an_empty_database(store):
    rows = dict(store.connection.execute("SELECT * FROM v_data_health").fetchall())
    assert rows == {"duplicate_keys": 0, "coverage_row_mismatch": 0, "contracts_with_no_bars": 0}


def test_read_only_is_refused(tmp_path):
    with pytest.raises(DuckStoreUsageError):
        DuckStore(tmp_path / "market.duckdb", read_only=True)


def test_attaching_the_live_file_is_refused(store):
    with pytest.raises(DuckStoreUsageError):
        guard_statement(f"ATTACH '{store.db_path}' AS other", store.db_path)


def test_attaching_a_different_file_is_allowed(store, tmp_path):
    guard_statement(f"ATTACH '{tmp_path / 'compacted.duckdb'}' AS newdb", store.db_path)


def test_locked_message_names_the_three_causes(tmp_path):
    text = locked_message(tmp_path / "market.duckdb", "Conflicting lock")
    assert "1. A second copy of ExpiryManager" in text
    assert "2. A duckdb command line session" in text
    assert "3. A notebook, DBeaver" in text
    assert "Nothing needs to be repaired." in text


def test_second_process_gets_the_plain_text_message(tmp_path):
    """A real second holder, because this is the failure users actually hit."""
    path = tmp_path / "market.duckdb"
    store = DuckStore(path, app_version="0.0.0-test")
    store.open()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import sys, duckdb
                con = duckdb.connect({str(path)!r})
                print("ready", flush=True)
                sys.stdin.readline()
                """
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        store.close()
        assert holder.stdout.readline().strip() == "ready"
        blocked = DuckStore(path, app_version="0.0.0-test")
        with pytest.raises(DuckStoreLockedError) as excinfo:
            blocked.open()
        message = str(excinfo.value)
        assert "A second copy of ExpiryManager" in message
        assert "duckdb command line session" in message
        assert "DBeaver" in message
    finally:
        holder.stdin.write("go\n")
        holder.stdin.flush()
        holder.wait(timeout=30)
