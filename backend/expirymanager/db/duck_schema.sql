-- ExpiryManager analytic store, DuckDB.
--
-- Applied idempotently on every startup by db/duck.py. Every statement is CREATE ... IF NOT
-- EXISTS or CREATE OR REPLACE, so re-running it on a populated database is a no-op.
--
-- Price scale note: candle prices are DECIMAL(11,4), and every strike and quote column carries
-- four decimal places. The product lets a user register their own underlying, so currency
-- derivatives (0.0025 tick) must be representable. Two decimal places would truncate them
-- silently, and widening after the first backfill means rewriting every row of the largest
-- table in the system. The extra bytes per row are accepted deliberately.

-- ---------------------------------------------------------------------------
-- Sequences
-- ---------------------------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS seq_contract_block START 1000;
CREATE SEQUENCE IF NOT EXISTS seq_expiry_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_run_id START 1;

-- ---------------------------------------------------------------------------
-- The fact table
-- ---------------------------------------------------------------------------

-- Nine columns. No PRIMARY KEY, no UNIQUE constraint and no index, on purpose.
--
-- This is a measured decision, not an oversight, and it must not be "fixed" later:
-- adding PRIMARY KEY (contract_id, ts) to a 5,000,000 row table grew the file from 75.5 MB to
-- 341.6 MB and slowed the load from 0.45 s to 2.02 s, while point lookups were unchanged
-- (0.41 ms against 0.40 ms). DuckDB has no secondary index for range scans, so the only index
-- that matters is physical order.
--
-- The sort key is (contract_id, res_id, ts). It is maintained by inserting one contract at a
-- time in ascending ts, and restored by the offline compaction routine. A single contract scan
-- measured 0.1 ms in that order against 2.6 ms when the same rows were inserted in ts order.
--
-- Duplicates are prevented by the writer, which deletes the exact requested window before it
-- inserts. db/maintenance.py runs the duplicate assertion that a constraint would otherwise do.
CREATE TABLE IF NOT EXISTS candles (
    contract_id INTEGER       NOT NULL,
    res_id      UTINYINT      NOT NULL,
    ts          TIMESTAMP     NOT NULL,   -- bar open, naive, IST wall clock
    open        DECIMAL(11,4) NOT NULL,
    high        DECIMAL(11,4) NOT NULL,
    low         DECIMAL(11,4) NOT NULL,
    close       DECIMAL(11,4) NOT NULL,
    volume      BIGINT        NOT NULL,
    oi          BIGINT                    -- NULL when include_oi was 0, and for indices
);

-- ---------------------------------------------------------------------------
-- Catalog dimensions
-- ---------------------------------------------------------------------------

-- Writer maintained mirror of sqlite.underlying_registry. It lives here so that no query the
-- chart, the chain grid or the export builder runs ever has to cross engines.
CREATE TABLE IF NOT EXISTS dim_underlying (
    underlying_id      INTEGER PRIMARY KEY,
    fyers_symbol       VARCHAR NOT NULL UNIQUE,
    root               VARCHAR NOT NULL,
    exchange           VARCHAR NOT NULL,
    exchange_code      UTINYINT NOT NULL,
    segment            VARCHAR NOT NULL,
    segment_code       UTINYINT NOT NULL,
    instrument_kind    VARCHAR NOT NULL,
    display_name       VARCHAR NOT NULL,
    spot_contract_id   INTEGER NOT NULL,
    underlying_fytoken VARCHAR,
    data_from          DATE NOT NULL,
    first_expiry       DATE,
    last_expiry        DATE,
    expiry_count       INTEGER,
    contract_count     INTEGER,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    synced_at          TIMESTAMP NOT NULL,
    CHECK ((exchange_code, segment_code) IN
           ((10,10),(10,11),(10,12),(11,20),(12,10),(12,11),(12,12),(11,11)))
);

CREATE TABLE IF NOT EXISTS dim_expiry (
    expiry_id               INTEGER PRIMARY KEY,
    underlying_id           INTEGER NOT NULL,
    expiry_date             DATE NOT NULL,
    has_futures             BOOLEAN NOT NULL DEFAULT FALSE,
    has_options             BOOLEAN NOT NULL DEFAULT FALSE,
    futures_count           INTEGER,
    options_count           INTEGER,
    contract_count          INTEGER,
    min_strike              DECIMAL(12,4),
    max_strike              DECIMAL(12,4),
    strike_step             DECIMAL(12,4),
    contract_id_lo          INTEGER,
    contract_id_hi          INTEGER,
    expiry_cycle_derived    VARCHAR,
    expiry_cycle_source     VARCHAR NOT NULL DEFAULT 'derived',
    is_last_of_month        BOOLEAN,
    expiry_dow              UTINYINT,
    source_range_from       DATE,
    source_range_to         DATE,
    discovered_at           TIMESTAMP NOT NULL,
    contracts_discovered_at TIMESTAMP,
    discovered_task_id      BIGINT,
    UNIQUE (underlying_id, expiry_date)
);

