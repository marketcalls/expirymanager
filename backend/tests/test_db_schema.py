"""Schema, pragma, file mode and migration idempotency tests for the SQLite store.

These assert effective state read back from a real database file rather than the intent expressed
in the source, because every failure mode this file is defending against is silent: a pragma that
did not apply, a sidecar left group readable, a migration that ran twice, an index the lease query
does not actually use.
"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from expirymanager.db import migrate as migrate_module
from expirymanager.db import models, sqlite as sqlite_module

EXPECTED_TABLES = {
    "schema_version",
    "settings",
    "crypto_key",
    "app_user",
    "session",
    "broker_credential",
    "broker_token",
    "oauth_state",
    "underlying_registry",
    "schedule",
    "job",
    "task",
    "schedule_run",
    "api_budget",
    "rate_event",
    "pipeline_state",
    "market_holiday",
    "export_job",
    "notification",
    "audit_log",
    "ref_exchange",
    "ref_segment",
    "ref_instrument_type",
    "ref_resolution",
}

EXPECTED_INDEXES = {
    "idx_session_user",
    "idx_session_expiry",
    "idx_token_credential",
    "idx_job_status",
    "idx_task_dispatch",
    "idx_task_ready",
    "idx_task_lease",
    "idx_task_job",
    "idx_task_contract",
    "idx_schedule_run",
}

EXPECTED_SETTING_KEYS = {
    "plan_tier",
    "throttle_per_second",
    "throttle_per_minute",
    "throttle_in_flight",
    "daily_budget",
    "budget_reserve_fraction",
    "worker_count",
    "chunk_days",
    "default_resolutions",
    "include_oi_default",
    "option_life_days",
    "future_life_days",
    "estimate_confirm_threshold",
    "raw_payload_capture",
    "request_log_retention_days",
    "cookie_secure",
    "chart_persist",
    "symbol_year_window_lo",
    "symbol_year_window_hi_offset",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "config.sqlite3"


@pytest.fixture
def engine(db_path: Path):
    eng = sqlite_module.create_engine(db_path)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def migrated(engine):
    migrate_module.migrate(engine)
    return engine


def _table_names(engine) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).all()
    return {row[0] for row in rows if not row[0].startswith("sqlite_")}


def _index_names(engine) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")
        ).all()
    return {row[0] for row in rows}


def _schema_dump(engine) -> list[tuple[str, str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ).all()
    return [(row[0], row[1], row[2]) for row in rows]


class TestMigrationDiscovery:
    def test_migrations_are_discovered_contiguously_and_in_order(self):
        # Derived from the directory rather than hardcoded. A count written into the test is a
        # count that has to be edited every time a migration lands, which is churn that teaches
        # nothing; what actually matters is that versions are contiguous, ordered and start at 1.
        found = migrate_module.discover_migrations()
        versions = [m.version for m in found]
        assert versions == sorted(versions), "migrations are not in ascending order"
        assert versions == list(range(1, len(versions) + 1)), f"non contiguous versions: {versions}"
        assert [m.name for m in found][:5] == [
            "init",
            "pipeline",
            "reference",
            "underlyings",
            "schedules",
        ]

    def test_checksum_is_stable_across_calls(self):
        first = {m.version: m.checksum for m in migrate_module.discover_migrations()}
        second = {m.version: m.checksum for m in migrate_module.discover_migrations()}
        assert first == second

    def test_non_contiguous_versions_are_rejected(self, tmp_path: Path):
        (tmp_path / "0001_init.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
        (tmp_path / "0003_later.sql").write_text("CREATE TABLE b (x INTEGER);", encoding="utf-8")
        with pytest.raises(migrate_module.MigrationError):
            migrate_module.discover_migrations(tmp_path)

    def test_bad_filename_is_rejected(self, tmp_path: Path):
        (tmp_path / "init.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
        with pytest.raises(migrate_module.MigrationError):
            migrate_module.discover_migrations(tmp_path)


class TestStatementSplitter:
    def test_semicolon_inside_a_string_literal_does_not_split(self):
        statements = migrate_module.split_statements(
            "INSERT INTO t VALUES ('a;b'); INSERT INTO t VALUES ('c');"
        )
        assert statements == ["INSERT INTO t VALUES ('a;b')", "INSERT INTO t VALUES ('c')"]

    def test_escaped_quote_is_not_a_terminator(self):
        statements = migrate_module.split_statements("INSERT INTO t VALUES ('it''s; fine');")
        assert statements == ["INSERT INTO t VALUES ('it''s; fine')"]

    def test_comments_are_stripped(self):
        statements = migrate_module.split_statements(
            "-- a leading note; with a semicolon\n"
            "CREATE TABLE a (x INTEGER); /* block; comment */ CREATE TABLE b (x INTEGER);"
        )
        assert statements == ["CREATE TABLE a (x INTEGER)", "CREATE TABLE b (x INTEGER)"]

    def test_unterminated_string_is_rejected(self):
        with pytest.raises(migrate_module.MigrationError):
            migrate_module.split_statements("INSERT INTO t VALUES ('oops);")

    def test_every_shipped_migration_splits_into_executable_statements(self):
        for migration in migrate_module.discover_migrations():
            statements = migrate_module.split_statements(migration.sql)
            assert statements, f"{migration.name} produced no statements"
            for statement in statements:
                assert sqlite3.complete_statement(statement + ";"), statement[:80]


class TestFreshSchema:
    def test_every_table_exists(self, migrated):
        assert _table_names(migrated) == EXPECTED_TABLES

    def test_every_index_exists(self, migrated):
        assert EXPECTED_INDEXES <= _index_names(migrated)

    def test_schema_version_reaches_the_newest_migration(self, migrated):
        expected = max(m.version for m in migrate_module.discover_migrations())
        assert migrate_module.current_version(migrated) == expected

    def test_task_columns_match_the_data_model(self, migrated):
        columns = {c["name"] for c in inspect(migrated).get_columns("task")}
        assert columns == {
            "task_id", "job_id", "seq", "kind", "state", "priority",
            "underlying_id", "contract_id", "fyers_symbol", "expiry_date", "resolution",
            "range_from", "range_to", "include_oi", "request_params_json",
            "parent_task_id", "attempt", "max_attempts", "not_before", "lease_owner",
            "lease_expires_at",
            "http_status", "fyers_s", "fyers_code", "last_error_text", "latency_ms",
            "response_bytes", "row_count", "first_ts", "last_ts", "columns_json",
            "schema_version", "payload_sha256", "raw_body_path", "token_fingerprint",
            "started_at", "finished_at", "created_at",
        }

    def test_task_id_is_autoincrement(self, migrated):
        # AUTOINCREMENT, not plain rowid reuse: a task id must never be recycled, because it is
        # written into candle_coverage as the provenance key.
        with migrated.connect() as connection:
            sql = connection.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'task'")
            ).scalar_one()
            has_sequence = connection.execute(
                text("SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_sequence'")
            ).scalar_one()
        assert "AUTOINCREMENT" in sql
        assert has_sequence == 1

    def test_no_pin_column_exists_anywhere(self, migrated):
        inspector = inspect(migrated)
        for table in EXPECTED_TABLES:
            for column in inspector.get_columns(table):
                assert "pin" not in column["name"].lower(), f"{table}.{column['name']}"

    def test_declared_indexes_carry_their_partial_predicates(self, migrated):
        with migrated.connect() as connection:
            rows = dict(
                connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type = 'index'")
                ).all()
            )
        assert "WHERE state = 'pending'" in rows["idx_task_dispatch"]
        assert "WHERE state = 'pending'" in rows["idx_task_ready"]
        assert "WHERE state = 'leased'" in rows["idx_task_lease"]

    def test_lease_query_uses_the_dispatch_index(self, migrated):
        # The lease statement in PIPELINE.md section 3.1 selects the next tasks by
        # (priority, job_id, seq) over pending rows. If the planner stops choosing
        # idx_task_dispatch it means the index no longer matches the query.
        with migrated.connect() as connection:
            plan = connection.execute(
                text(
                    "EXPLAIN QUERY PLAN "
                    "SELECT task_id FROM task WHERE state = 'pending' AND not_before <= :now "
                    "ORDER BY priority, job_id, seq LIMIT 8"
                ),
                {"now": "2026-01-01T00:00:00Z"},
            ).all()
        detail = " ".join(str(row[-1]) for row in plan)
        assert "idx_task_dispatch" in detail, detail
        assert "TEMP B-TREE" not in detail.upper(), detail


class TestSeeds:
    def test_all_setting_keys_are_seeded(self, migrated):
        with migrated.connect() as connection:
            keys = {row[0] for row in connection.execute(text("SELECT key FROM settings")).all()}
        assert keys == EXPECTED_SETTING_KEYS

    def test_throttle_settings_match_the_standard_plan_targets(self, migrated):
        with migrated.connect() as connection:
            values = dict(
                connection.execute(text("SELECT key, value_json FROM settings")).all()
            )
        assert values["throttle_per_second"] == "8"
        assert values["throttle_per_minute"] == "170"
        assert values["plan_tier"] == '"standard"'
        assert values["daily_budget"] == "100000"

    def test_reference_rows_are_seeded(self, migrated):
        with migrated.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM ref_exchange")).scalar_one() == 3
            assert connection.execute(text("SELECT count(*) FROM ref_segment")).scalar_one() == 4
            assert (
                connection.execute(text("SELECT count(*) FROM ref_instrument_type")).scalar_one()
                == 13
            )
            assert (
                connection.execute(text("SELECT count(*) FROM ref_resolution")).scalar_one() == 14
            )
            assert (
                connection.execute(
                    text("SELECT availability_window_days FROM ref_resolution WHERE fyers_code='5S'")
                ).scalar_one()
                == 30
            )
            # 100, raised from 95 by migration 0006 once the live API settled whether the
            # documented limit counts calendar or trading days. See docs/API-PROBES.md: a span of
            # 100 answers 200 and 101 answers 422. The planner reads THIS column, not the constant
            # in calendar.py, so the two disagreeing is what silently kept chunks at 95.
            assert (
                connection.execute(
                    text("SELECT max_days_per_request FROM ref_resolution WHERE fyers_code='1'")
                ).scalar_one()
                == 100
            )

    def test_builtin_underlyings_are_seeded(self, migrated):
        with migrated.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT underlying_id, fyers_symbol, root, exchange, instrument_kind, "
                    "data_from, spot_contract_id, is_builtin FROM underlying_registry "
                    "ORDER BY underlying_id"
                )
            ).all()
        assert rows == [
            (1, "NSE:NIFTY50-INDEX", "NIFTY", "NSE", "INDEX", "2022-01-03", 1, 1),
            (2, "NSE:NIFTYBANK-INDEX", "BANKNIFTY", "NSE", "INDEX", "2022-01-03", 2, 1),
            (3, "BSE:SENSEX-INDEX", "SENSEX", "BSE", "INDEX", "2023-08-07", 3, 1),
            (4, "NSE:RELIANCE-EQ", "RELIANCE", "NSE", "EQUITY", "2022-01-03", 4, 1),
        ]

    def test_spot_contract_ids_are_inside_the_reserved_range(self, migrated):
        with migrated.connect() as connection:
            worst = connection.execute(
                text("SELECT max(spot_contract_id), min(spot_contract_id) FROM underlying_registry")
            ).one()
        assert 1 <= worst[1] and worst[0] <= 999

    def test_market_holidays_are_seeded_for_both_exchanges(self, migrated):
        with migrated.connect() as connection:
            counts = dict(
                connection.execute(
                    text("SELECT exchange, count(*) FROM market_holiday GROUP BY exchange")
                ).all()
            )
            sample = connection.execute(
                text(
                    "SELECT count(*) FROM market_holiday "
                    "WHERE exchange = 'NSE' AND holiday_date = '2025-10-02'"
                )
            ).scalar_one()
        assert counts["NSE"] == counts["BSE"] > 0
        assert sample == 1

    def test_the_temp_seed_table_did_not_survive(self, migrated):
        assert "seed_holiday" not in _table_names(migrated)

    def test_pipeline_state_has_exactly_one_row(self, migrated):
        with migrated.connect() as connection:
            rows = connection.execute(text("SELECT id, mode FROM pipeline_state")).all()
        assert rows == [(1, "running")]


class TestPragmas:
    def test_pragmas_are_effective_on_a_connection(self, migrated):
        values = sqlite_module.read_pragmas(migrated)
        assert values["journal_mode"] == "wal"
        assert values["synchronous"] == "1"
        assert values["foreign_keys"] == "1"
        assert values["busy_timeout"] == "5000"
        assert values["secure_delete"] == "1"
        assert values["trusted_schema"] == "0"
        assert values["cell_size_check"] == "1"

    def test_pragmas_are_reapplied_on_a_brand_new_connection(self, db_path: Path, migrated):
        # foreign_keys and busy_timeout are per connection, so the value that matters is the one a
        # freshly pooled connection reports, not the one the first connection set.
        migrated.dispose()
        second = sqlite_module.create_engine(db_path)
        try:
            values = sqlite_module.read_pragmas(second)
        finally:
            second.dispose()
        assert values["foreign_keys"] == "1"
        assert values["busy_timeout"] == "5000"
        assert values["secure_delete"] == "1"

    def test_foreign_keys_are_actually_enforced(self, migrated):
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            with migrated.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO broker_token (token_id, credential_id, access_token_enc, "
                        "key_ver, token_fingerprint, issued_at, state) "
                        "VALUES ('t1', 'missing-credential', X'00', 1, 'fp', 'now', 'active')"
                    )
                )


class TestFileModes:
    def test_database_and_sidecars_are_owner_only(self, db_path: Path, migrated):
        # Sidecars only exist while a WAL connection is open, so hold one for the assertion.
        with migrated.connect() as connection:
            connection.execute(text("SELECT 1")).all()
            for path in (
                db_path,
                db_path.with_name(db_path.name + "-wal"),
                db_path.with_name(db_path.name + "-shm"),
            ):
                assert path.exists(), path
                mode = stat.S_IMODE(path.stat().st_mode)
                assert mode & 0o077 == 0, f"{path.name} is {oct(mode)}"

    def test_parent_directory_is_owner_only(self, db_path: Path, migrated):
        mode = stat.S_IMODE(db_path.parent.stat().st_mode)
        assert mode & 0o077 == 0, oct(mode)

    def test_a_preexisting_loose_database_file_is_tightened(self, tmp_path: Path):
        loose = tmp_path / "loose.sqlite3"
        loose.touch()
        loose.chmod(0o644)
        eng = sqlite_module.create_engine(loose)
        try:
            assert stat.S_IMODE(loose.stat().st_mode) & 0o077 == 0
        finally:
            eng.dispose()


class TestIdempotency:
    def test_second_migrate_applies_nothing(self, engine):
        expected = [m.version for m in migrate_module.discover_migrations()]
        first = migrate_module.migrate(engine)
        second = migrate_module.migrate(engine)
        assert first == expected
        assert second == []

    def test_schema_is_byte_identical_after_a_second_run(self, engine):
        migrate_module.migrate(engine)
        before = _schema_dump(engine)
        migrate_module.migrate(engine)
        assert _schema_dump(engine) == before

    def test_seed_rows_are_not_duplicated(self, engine):
        migrate_module.migrate(engine)
        migrate_module.migrate(engine)
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM underlying_registry")).scalar_one()
                == 4
            )
            assert connection.execute(text("SELECT count(*) FROM settings")).scalar_one() == 19
            assert (
                connection.execute(text("SELECT count(*) FROM schema_version")).scalar_one()
                == len(migrate_module.discover_migrations())
            )

    def test_ledger_records_a_checksum_per_migration(self, engine):
        migrate_module.migrate(engine)
        recorded = migrate_module.applied_migrations(engine)
        expected = {m.version: m.checksum for m in migrate_module.discover_migrations()}
        assert recorded == expected

    def test_editing_an_applied_migration_is_refused(self, engine, tmp_path: Path):
        staging = tmp_path / "migrations"
        staging.mkdir()
        for migration in migrate_module.discover_migrations():
            (staging / migration.path.name).write_text(migration.sql, encoding="utf-8")
        migrate_module.migrate(engine, directory=staging)

        target = staging / "0003_reference.sql"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n-- an edit after the fact\n", encoding="utf-8"
        )
        with pytest.raises(migrate_module.MigrationChecksumError):
            migrate_module.migrate(engine, directory=staging)

    def test_a_later_migration_is_applied_without_replaying_the_earlier_ones(
        self, engine, tmp_path: Path
    ):
        # This is the shape W14 adds 0005_schedules.sql in: the ledger must carry the new version
        # forward and leave the seeded rows of the earlier files untouched.
        staging = tmp_path / "migrations"
        staging.mkdir()
        (staging / "0001_init.sql").write_text(
            "CREATE TABLE thing (id INTEGER PRIMARY KEY);\nINSERT INTO thing VALUES (1);\n",
            encoding="utf-8",
        )
        assert migrate_module.migrate(engine, directory=staging) == [1]

        (staging / "0002_more.sql").write_text(
            "ALTER TABLE thing ADD COLUMN label TEXT;\n", encoding="utf-8"
        )
        assert migrate_module.migrate(engine, directory=staging) == [2]
        assert migrate_module.current_version(engine) == 2

        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM thing")).scalar_one() == 1
            columns = {c["name"] for c in inspect(engine).get_columns("thing")}
        assert columns == {"id", "label"}

    def test_a_failing_migration_leaves_no_partial_schema(self, engine, tmp_path: Path):
        staging = tmp_path / "migrations"
        staging.mkdir()
        (staging / "0001_init.sql").write_text(
            "CREATE TABLE good (x INTEGER);\nCREATE TABLE good (x INTEGER);\n", encoding="utf-8"
        )
        with pytest.raises(Exception):
            migrate_module.migrate(engine, directory=staging)
        assert "good" not in _table_names(engine)
        assert migrate_module.current_version(engine) == 0


class TestModelsMatchTheMigratedSchema:
    def test_model_tables_match_the_database(self, migrated):
        assert set(models.metadata.tables) == _table_names(migrated)

    def test_model_columns_match_the_database(self, migrated):
        inspector = inspect(migrated)
        for name, table in models.metadata.tables.items():
            reflected = {c["name"]: c for c in inspector.get_columns(name)}
            assert set(table.columns.keys()) == set(reflected), name
            for column in table.columns:
                actual = reflected[column.name]
                assert str(column.type) == str(actual["type"]), f"{name}.{column.name}"
                if column.primary_key:
                    # SQLite reports an INTEGER PRIMARY KEY rowid alias as nullable, because
                    # inserting NULL there means "assign the next rowid". Not a mismatch.
                    continue
                assert column.nullable == actual["nullable"], f"{name}.{column.name}"

    def test_model_primary_keys_match_the_database(self, migrated):
        inspector = inspect(migrated)
        for name, table in models.metadata.tables.items():
            expected = [c.name for c in table.primary_key.columns]
            actual = inspector.get_pk_constraint(name)["constrained_columns"]
            assert expected == list(actual), name

    def test_model_index_names_match_the_database(self, migrated):
        declared = {index.name for table in models.metadata.tables.values() for index in table.indexes}
        assert declared == EXPECTED_INDEXES

    def test_orm_round_trip_through_the_session_scope(self, db_path: Path, migrated):
        migrated.dispose()
        sqlite_module.dispose_engine()
        sqlite_module.init_engine(db_path)
        try:
            with sqlite_module.session_scope() as session:
                session.add(
                    models.Notification(
                        notification_id="n1",
                        level="warning",
                        code="needs_reauth",
                        title="Reconnect Fyers",
                        created_at="2026-01-01T00:00:00Z",
                    )
                )
            with sqlite_module.session_scope() as session:
                stored = session.get(models.Notification, "n1")
                assert stored is not None
                assert stored.code == "needs_reauth"
                assert stored.read_at is None
        finally:
            sqlite_module.dispose_engine()
