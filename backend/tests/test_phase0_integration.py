"""Phase 0 integration gate: boot an empty data directory into a working install.

Every other test module exercises one item in isolation with its own fixtures. Nothing proves
that the six items compose, and the seams between them are where six agents coding against each
other's written interfaces actually diverge. This module boots the whole thing the way the entry
point does, in order, from a directory that does not exist yet:

    paths.ensure -> TLS material -> SQLite engine -> migrations -> settings -> key hierarchy
    -> DuckDB store -> Arrow batch -> writer -> read back -> tear down

A failure here means an item is fine on its own and wrong in the assembly.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from datetime import date
from pathlib import Path

import pytest

from expirymanager import paths as paths_module
from expirymanager import settings_store, version
from expirymanager.db import arrow, migrate, sqlite, writer
from expirymanager.db.duck import DuckStore
from expirymanager.security import kek, keys

# One IST trading session, epoch seconds as Fyers returns them. 1742960700 is the vendor's own
# documented sample and equals 2025-03-26 09:15 IST, which anchors the offset conversion.
FIRST_TS = 1742960700
# The columns array verbatim as section 24 of the Fyers documentation returns it.
SAMPLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
SAMPLE_CANDLES = [
    [FIRST_TS + (i * 60), 100.25 + i, 101.5 + i, 99.75 + i, 100.5 + i, 1000 + i]
    for i in range(5)
]


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """An application home that does not exist yet, so ensure() does the real first-run work."""
    return tmp_path / "home" / ".expirymanager"


def test_empty_directory_boots_to_a_complete_install(data_root: Path) -> None:
    """The whole startup path, in the order __main__ runs it, against one fresh directory."""
    assert not data_root.exists()

    # --- W01: the data directory tree -------------------------------------------------------
    paths = paths_module.ensure(data_root)
    assert paths.root == data_root
    for directory in paths.directories():
        assert directory.is_dir(), f"{directory} was not created"
        assert directory.stat().st_mode & 0o777 == 0o700, f"{directory} is not private"

    # --- W01 seam onto W04: the entry point finds the TLS generator -------------------------
    # ensure() already called the seam. It must have produced real material, not fallen through
    # to the None branch that silently degrades the server to plain HTTP.
    material = paths_module.ensure_tls_material(paths)
    assert material is not None, "the TLS seam returned None, so the server would drop to http"
    key_path, cert_path = material
    assert key_path == paths.tls_key and cert_path == paths.tls_cert
    for secret in (key_path, cert_path):
        assert secret.stat().st_mode & 0o777 == 0o600, f"{secret} is not 0600"
    # The generated pair has to satisfy the exact call uvicorn makes, not merely parse.
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_path, key_path)

    # --- W02: the config database and its migrations ----------------------------------------
    engine = sqlite.create_engine(paths.sqlite_db)
    try:
        applied = migrate.migrate(engine)
        expected_versions = [m.version for m in migrate.discover_migrations()]
        assert applied == expected_versions, f"expected {expected_versions}, applied {applied}"
        assert migrate.current_version(engine) == max(expected_versions)
        assert paths.sqlite_db.stat().st_mode & 0o777 == 0o600

        pragmas = sqlite.read_pragmas(engine)
        assert pragmas["journal_mode"] == "wal"
        assert pragmas["foreign_keys"] == "1"

        # --- W01 seam onto W02: settings read through the real migrated engine --------------
        store = settings_store.SettingsStore(engine)
        values = store.all()
        assert set(values) == set(settings_store.SETTINGS), (
            "the settings store and the seeded table disagree on which keys exist"
        )
        # Decision 5: the governor targets are what migration 0001 seeded, read back live.
        assert store.get_int("throttle_per_second") == 8
        assert store.get_int("throttle_per_minute") == 170
        # A write must survive the read-through cache and land in the database.
        store.set("throttle_in_flight", 4)
        assert settings_store.read_uncached(engine, "throttle_in_flight") == 4
        assert settings_store.SettingsStore(engine).get_int("throttle_in_flight") == 4

        # --- W02 seam onto W04: the key hierarchy over the migrated crypto_key table --------
        raw_connection = engine.raw_connection()
        try:
            key_store = keys.SqliteCryptoKeyStore(raw_connection.driver_connection)
            provider = kek.build_kek_provider(kek.DEFAULT_PROVIDER, key_path=paths.master_key)
            manager = keys.KeyManager(key_store, provider)
            assert manager.ensure_dek() == 1
            assert paths.master_key.stat().st_mode & 0o777 == 0o600

            secret = "synthetic-app-secret-not-a-real-credential"
            blob = manager.encrypt_field(
                secret, table="broker_credential", column="api_secret_enc", row_id="row-1"
            )
            assert secret.encode() not in blob, "the field was stored in the clear"
            assert (
                manager.decrypt_text(
                    blob, table="broker_credential", column="api_secret_enc", row_id="row-1"
                )
                == secret
            )
        finally:
            raw_connection.close()
    finally:
        engine.dispose()

    # --- W03: the market database, the Arrow builder and the writer -------------------------
    asyncio.run(_exercise_market_database(paths))

    # Both databases exist on disk after a clean tear down.
    assert paths.sqlite_db.is_file()
    assert paths.duckdb_file.is_file()


async def _exercise_market_database(paths: paths_module.Paths) -> None:
    """Start the store, push one chunk through the builder into the writer, read it back."""
    store = DuckStore(
        paths.duckdb_file,
        temp_directory=paths.tmp_dir,
        app_version=version.__version__,
    )
    await store.start()
    try:
        # W03 internal seam: the builder's batch is what the writer's INSERT consumes. These are
        # separate modules and the schema is pinned in only one of them.
        batch = arrow.candles_to_arrow(SAMPLE_CANDLES, SAMPLE_COLUMNS, contract_id=1, res_id=2)
        assert batch.schema == arrow.CANDLE_SCHEMA
        assert batch.num_rows == len(SAMPLE_CANDLES)

        def chunk(payload_sha256: str | None) -> writer.CandleChunkWrite:
            return writer.CandleChunkWrite(
                contract_id=1,
                res_id=2,
                range_from=date(2025, 3, 26),
                range_to=date(2025, 3, 26),
                coverage=writer.CoverageRow(
                    status="ok",
                    include_oi=False,
                    columns_json=json.dumps(SAMPLE_COLUMNS),
                    task_id=1,
                    payload_sha256=payload_sha256,
                ),
                batch=batch,
            )

        op = chunk(None)
        result = await store.writer.submit(op)
        assert result.rows_written == len(SAMPLE_CANDLES)
        assert result.skipped is False

        rows = await store.reader.fetch_value(
            "SELECT count(*) FROM candles WHERE contract_id = 1 AND res_id = 2"
        )
        assert rows == len(SAMPLE_CANDLES)

        # The documented epoch converts to naive IST, which is the one thing a reader of this
        # table has to be able to trust.
        first = await store.reader.fetch_value(
            "SELECT min(ts) FROM candles WHERE contract_id = 1 AND res_id = 2"
        )
        assert first.year == 2025 and first.month == 3 and first.day == 26
        assert (first.hour, first.minute) == (9, 15)

        # Re-submitting the identical chunk converges rather than duplicating. With no payload
        # digest recorded there is nothing to compare, so the writer rewrites the window: it
        # deletes exactly what it re-inserts and the row count is unchanged.
        again = await store.writer.submit(op)
        assert again.skipped is False
        assert (again.rows_deleted, again.rows_written) == (len(SAMPLE_CANDLES),) * 2
        assert await store.reader.fetch_value("SELECT count(*) FROM candles") == len(
            SAMPLE_CANDLES
        )

        # With a digest, a re-fetch of a settled expiry short circuits the whole write, which is
        # what keeps a nightly re-run from fragmenting a file that never shrinks.
        assert (await store.writer.submit(chunk("digest-of-this-payload"))).skipped is False
        assert (await store.writer.submit(chunk("digest-of-this-payload"))).skipped is True
        assert await store.reader.fetch_value("SELECT count(*) FROM candles") == len(
            SAMPLE_CANDLES
        )
    finally:
        await store.aclose()


def test_a_currency_underlying_can_be_registered(data_root: Path) -> None:
    """Decision 1 removed the currency-derivative guard, so a CD root must be storable.

    The symbol parser already classifies a currency root, so a narrower CHECK on the registry
    would reject a row the parser is willing to produce, and only at INSERT time.
    """
    paths = paths_module.ensure(data_root, ensure_tls=False)
    engine = sqlite.create_engine(paths.sqlite_db)
    try:
        migrate.migrate(engine)
        from sqlalchemy import text

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO underlying_registry (underlying_id, spot_contract_id, "
                    "fyers_symbol, root, exchange, segment, instrument_kind, display_name, "
                    "data_from, default_resolutions, is_builtin, created_at, updated_at) VALUES "
                    "(:i, :i, :s, :r, 'NSE', 'CD', 'CURRENCY', :d, '2022-01-03', '[\"1\"]', 0, "
                    ":t, :t)"
                ),
                {
                    "i": 900,
                    "s": "NSE:USDINR",
                    "r": "USDINR",
                    "d": "USD INR",
                    "t": "2026-01-01T00:00:00Z",
                },
            )
            stored = connection.execute(
                text("SELECT instrument_kind FROM underlying_registry WHERE underlying_id = 900")
            ).scalar_one()
        assert stored == "CURRENCY"
    finally:
        engine.dispose()


def test_settings_store_answers_from_defaults_without_an_engine() -> None:
    """Bootstrap ordering: settings are read before the database exists, and must not fail."""
    store = settings_store.SettingsStore(None)
    assert store.get_int("throttle_per_second") == 8
    assert set(store.all()) == set(settings_store.SETTINGS)


def test_a_second_instance_lock_is_refused(data_root: Path) -> None:
    """__main__ holds this for the life of the process, so a second acquire must be rejected."""
    paths = paths_module.ensure(data_root, ensure_tls=False)
    lock = paths_module.InstanceLock(paths.lock_file)
    lock.acquire()
    try:
        with pytest.raises(paths_module.SingleInstanceError):
            paths_module.InstanceLock(paths.lock_file).acquire()
    finally:
        lock.release()