-- expiry_date is the value passed to Get Expired Contracts and is authoritative, because a
-- monthly coded symbol carries no expiry day. parsed_expiry_date holds whatever the symbol
-- string said and a mismatch is a data quality alarm, never something to paper over.
CREATE TABLE IF NOT EXISTS dim_contract (
    contract_id            INTEGER PRIMARY KEY,
    underlying_id          INTEGER NOT NULL,
    expiry_id              INTEGER,
    fyers_symbol           VARCHAR NOT NULL UNIQUE,
    kind                   VARCHAR NOT NULL,   -- 'SPOT' | 'FUT' | 'OPT'
    instrument_class       VARCHAR NOT NULL,
    exchange               VARCHAR NOT NULL,
    exchange_code          UTINYINT NOT NULL,
    segment                VARCHAR NOT NULL,
    segment_code           UTINYINT NOT NULL,
    ex_instrument_type     UTINYINT,
    root                   VARCHAR NOT NULL,

    expiry_date            DATE,
    expiry_year            SMALLINT,
    expiry_month           UTINYINT,
    expiry_day             UTINYINT,
    expiry_dow             UTINYINT,
    parsed_expiry_date     DATE,
    symbol_expiry_encoding VARCHAR,
    expiry_cycle           VARCHAR,
    expiry_cycle_source    VARCHAR,

    strike                 DECIMAL(12,4),
    strike_raw             VARCHAR,
    strike_ordinal         INTEGER,
    option_type            VARCHAR,

    fytoken                VARCHAR,
    exchange_token         INTEGER,
    isin                   VARCHAR,
    lot_size               INTEGER,
    tick_size              DECIMAL(9,4),
    qty_freeze             INTEGER,
    qty_multiplier         DECIMAL(12,4),
    trading_session        VARCHAR,
    symbol_description     VARCHAR,
    instrument_master_valid_from DATE,

    source_expiry_date_requested DATE,
    source_array           VARCHAR,
    source_array_index     INTEGER,
    source_endpoint        VARCHAR NOT NULL,
    discovered_task_id     BIGINT,
    parse_method           VARCHAR NOT NULL,
    parse_confidence       VARCHAR NOT NULL,
    parse_warnings         VARCHAR,
    first_seen_at          TIMESTAMP NOT NULL,
    last_seen_at           TIMESTAMP NOT NULL,
    sealed_at              TIMESTAMP,          -- life fully covered, never request again
    raw_payload            JSON
);

CREATE TABLE IF NOT EXISTS dim_resolution (
    res_id                   UTINYINT PRIMARY KEY,
    fyers_code               VARCHAR NOT NULL UNIQUE,
    seconds                  INTEGER NOT NULL,
    label                    VARCHAR NOT NULL,
    chart_interval           VARCHAR NOT NULL,
    max_days_per_request     INTEGER NOT NULL,
    availability_window_days INTEGER,
    is_intraday              BOOLEAN NOT NULL DEFAULT TRUE
);

-- Derived from observed spot bars, never from a hardcoded weekday or holiday rule: NSE and BSE
-- expiry weekdays have changed several times since 2022.
CREATE TABLE IF NOT EXISTS dim_trading_day (
    exchange       VARCHAR NOT NULL,
    trade_date     DATE NOT NULL,
    session_open   TIMESTAMP,
    session_close  TIMESTAMP,
    bar_count      INTEGER,
    contract_count INTEGER,
    derived_from   VARCHAR NOT NULL,   -- 'spot_bars' | 'seed'
    PRIMARY KEY (exchange, trade_date)
);

-- ---------------------------------------------------------------------------
-- Coverage, bounds and provenance
-- ---------------------------------------------------------------------------

-- Provenance is captured per fetch chunk rather than per row. A four byte per-row source id
-- would add roughly 2.4 GB to a 600 million row store for information already recoverable: an
-- interval lookup here yields task_id, which yields the full parameter set and the archived body.
CREATE TABLE IF NOT EXISTS candle_coverage (
    contract_id       INTEGER NOT NULL,
    res_id            UTINYINT NOT NULL,
    range_from        DATE NOT NULL,
    range_to          DATE NOT NULL,
    status            VARCHAR NOT NULL,   -- 'ok' | 'empty' | 'error'
    row_count         BIGINT NOT NULL,
    first_ts          TIMESTAMP,
    last_ts           TIMESTAMP,
    include_oi        BOOLEAN NOT NULL,
    columns_json      VARCHAR NOT NULL,
    schema_version    INTEGER,
    payload_sha256    VARCHAR,
    http_status       INTEGER,
    fyers_code        INTEGER,
    latency_ms        INTEGER,
    response_bytes    BIGINT,
    token_fingerprint VARCHAR,
    task_id           BIGINT NOT NULL,
    run_id            BIGINT,
    fetched_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (contract_id, res_id, range_from, range_to)
);

