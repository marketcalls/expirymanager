"""Typed settings: code defaults, validation, persistence and the missing-table fallback."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from expirymanager.settings_store import (
    SETTINGS,
    SettingOutOfRangeError,
    SettingsStore,
    UnknownSettingError,
    defaults,
    read_uncached,
)

# The keys DATA-MODEL.md section 1.2 says migration 0001 seeds. Every one needs a code default,
# because the store must answer correctly before that migration has run.
SEEDED_KEYS = (
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
)

CREATE_SETTINGS = (
    "CREATE TABLE settings ("
    "key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path / 'config.sqlite3'}")
    with eng.begin() as conn:
        conn.execute(text(CREATE_SETTINGS))
    yield eng
    eng.dispose()


@pytest.fixture
def engine_without_table(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path / 'empty.sqlite3'}")
    yield eng
    eng.dispose()


class TestDefaults:
    def test_every_seeded_key_has_a_code_default(self) -> None:
        for key in SEEDED_KEYS:
            assert key in SETTINGS, f"{key} has no spec"
        assert set(defaults()) == set(SETTINGS)

    def test_no_key_is_defined_that_the_data_model_does_not_list(self) -> None:
        assert set(SETTINGS) == set(SEEDED_KEYS)

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("plan_tier", "standard"),
            ("throttle_per_second", 8),
            ("throttle_per_minute", 170),
            ("throttle_in_flight", 6),
            ("daily_budget", 100_000),
            ("budget_reserve_fraction", 0.70),
            ("worker_count", 8),
            ("chunk_days", 95),
            ("default_resolutions", ["1", "5"]),
            ("include_oi_default", True),
            ("option_life_days", 200),
            ("future_life_days", 400),
            ("raw_payload_capture", "discovery"),
            ("cookie_secure", True),
            ("symbol_year_window_lo", 2015),
            ("symbol_year_window_hi_offset", 5),
        ],
    )
    def test_documented_defaults(self, key: str, expected: object) -> None:
        assert SettingsStore(None).get(key) == expected

    def test_the_governor_targets_match_the_standard_plan_decision(self) -> None:
        store = SettingsStore(None)
        assert store.get_int("throttle_per_second") == 8
        assert store.get_int("throttle_per_minute") == 170
        assert store.get_int("daily_budget") == 100_000

    def test_every_default_passes_its_own_validator(self) -> None:
        for key, spec in SETTINGS.items():
            assert spec.validate(spec.default) == spec.default, key

    def test_a_store_without_an_engine_answers_from_defaults(self) -> None:
        assert SettingsStore(None).all() == defaults()

    def test_describe_carries_value_default_and_description(self) -> None:
        described = SettingsStore(None).describe()
        assert described["worker_count"]["value"] == 8
        assert described["worker_count"]["default"] == 8
        assert described["worker_count"]["type"] == "int"
        assert described["worker_count"]["description"]


class TestValidation:
    def test_an_unknown_key_is_rejected_on_read(self) -> None:
        with pytest.raises(UnknownSettingError) as excinfo:
            SettingsStore(None).get("no_such_setting")
        assert excinfo.value.code == "unknown_setting"

    def test_an_unknown_key_is_rejected_on_write(self) -> None:
        with pytest.raises(UnknownSettingError):
            SettingsStore(None).set("no_such_setting", 1)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("throttle_per_second", 11),
            ("throttle_per_second", 0),
            ("throttle_per_minute", 601),
            ("worker_count", 0),
            ("chunk_days", 96),
            ("budget_reserve_fraction", 1.5),
            ("budget_reserve_fraction", -0.1),
            ("plan_tier", "platinum"),
            ("raw_payload_capture", "everything"),
            ("symbol_year_window_lo", 1900),
        ],
    )
    def test_out_of_range_values_are_rejected(self, key: str, value: object) -> None:
        with pytest.raises(SettingOutOfRangeError) as excinfo:
            SettingsStore(None).set(key, value)
        assert excinfo.value.code == "setting_out_of_range"

    def test_chunk_days_may_not_exceed_the_documented_request_span(self) -> None:
        store = SettingsStore(None)
        assert store.set("chunk_days", 95) == 95
        with pytest.raises(SettingOutOfRangeError):
            store.set("chunk_days", 100)

    def test_a_boolean_setting_rejects_an_arbitrary_integer(self) -> None:
        with pytest.raises(SettingOutOfRangeError):
            SettingsStore(None).set("include_oi_default", 2)

    def test_an_integer_setting_rejects_a_boolean(self) -> None:
        with pytest.raises(SettingOutOfRangeError):
            SettingsStore(None).set("worker_count", True)

    def test_unknown_resolution_codes_are_rejected(self) -> None:
        with pytest.raises(SettingOutOfRangeError):
            SettingsStore(None).set("default_resolutions", ["1", "7"])

    def test_an_empty_resolution_list_is_rejected(self) -> None:
        with pytest.raises(SettingOutOfRangeError):
            SettingsStore(None).set("default_resolutions", [])

    def test_the_second_resolution_code_is_accepted(self) -> None:
        assert SettingsStore(None).set("default_resolutions", ["5S", "1"]) == ["5S", "1"]


class TestPersistence:
    def test_set_then_get_round_trips_through_sqlite(self, engine) -> None:
        store = SettingsStore(engine)
        store.set("worker_count", 4)

        fresh = SettingsStore(engine)
        assert fresh.get_int("worker_count") == 4

    def test_the_stored_value_is_json(self, engine) -> None:
        SettingsStore(engine).set("default_resolutions", ["1", "15"])
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value_json, updated_at FROM settings WHERE key = 'default_resolutions'")
            ).one()
        assert json.loads(row[0]) == ["1", "15"]
        assert row[1]

    def test_set_many_writes_nothing_when_one_key_is_invalid(self, engine) -> None:
        store = SettingsStore(engine)
        with pytest.raises(SettingOutOfRangeError):
            store.set_many({"worker_count": 4, "throttle_per_second": 99})

        fresh = SettingsStore(engine)
        assert fresh.get_int("worker_count") == 8

    def test_set_many_applies_every_key_when_all_are_valid(self, engine) -> None:
        SettingsStore(engine).set_many({"worker_count": 2, "chunk_days": 30})
        fresh = SettingsStore(engine)
        assert fresh.get_int("worker_count") == 2
        assert fresh.get_int("chunk_days") == 30

    def test_reset_removes_the_row_and_restores_the_default(self, engine) -> None:
        store = SettingsStore(engine)
        store.set("worker_count", 3)
        assert store.reset("worker_count") == 8
        assert SettingsStore(engine).get_int("worker_count") == 8

    def test_all_merges_stored_rows_over_defaults(self, engine) -> None:
        SettingsStore(engine).set("plan_tier", "prime")
        merged = SettingsStore(engine).all()
        assert merged["plan_tier"] == "prime"
        assert merged["worker_count"] == 8
        assert set(merged) == set(SETTINGS)

    def test_read_uncached_bypasses_the_cache(self, engine) -> None:
        store = SettingsStore(engine)
        store.get_int("worker_count")
        SettingsStore(engine).set("worker_count", 5)
        assert store.get_int("worker_count") == 8, "the first store is still serving its cache"
        assert read_uncached(engine, "worker_count") == 5

    def test_invalidate_picks_up_an_out_of_band_write(self, engine) -> None:
        store = SettingsStore(engine)
        store.get_int("worker_count")
        SettingsStore(engine).set("worker_count", 5)
        store.invalidate()
        assert store.get_int("worker_count") == 5


class TestDegradedDatabase:
    def test_a_missing_settings_table_falls_back_to_defaults(self, engine_without_table) -> None:
        store = SettingsStore(engine_without_table)
        assert store.get_int("worker_count") == 8
        assert store.all() == defaults()

    def test_a_stored_value_that_no_longer_validates_falls_back(self, engine) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settings (key, value_json, updated_at) "
                    "VALUES ('worker_count', '999', '2026-01-01T00:00:00Z')"
                )
            )
        assert SettingsStore(engine).get_int("worker_count") == 8

    def test_a_key_from_a_newer_build_is_ignored_rather_than_fatal(self, engine) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settings (key, value_json, updated_at) "
                    "VALUES ('future_feature_flag', 'true', '2026-01-01T00:00:00Z')"
                )
            )
        assert SettingsStore(engine).all() == defaults()

    def test_malformed_json_falls_back(self, engine) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settings (key, value_json, updated_at) "
                    "VALUES ('plan_tier', 'not json', '2026-01-01T00:00:00Z')"
                )
            )
        assert SettingsStore(engine).get_str("plan_tier") == "standard"


class TestNoEnvironmentConfiguration:
    def test_the_module_reads_no_environment_variable(self) -> None:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "expirymanager" / "settings_store.py"
        ).read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "getenv" not in source
        assert "dotenv" not in source