-- One row read for the chart adapter's window clamp, instead of a min(ts), max(ts) scan on
-- every chart load. It also drives the interval pill list.
CREATE TABLE IF NOT EXISTS contract_bounds (
    contract_id INTEGER NOT NULL,
    res_id      UTINYINT NOT NULL,
    first_ts    TIMESTAMP,
    last_ts     TIMESTAMP,
    row_count   BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (contract_id, res_id)
);

CREATE TABLE IF NOT EXISTS ingest_run (
    run_id           BIGINT PRIMARY KEY,
    job_id           VARCHAR NOT NULL,
    job_kind         VARCHAR NOT NULL,
    underlying_id    INTEGER,
    started_at       TIMESTAMP NOT NULL,
    finished_at      TIMESTAMP,
    status           VARCHAR NOT NULL,
    requests_made    INTEGER NOT NULL DEFAULT 0,
    rows_written     BIGINT NOT NULL DEFAULT 0,
    bytes_downloaded BIGINT NOT NULL DEFAULT 0,
    app_version      VARCHAR NOT NULL,
    error_text       VARCHAR
);

CREATE TABLE IF NOT EXISTS export_manifest (
    export_id    VARCHAR PRIMARY KEY,
    kind         VARCHAR NOT NULL,
    path         VARCHAR NOT NULL,
    filters_json JSON NOT NULL,
    row_count    BIGINT,
    byte_size    BIGINT,
    sha256       VARCHAR,
    created_at   TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

-- ---------------------------------------------------------------------------
-- Greeks and chain snapshots
-- ---------------------------------------------------------------------------

-- Created empty and stays empty until include_greeks ships on the expired historical-data
-- endpoint. It exists now so that day is a config flip and not a migration, and it is separate
-- so the nine column hot path never widens.
CREATE TABLE IF NOT EXISTS candle_greeks (
    contract_id INTEGER NOT NULL,
    res_id      UTINYINT NOT NULL,
    ts          TIMESTAMP NOT NULL,
    iv          DECIMAL(9,4),
    delta       DECIMAL(9,6),
    gamma       DECIMAL(12,8),
    theta       DECIMAL(12,6),
    vega        DECIMAL(12,6),
    rho         DECIMAL(12,6),
    fp          DECIMAL(11,4),
    src         VARCHAR NOT NULL
);

-- Captured for LIVE contracts, because expiry_flag (W or M) is available only in
-- /data/options-chain-v3 and never for expired contracts. Accumulating it from day one is the
-- only way expiry_cycle ever becomes authoritative rather than derived.
CREATE TABLE IF NOT EXISTS chain_snapshot (
    snapshot_ts   TIMESTAMP NOT NULL,
    underlying_id INTEGER NOT NULL,
    expiry_date   DATE NOT NULL,
    expiry_flag   VARCHAR,
    strike        DECIMAL(12,4),
    option_type   VARCHAR,
    fyers_symbol  VARCHAR,
    ltp           DECIMAL(11,4),
    bid           DECIMAL(11,4),
    ask           DECIMAL(11,4),
    volume        BIGINT,
    oi            BIGINT,
    prev_oi       BIGINT,
    iv            DECIMAL(9,4),
    delta         DECIMAL(9,6),
    gamma         DECIMAL(12,8),
    theta         DECIMAL(12,6),
    vega          DECIMAL(12,6),
    fp            DECIMAL(11,4),
    india_vix     DECIMAL(11,4),
    task_id       BIGINT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Symbol master as a slowly changing dimension
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dim_instrument_master (
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
    valid_from         DATE NOT NULL,
    valid_to           DATE,
    row_hash           VARCHAR NOT NULL,
    PRIMARY KEY (fytoken, valid_from)
);

CREATE TABLE IF NOT EXISTS symbol_master_snapshot (
    snapshot_date DATE NOT NULL,
    file          VARCHAR NOT NULL,
    url           VARCHAR NOT NULL,
    sha256        VARCHAR NOT NULL,
    row_count     INTEGER NOT NULL,
    byte_size     BIGINT NOT NULL,
    fetched_at    TIMESTAMP NOT NULL,
    PRIMARY KEY (snapshot_date, file)
);

-- ---------------------------------------------------------------------------
-- Resolution reference, mirrored from sqlite.ref_resolution
-- ---------------------------------------------------------------------------

-- res_id 100 is reserved for the derived daily series produced by v_candle_daily and is never
-- written by ingest, because Fyers has no daily candles for expired contracts.
--
-- Rewritten wholesale rather than upserted: the table carries two unique keys (res_id and
-- fyers_code), and DuckDB refuses an ON CONFLICT without a conflict target in that case.
DELETE FROM dim_resolution;
INSERT INTO dim_resolution
    (res_id, fyers_code, seconds, label, chart_interval, max_days_per_request,
     availability_window_days, is_intraday)
VALUES
    (1,  '5S',  5,     '5 seconds',  '5s',  30, 30,   TRUE),
    (2,  '1',   60,    '1 minute',   '1m',  95, NULL, TRUE),
    (3,  '2',   120,   '2 minutes',  '2m',  95, NULL, TRUE),
    (4,  '3',   180,   '3 minutes',  '3m',  95, NULL, TRUE),
    (5,  '5',   300,   '5 minutes',  '5m',  95, NULL, TRUE),
    (6,  '10',  600,   '10 minutes', '10m', 95, NULL, TRUE),
    (7,  '15',  900,   '15 minutes', '15m', 95, NULL, TRUE),
    (8,  '20',  1200,  '20 minutes', '20m', 95, NULL, TRUE),
    (9,  '30',  1800,  '30 minutes', '30m', 95, NULL, TRUE),
    (10, '45',  2700,  '45 minutes', '45m', 95, NULL, TRUE),
    (11, '60',  3600,  '1 hour',     '1h',  95, NULL, TRUE),
    (12, '120', 7200,  '2 hours',    '2h',  95, NULL, TRUE),
    (13, '180', 10800, '3 hours',    '3h',  95, NULL, TRUE),
    (14, '240', 14400, '4 hours',    '4h',  95, NULL, TRUE),
    (100,'D',   86400, '1 day',      '1d',  0,  NULL, FALSE);

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

-- Indirection so a future hot and cold split is invisible to every query above it.
CREATE OR REPLACE VIEW v_candle AS SELECT * FROM candles;

CREATE OR REPLACE VIEW v_contract_full AS
SELECT c.*, u.fyers_symbol AS underlying_symbol, u.display_name AS underlying_name,
       u.instrument_kind AS underlying_kind, e.expiry_cycle_derived, e.contract_id_lo,
       e.contract_id_hi, e.options_count, e.futures_count
  FROM dim_contract c
  JOIN dim_underlying u USING (underlying_id)
  LEFT JOIN dim_expiry e ON e.expiry_id = c.expiry_id;

-- Fyers has no daily candles for expired contracts, so daily must be aggregated here.
CREATE OR REPLACE VIEW v_candle_daily AS
SELECT contract_id,
       CAST(ts AS DATE)                     AS trade_date,
       first(open  ORDER BY ts)             AS open,
       max(high)                            AS high,
       min(low)                             AS low,
       last(close  ORDER BY ts)             AS close,
       sum(volume)                          AS volume,
       last(oi     ORDER BY ts)             AS oi,
       count(*)                             AS bar_count
  FROM candles
 WHERE res_id = 2                            -- one minute is the daily aggregation source
 GROUP BY contract_id, CAST(ts AS DATE);

CREATE OR REPLACE VIEW v_coverage_gaps AS
SELECT cov.contract_id, cov.res_id,
       cov.range_to                         AS gap_after,
       lead(cov.range_from) OVER w          AS gap_before,
       c.fyers_symbol, c.expiry_date
  FROM candle_coverage cov
  JOIN dim_contract c USING (contract_id)
 WINDOW w AS (PARTITION BY cov.contract_id, cov.res_id ORDER BY cov.range_from)
 QUALIFY lead(cov.range_from) OVER w > cov.range_to + INTERVAL 1 DAY;

CREATE OR REPLACE VIEW v_data_health AS
SELECT 'duplicate_keys' AS check_name,
       count(*)         AS offending
  FROM (SELECT contract_id, res_id, ts FROM candles
         GROUP BY 1,2,3 HAVING count(*) > 1)
UNION ALL
SELECT 'coverage_row_mismatch',
       count(*)
  FROM (SELECT cov.contract_id, cov.res_id,
               sum(cov.row_count) AS claimed,
               (SELECT count(*) FROM candles k
                 WHERE k.contract_id = cov.contract_id AND k.res_id = cov.res_id) AS actual
          FROM candle_coverage cov
         WHERE cov.status = 'ok'
         GROUP BY 1,2 HAVING claimed <> actual)
UNION ALL
SELECT 'contracts_with_no_bars',
       count(*)
  FROM dim_contract c
 WHERE c.kind <> 'SPOT'
   AND NOT EXISTS (SELECT 1 FROM contract_bounds b WHERE b.contract_id = c.contract_id);
