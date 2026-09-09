# ExpiryManager Data Model

Two stores, one boundary rule.

**SQLite (`~/.expirymanager/config.sqlite3`)** owns what a human authored and what must survive a
DuckDB rebuild: credentials, settings, the declared underlying registry, jobs, tasks, schedules,
the daily API budget, exports metadata, holidays, notifications, the audit log.

**DuckDB (`~/.expirymanager/market.duckdb`)** owns what the market gave us: the analytic catalog,
the coverage ledger, the symbol master history and every candle. The catalog lives here and not
in SQLite because it is joined against `candles` on literally every query the chart, the chain
grid, the export builder and the Phase 2 backtester will run, and a cross-store join would force
each of those through Python.

`dim_underlying` is the one deliberate duplication: it is a writer-maintained mirror of
`sqlite.underlying_registry`, refreshed inside the same transaction as any catalog write, so
editing an underlying in the UI never queues behind the single DuckDB writer and no query ever
crosses engines.

---

## 1. SQLite schema

Applied by `db/migrate.py` from numbered files in `db/migrations/`. Every connection carries:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;        -- per connection, OFF by default, must be re-applied on connect
PRAGMA busy_timeout = 5000;
PRAGMA secure_delete = ON;       -- rotated tokens must not linger in free pages
PRAGMA trusted_schema = OFF;
PRAGMA cell_size_check = ON;
```

### 1.1 Migration bookkeeping

```sql
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    checksum    TEXT NOT NULL
);
```

### 1.2 Configuration and crypto

```sql
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- Seeded keys: plan_tier, throttle_per_second, throttle_per_minute, throttle_in_flight,
-- daily_budget, budget_reserve_fraction, worker_count, chunk_days, default_resolutions,
-- include_oi_default, option_life_days, future_life_days, estimate_confirm_threshold,
-- raw_payload_capture, request_log_retention_days, cookie_secure, chart_persist,
-- symbol_year_window_lo, symbol_year_window_hi_offset.

CREATE TABLE crypto_key (
    version       INTEGER PRIMARY KEY,      -- matches key_ver inside the EM1 envelope
    wrapped_dek   BLOB    NOT NULL,         -- EM1 envelope, AAD = b'expirymanager|kek-wrap|v1'
    kek_provider  TEXT    NOT NULL CHECK (kek_provider IN ('keyfile','keyring','passphrase')),
    kdf_params    TEXT,                     -- JSON, passphrase provider only
    state         TEXT    NOT NULL CHECK (state IN ('active','retiring','retired')),
    created_at    TEXT    NOT NULL,
    retired_at    TEXT,
    use_count     INTEGER NOT NULL DEFAULT 0
);
```

### 1.3 Local identity

```sql
CREATE TABLE app_user (
    user_id         TEXT PRIMARY KEY,       -- uuid4 generated in Python
    username        TEXT NOT NULL UNIQUE,
    password_phc    TEXT NOT NULL,          -- full Argon2id PHC string
    created_at      TEXT NOT NULL,
    last_login_at   TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT
);

CREATE TABLE session (
    id_hash             BLOB PRIMARY KEY,   -- sha256 of the opaque id. The raw id is never stored.
    user_id             TEXT NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
    csrf_token          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    idle_expires_at     TEXT NOT NULL,      -- last_seen_at + 8h, slides
    absolute_expires_at TEXT NOT NULL,      -- created_at + 7d, never slides
    user_agent          TEXT,
    client_ip           TEXT
);
CREATE INDEX idx_session_user ON session(user_id);
CREATE INDEX idx_session_expiry ON session(absolute_expires_at);
```

### 1.4 Broker credentials and tokens

Encrypted-column tables use TEXT UUID primary keys, because the AAD needs `row_id` before the
INSERT and `INTEGER PRIMARY KEY AUTOINCREMENT` would force an insert-then-update.

```sql
CREATE TABLE broker_credential (
    credential_id  TEXT PRIMARY KEY,        -- uuid4, known before encryption
    broker         TEXT NOT NULL DEFAULT 'fyers',
    label          TEXT NOT NULL,
    app_id         TEXT NOT NULL,           -- not secret, e.g. 'XXXXXXXXXX-100'
    app_secret_enc BLOB NOT NULL,           -- EM1 envelope
    redirect_uri   TEXT NOT NULL,
    -- No pin column. SEBI discontinued the refresh token flow from 1 April 2026, so a PIN
    -- cannot buy unattended refresh and would only add a fourth secret to protect.
    plan           TEXT NOT NULL DEFAULT 'standard' CHECK (plan IN ('standard','prime')),
    key_ver        INTEGER NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE broker_token (
    token_id           TEXT PRIMARY KEY,    -- uuid4
    credential_id      TEXT NOT NULL REFERENCES broker_credential(credential_id) ON DELETE CASCADE,
    access_token_enc   BLOB NOT NULL,
    refresh_token_enc  BLOB,
    key_ver            INTEGER NOT NULL,
    generation         INTEGER NOT NULL DEFAULT 1,  -- bumped on every successful refresh or login
    token_fingerprint  TEXT NOT NULL,       -- sha256 of the access token, safe to log and to store on coverage rows
    issued_at          TEXT NOT NULL,
    access_expires_at  TEXT,                -- decoded from the JWT exp claim, never assumed
    refresh_expires_at TEXT,                -- issued_at + 15 days, documented refresh token life
    state              TEXT NOT NULL CHECK (state IN ('active','expiring','expired','needs_reauth','revoked')),
    last_error         TEXT,
    revoked_at         TEXT
);
CREATE INDEX idx_token_credential ON broker_token(credential_id, state);

CREATE TABLE oauth_state (
    state_hash      BLOB PRIMARY KEY,       -- sha256 of the random state value
    session_id_hash BLOB NOT NULL,
    credential_id   TEXT NOT NULL REFERENCES broker_credential(credential_id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,          -- created_at + 10 minutes
    used_at         TEXT
);
```

### 1.5 The declared underlying universe

```sql
CREATE TABLE underlying_registry (
    underlying_id        INTEGER PRIMARY KEY,   -- mirrors dim_underlying.underlying_id
    fyers_symbol         TEXT NOT NULL UNIQUE,  -- 'NSE:NIFTY50-INDEX'
    root                 TEXT NOT NULL,         -- 'NIFTY', authoritative from the expiry-dates echo
    exchange             TEXT NOT NULL CHECK (exchange IN ('NSE','BSE','MCX')),
    segment              TEXT NOT NULL CHECK (segment IN ('CM','FO','CD','COM')),
    instrument_kind      TEXT NOT NULL CHECK (instrument_kind IN ('INDEX','EQUITY','COMMODITY','CURRENCY')),
    display_name         TEXT NOT NULL,
    data_from            TEXT NOT NULL,         -- exchange availability floor as yyyy-mm-dd
    default_resolutions  TEXT NOT NULL,         -- JSON array of Fyers resolution codes
    include_oi           INTEGER NOT NULL DEFAULT 1,
    option_life_days     INTEGER NOT NULL DEFAULT 200,
    future_life_days     INTEGER NOT NULL DEFAULT 400,
    spot_contract_id     INTEGER NOT NULL,      -- reserved id in 1..999, see section 2.2
    resolved_root_echo   TEXT,                  -- exact data.symbol returned by expiry-dates
    resolved_at          TEXT,
    is_builtin           INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    notes                TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
```

Seeded by migration `0004_underlyings.sql`, all with `is_builtin = 1`:

| underlying_id | fyers_symbol | root | exchange | kind | data_from | spot_contract_id |
|---|---|---|---|---|---|---|
| 1 | NSE:NIFTY50-INDEX | NIFTY | NSE | INDEX | 2022-01-03 | 1 |
| 2 | NSE:NIFTYBANK-INDEX | BANKNIFTY | NSE | INDEX | 2022-01-03 | 2 |
| 3 | BSE:SENSEX-INDEX | SENSEX | BSE | INDEX | 2023-08-07 | 3 |
| 4 | NSE:RELIANCE-EQ | RELIANCE | NSE | EQUITY | 2022-01-03 | 4 |

`root` for the seeds is the value the docs show; `resolved_root_echo` is filled on the first
expiry-dates call and a mismatch raises a notification rather than silently overwriting.

### 1.6 Jobs and the task ledger

The `task` table is the centre of the system. One row is one outbound Fyers request, and that
same row is simultaneously the work queue entry, the retry record and the request provenance
record. Checkpoint granularity therefore equals request granularity, which is the finest useful
unit and the only one that survives a hostile rate limit.

```sql
CREATE TABLE job (
    job_id            TEXT PRIMARY KEY,     -- uuid4
    kind              TEXT NOT NULL CHECK (kind IN (
                          'expiry_discovery','contract_discovery','candle_backfill',
                          'underlying_history','symbol_master','seconds_capture',
                          'chain_snapshot','gap_repair','export')),
    status            TEXT NOT NULL CHECK (status IN (
                          'draft','queued','running','paused','blocked_auth','blocked_rate',
                          'deferred_budget','completed','completed_with_errors',
                          'cancelled','failed')),
    params_json       TEXT NOT NULL,
    parent_job_id     TEXT REFERENCES job(job_id),   -- set on a retry-failed child job
    schedule_id       TEXT REFERENCES schedule(schedule_id),
    priority          INTEGER NOT NULL DEFAULT 100,  -- lower runs first
    est_requests      INTEGER NOT NULL DEFAULT 0,
    total_tasks       INTEGER NOT NULL DEFAULT 0,
    done_tasks        INTEGER NOT NULL DEFAULT 0,
    empty_tasks       INTEGER NOT NULL DEFAULT 0,
    failed_tasks      INTEGER NOT NULL DEFAULT 0,
    skipped_tasks     INTEGER NOT NULL DEFAULT 0,
    requests_used     INTEGER NOT NULL DEFAULT 0,
    rows_written      INTEGER NOT NULL DEFAULT 0,
    bytes_downloaded  INTEGER NOT NULL DEFAULT 0,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    block_reason      TEXT,
    error_text        TEXT,
    created_by        TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);
CREATE INDEX idx_job_status ON job(status, created_at DESC);

CREATE TABLE task (
    task_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT NOT NULL REFERENCES job(job_id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN (
                           'expiry_dates','underlying_symbols','candle_chunk','spot_chunk',
                           'symbol_master','chain_snapshot')),
    state              TEXT NOT NULL CHECK (state IN (
                           'pending','leased','done','empty','failed','skipped','cancelled')),
    priority           INTEGER NOT NULL DEFAULT 100,

    -- request identity
    underlying_id      INTEGER,
    contract_id        INTEGER,
    fyers_symbol       TEXT,
    expiry_date        TEXT,
    resolution         TEXT,
    range_from         TEXT,
    range_to           TEXT,
    include_oi         INTEGER NOT NULL DEFAULT 1,
    request_params_json TEXT,               -- verbatim query parameters, never a token

    -- scheduling
    parent_task_id     INTEGER REFERENCES task(task_id),
    attempt            INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL DEFAULT 4,
    not_before         TEXT NOT NULL,
    lease_owner        TEXT,
    lease_expires_at   TEXT,

    -- outcome and provenance
    http_status        INTEGER,
    fyers_s            TEXT,
    fyers_code         INTEGER,
    last_error_text    TEXT,
    latency_ms         INTEGER,
    response_bytes     INTEGER,
    row_count          INTEGER,
    first_ts           TEXT,
    last_ts            TEXT,
    columns_json       TEXT,                -- the verbatim columns array Fyers returned
    schema_version     INTEGER,
    payload_sha256     TEXT,
    raw_body_path      TEXT,                -- relative path under ~/.expirymanager/raw, when captured
    token_fingerprint  TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX idx_task_dispatch  ON task(priority, job_id, seq) WHERE state = 'pending';
CREATE INDEX idx_task_ready     ON task(not_before)            WHERE state = 'pending';
CREATE INDEX idx_task_lease     ON task(lease_expires_at)      WHERE state = 'leased';
CREATE INDEX idx_task_job       ON task(job_id, state);
CREATE INDEX idx_task_contract  ON task(contract_id, resolution, range_from);
```

`raw_payload_capture` in settings controls `raw_body_path`. Values: `none`, `discovery` (default,
captures expiry-dates, underlying-symbols, symbol master and chain snapshots), `all` (adds candle
payloads, which is gigabytes per backfill and is only for debugging).

### 1.7 Schedules

`sqlite.schedule` is the source of truth. APScheduler runs a `MemoryJobStore` rebuilt from this
table on startup and after every mutation, so what the user sees on the schedules screen is
exactly what will fire, and a deleted schedule can never be resurrected by a pickled trigger.

```sql
CREATE TABLE schedule (
    schedule_id           TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    kind                  TEXT NOT NULL,     -- matches job.kind
    cron                  TEXT NOT NULL,     -- five field cron, evaluated in `timezone`
    timezone              TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    params_json           TEXT NOT NULL,
    enabled               INTEGER NOT NULL DEFAULT 1,
    trading_days_only     INTEGER NOT NULL DEFAULT 1,
    misfire_grace_seconds INTEGER NOT NULL DEFAULT 3600,
    max_requests_per_run  INTEGER,
    is_builtin            INTEGER NOT NULL DEFAULT 0,
    last_fired_at         TEXT,
    next_fire_at          TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE schedule_run (
    run_id      TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedule(schedule_id) ON DELETE CASCADE,
    fired_at    TEXT NOT NULL,
    job_id      TEXT REFERENCES job(job_id),
    outcome     TEXT NOT NULL CHECK (outcome IN (
                    'enqueued','skipped_disabled','skipped_holiday','skipped_needs_auth',
                    'skipped_blocked','skipped_budget','error')),
    note        TEXT
);
CREATE INDEX idx_schedule_run ON schedule_run(schedule_id, fired_at DESC);
```

### 1.8 Rate budget, pipeline state, notifications, audit

```sql
CREATE TABLE api_budget (
    ist_date          TEXT PRIMARY KEY,      -- yyyy-mm-dd in Asia/Kolkata
    plan              TEXT NOT NULL,
    plan_limit_day    INTEGER NOT NULL,      -- 100000 standard, 200000 prime
    plan_limit_minute INTEGER NOT NULL,      -- 200 standard, 600 prime
    requests_used     INTEGER NOT NULL DEFAULT 0,
    minute_violations INTEGER NOT NULL DEFAULT 0,   -- the three strikes counter
    last_429_at       TEXT,
    blocked_until     TEXT,
    updated_at        TEXT NOT NULL
);

CREATE TABLE rate_event (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN (
                 'throttle_wait','http_429','fyers_429','breaker_open','breaker_close',
                 'budget_warning','budget_exhausted')),
    endpoint TEXT,
    detail   TEXT
);

CREATE TABLE pipeline_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    mode       TEXT NOT NULL CHECK (mode IN (
                   'running','paused_auth','paused_rate','paused_user',
                   'stopped_budget','stopped_fatal')),
    reason     TEXT,
    changed_at TEXT NOT NULL,
    changed_by TEXT
);

CREATE TABLE market_holiday (
    exchange     TEXT NOT NULL,
    holiday_date TEXT NOT NULL,
    description  TEXT,
    source       TEXT NOT NULL DEFAULT 'seed',
    PRIMARY KEY (exchange, holiday_date)
);

CREATE TABLE export_job (
    export_id     TEXT PRIMARY KEY,
    job_id        TEXT REFERENCES job(job_id),
    format        TEXT NOT NULL CHECK (format IN ('parquet','csv')),
    layout        TEXT NOT NULL CHECK (layout IN ('single','hive')),
    compression   TEXT,
    scope_json    TEXT NOT NULL,
    file_path     TEXT,
    manifest_path TEXT,
    row_count     INTEGER,
    byte_size     INTEGER,
    sha256        TEXT,
    status        TEXT NOT NULL CHECK (status IN ('queued','running','ready','failed','deleted')),
    error_text    TEXT,
    created_at    TEXT NOT NULL,
    finished_at   TEXT,
    expires_at    TEXT
);

CREATE TABLE notification (
    notification_id TEXT PRIMARY KEY,
    level           TEXT NOT NULL CHECK (level IN ('info','warning','error')),
    code            TEXT NOT NULL,   -- needs_reauth, budget_exhausted, breaker_open,
                                     -- seconds_window_missed, compaction_suggested, parse_ambiguous
    title           TEXT NOT NULL,
    body            TEXT,
    created_at      TEXT NOT NULL,
    read_at         TEXT,
    dismissed_at    TEXT
);

CREATE TABLE audit_log (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT,
    detail_json TEXT
);
```

### 1.9 Reference tables

```sql
CREATE TABLE ref_exchange (
    code                INTEGER PRIMARY KEY,   -- NSE 10, MCX 11, BSE 12. Not alphabetical.
    name                TEXT NOT NULL UNIQUE,
    data_available_from TEXT NOT NULL
);
INSERT INTO ref_exchange VALUES (10,'NSE','2022-01-03'),(11,'MCX','2022-01-03'),(12,'BSE','2023-08-07');

CREATE TABLE ref_segment (
    code INTEGER PRIMARY KEY,   -- 10 CM, 11 FO, 12 CD, 20 COM
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE ref_instrument_type (
    segment_code INTEGER NOT NULL REFERENCES ref_segment(code),
    type_code    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    PRIMARY KEY (segment_code, type_code)
);
-- Instrument type integers are reused across segments, so a type is only meaningful joined
-- with its segment: FO 11 FUTIDX, 13 FUTSTK, 14 OPTIDX, 15 OPTSTK; CM 0 EQ, 9 ETF, 10 INDEX;
-- CD 16 FUTCUR, 19 OPTCUR; COM 30 FUTCOM, 31 and 37 OPTFUT, 32 OPTCOM.

CREATE TABLE ref_resolution (
    fyers_code              TEXT PRIMARY KEY,
    res_id                  INTEGER NOT NULL UNIQUE,
    seconds                 INTEGER NOT NULL,
    label                   TEXT NOT NULL,
    chart_interval          TEXT NOT NULL,   -- the openalgo-charts interval code
    max_days_per_request    INTEGER NOT NULL,
    availability_window_days INTEGER,        -- 30 for second resolutions, NULL otherwise
    is_intraday             INTEGER NOT NULL DEFAULT 1
);
```

Seeded resolutions. `max_days_per_request` is 95 for every minute code, deliberately under the
documented 100, because the docs do not say whether the limit is calendar or trading days.

| fyers_code | res_id | seconds | chart_interval | max_days | availability_window_days |
|---|---|---|---|---|---|
| 5S | 1 | 5 | 5s | 30 | 30 |
| 1 | 2 | 60 | 1m | 95 | |
| 2 | 3 | 120 | 2m | 95 | |
| 3 | 4 | 180 | 3m | 95 | |
| 5 | 5 | 300 | 5m | 95 | |
| 10 | 6 | 600 | 10m | 95 | |
| 15 | 7 | 900 | 15m | 95 | |
| 20 | 8 | 1200 | 20m | 95 | |
| 30 | 9 | 1800 | 30m | 95 | |
| 45 | 10 | 2700 | 45m | 95 | |
| 60 | 11 | 3600 | 1h | 95 | |
| 120 | 12 | 7200 | 2h | 95 | |
| 180 | 13 | 10800 | 3h | 95 | |
| 240 | 14 | 14400 | 4h | 95 | |

`res_id` 100 is reserved for the derived daily series produced by `v_candle_daily`. It is never
written by ingest, because Fyers has no daily candles for expired contracts.

---

## 2. DuckDB schema

Connection config, pinned in `db/duck.py` rather than inherited:

```python
duckdb.connect(paths.duckdb, config={
    "TimeZone": "Asia/Kolkata",
    "threads": 6,                    # not 10: one export must not starve the chart queries
    "memory_limit": "4GB",           # the default is 80 percent of RAM, far too greedy here
    "temp_directory": str(paths.tmp),# the default '.tmp' is relative to the process cwd
    "preserve_insertion_order": "false",
    "checkpoint_threshold": "64MB",
})
```

### 2.1 The fact table

```sql
CREATE TABLE IF NOT EXISTS candles (
    contract_id INTEGER      NOT NULL,
    res_id      UTINYINT     NOT NULL,
    ts          TIMESTAMP    NOT NULL,   -- bar open, naive, IST wall clock
    open        DECIMAL(11,4) NOT NULL,
    high        DECIMAL(11,4) NOT NULL,
    low         DECIMAL(11,4) NOT NULL,
    close       DECIMAL(11,4) NOT NULL,
    volume      BIGINT       NOT NULL,
    oi          BIGINT                   -- NULL when include_oi was 0, and for indices
);
```

Nine columns. No primary key, no unique constraint, no index. Physical sort order
`(contract_id, res_id, ts)` is the only index and it is maintained by inserting in that order and
by the compaction routine.

Every one of those choices is measured rather than assumed:

| Choice | Measurement |
|---|---|
| `DECIMAL(11,4)` prices | 19.30 bytes/row, against 15.21 for `DECIMAL(9,2)`, 17.36 for DOUBLE and 22.28 for FLOAT. `DECIMAL(9,2)` is the smallest option and was the original choice, but it cannot represent the 0.0025 currency-derivative tick, and the product lets a user register their own underlying. Widening after the first backfill means rewriting every row, so the 27 percent extra disk is paid up front. FLOAT is larger than DOUBLE because DuckDB's ALP compression degrades to the ALPRD raw-bits fallback on 32 bits. |
| No PRIMARY KEY | Adding `PRIMARY KEY (contract_id, ts)` to 5,000,000 rows grew the file from 75.5 MB to 341.6 MB and slowed the load from 0.45 s to 2.02 s, with zero point-lookup benefit (0.41 ms against 0.40 ms). |
| Sort key `(contract_id, res_id, ts)` | `WHERE contract_id = X` takes 0.1 ms sorted against 2.6 ms when inserted in ts order, a 26x difference purely from zone map pruning. |
| Naive IST `TIMESTAMP` | DuckDB's session TimeZone defaults to the host OS zone and `to_timestamp` returns TIMESTAMPTZ, so relying on either renders differently on a container and on the user's machine. India has had a fixed UTC+05:30 offset since 1945, so naive IST is lossless. |
| `res_id UTINYINT` in one table | Compression is `Constant` inside every row group, so it costs effectively nothing, and one table keeps one sort order. |

Sizing at 19.30 bytes per row: the four seed underlyings over 2022 to 2026 at one minute land
around 400 to 600 million rows, which is 8 to 12 GB. A pessimistic one billion rows is about
19 GB. A single 19 GB DuckDB file is routine and is not a reason to shard.

Ingest guard on precision, not on segment. There is no segment allowlist and no refusal of
Currency Derivatives: `DECIMAL(11,4)` is exact for the 0.05 NSE and BSE tick and for the 0.0025
currency tick alike. What `db/arrow.py` does enforce is that a price needing more than four
decimal places, or exceeding the DECIMAL(11,4) range, raises rather than rounds. Silently
rounding a price is the failure this guard exists to prevent, and it is the reason the price
columns are DECIMAL at all rather than DOUBLE.

### 2.2 Contract identity and the id keyspace

`contract_id` is not an arbitrary surrogate. It is allocated so that physical clustering does the
work an index would otherwise do.

```
  1 ..    999   reserved for SPOT rows, one per underlying, allocated at registry insert.
                Underlyings therefore cluster at the very front of the sort order, so a spot
                scan touches a handful of row groups.

1000 .. 2^31-1  allocated in one padded contiguous block per (underlying_id, expiry_date)
                discovery batch, block size = ceil(contract_count / 1024) * 1024, minimum 1024.
                Within a block, ids are assigned ordered by (strike, option_type, instrument
                class) so that a plus-or-minus-N strike band around ATM is itself a narrow
                contiguous id range.
```

Underlying index and equity bars live in the same `candles` table, as contracts of kind `SPOT`
with a NULL expiry. That unification is deliberate: the two hardest Phase 2 joins, option to spot
for moneyness and ATM selection, become a self join on the leading sort key inside one table
instead of a second table or a Python round trip, and charting the underlying reuses the identical
bars endpoint and chart adapter.

Consequence to accept: the block allocation order is fixed now, because renumbering later means
rewriting `candles`.

### 2.3 The catalog

```sql
CREATE SEQUENCE IF NOT EXISTS seq_contract_block START 1000;
CREATE SEQUENCE IF NOT EXISTS seq_expiry_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_run_id START 1;

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
    expiry_id             INTEGER PRIMARY KEY,
    underlying_id         INTEGER NOT NULL,
    expiry_date           DATE NOT NULL,
    has_futures           BOOLEAN NOT NULL DEFAULT FALSE,
    has_options           BOOLEAN NOT NULL DEFAULT FALSE,
    futures_count         INTEGER,
    options_count         INTEGER,
    contract_count        INTEGER,
    min_strike            DECIMAL(12,4),
    max_strike            DECIMAL(12,4),
    strike_step           DECIMAL(12,4),
    contract_id_lo        INTEGER,
    contract_id_hi        INTEGER,
    expiry_cycle_derived  VARCHAR,           -- 'W' | 'M', derived: last options expiry of a month is M
    expiry_cycle_source   VARCHAR NOT NULL DEFAULT 'derived',  -- 'derived' | 'option_chain'
    is_last_of_month      BOOLEAN,
    expiry_dow            UTINYINT,
    source_range_from     DATE,
    source_range_to       DATE,
    discovered_at         TIMESTAMP NOT NULL,
    contracts_discovered_at TIMESTAMP,
    discovered_task_id    BIGINT,
    UNIQUE (underlying_id, expiry_date)
);

CREATE TABLE IF NOT EXISTS dim_contract (
    contract_id            INTEGER PRIMARY KEY,
    underlying_id          INTEGER NOT NULL,
    expiry_id              INTEGER,
    fyers_symbol           VARCHAR NOT NULL UNIQUE,
    kind                   VARCHAR NOT NULL,   -- 'SPOT' | 'FUT' | 'OPT'
    instrument_class       VARCHAR NOT NULL,   -- 'INDEX' | 'EQUITY' | 'FUTIDX' | 'FUTSTK' | 'OPTIDX' | 'OPTSTK'
    exchange               VARCHAR NOT NULL,
    exchange_code          UTINYINT NOT NULL,
    segment                VARCHAR NOT NULL,
    segment_code           UTINYINT NOT NULL,
    ex_instrument_type     UTINYINT,
    root                   VARCHAR NOT NULL,

    -- expiry facts. expiry_date is the value passed to the discovery call and is authoritative.
    expiry_date            DATE,
    expiry_year            SMALLINT,
    expiry_month           UTINYINT,
    expiry_day             UTINYINT,
    expiry_dow             UTINYINT,
    parsed_expiry_date     DATE,               -- from the symbol string, cross-check only
    symbol_expiry_encoding VARCHAR,            -- 'MONTHLY_CODED' | 'WEEKLY_CODED' | 'NONE'
    expiry_cycle           VARCHAR,            -- 'W' | 'M'
    expiry_cycle_source    VARCHAR,            -- 'derived' | 'option_chain'

    -- option facts
    strike                 DECIMAL(12,4),
    strike_raw             VARCHAR,            -- the verbatim substring, e.g. '80.5'
    strike_ordinal         INTEGER,            -- rank within the expiry, for chain slicing
    option_type            VARCHAR,            -- 'CE' | 'PE'

    -- identity from the symbol master
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

    -- provenance and parse quality
    source_expiry_date_requested DATE,
    source_array           VARCHAR,            -- 'futures' | 'options'
    source_array_index     INTEGER,
    source_endpoint        VARCHAR NOT NULL,
    discovered_task_id     BIGINT,
    parse_method           VARCHAR NOT NULL,   -- 'hinted' | 'enumerated' | 'none'
    parse_confidence       VARCHAR NOT NULL,   -- 'exact' | 'scored' | 'quarantined'
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
```

`dim_trading_day` is derived from observed spot bars, never from a hardcoded weekday or holiday
rule. NSE and BSE expiry weekdays have changed several times since 2022, and any hardcoded rule
silently corrupts historical data.

Two columns exist specifically because a monthly coded symbol carries no expiry day.
`NSE:BANKNIFTY25MAR52000PE` gives only 2025-03, so `expiry_date` must come from the
`expiry_date` passed to the Get Expired Contracts call. `parsed_expiry_date` holds whatever the
symbol string said, and a mismatch is a data quality alarm rather than something to paper over.
`symbol_expiry_encoding` and `expiry_cycle` are separate columns because monthly coded is an
encoding and not a cycle: the last weekly expiry of a calendar month is written in monthly form.

Every strike column carries four decimal places for the same reason the candle prices do. A
currency-derivative strike is quoted to four places, and a strike that has been truncated is a
missing leg rather than a rounding error. Precision 12 stores in INT64 at either scale, so
`DECIMAL(12,4)` costs exactly what `DECIMAL(12,2)` did.

Primary keys on the catalog tables are wanted. The ART index cost is a function of row count, and
these tables hold on the order of 100,000 rows, not one billion.

### 2.4 Coverage, bounds and provenance

```sql
CREATE TABLE IF NOT EXISTS candle_coverage (
    contract_id     INTEGER NOT NULL,
    res_id          UTINYINT NOT NULL,
    range_from      DATE NOT NULL,
    range_to        DATE NOT NULL,
    status          VARCHAR NOT NULL,   -- 'ok' | 'empty' | 'error'
    row_count       BIGINT NOT NULL,
    first_ts        TIMESTAMP,
    last_ts         TIMESTAMP,
    include_oi      BOOLEAN NOT NULL,
    columns_json    VARCHAR NOT NULL,
    schema_version  INTEGER,
    payload_sha256  VARCHAR,
    http_status     INTEGER,
    fyers_code      INTEGER,
    latency_ms      INTEGER,
    response_bytes  BIGINT,
    token_fingerprint VARCHAR,
    task_id         BIGINT NOT NULL,
    run_id          BIGINT,
    fetched_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (contract_id, res_id, range_from, range_to)
);

CREATE TABLE IF NOT EXISTS contract_bounds (
    contract_id  INTEGER NOT NULL,
    res_id       UTINYINT NOT NULL,
    first_ts     TIMESTAMP,
    last_ts      TIMESTAMP,
    row_count    BIGINT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (contract_id, res_id)
);

CREATE TABLE IF NOT EXISTS ingest_run (
    run_id        BIGINT PRIMARY KEY,
    job_id        VARCHAR NOT NULL,
    job_kind      VARCHAR NOT NULL,
    underlying_id INTEGER,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    status        VARCHAR NOT NULL,
    requests_made INTEGER NOT NULL DEFAULT 0,
    rows_written  BIGINT NOT NULL DEFAULT 0,
    bytes_downloaded BIGINT NOT NULL DEFAULT 0,
    app_version   VARCHAR NOT NULL,
    error_text    VARCHAR
);

CREATE TABLE IF NOT EXISTS export_manifest (
    export_id   VARCHAR PRIMARY KEY,
    kind        VARCHAR NOT NULL,
    path        VARCHAR NOT NULL,
    filters_json JSON NOT NULL,
    row_count   BIGINT,
    byte_size   BIGINT,
    sha256      VARCHAR,
    created_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
-- Seeded: schema_version, app_version, duckdb_version, created_at,
-- candles.ts.timezone = 'Asia/Kolkata',
-- candles.ts.semantics = 'naive local wall clock at bar open',
-- ist_offset_seconds = '19800',
-- candles.price.type = 'DECIMAL(11,4)'.
```

`candle_coverage` is why provenance is captured at chunk granularity and not as a per-row source
column. A four byte per-row source id would add roughly 2.4 GB to a 600 million row store for
information that is already recoverable losslessly: the fetch chunk is the atomic unit of
ingestion, so an interval lookup on `(contract_id, res_id, ts)` yields the coverage row, which
yields `task_id`, which yields the full parameter set, latency, envelope code, the verbatim
columns array, the payload hash and the archived raw body. Every row is auditable and
re-fetchable at zero cost in the hot table.

`contract_bounds` exists because the chart adapter must clamp the widget's now-relative window
onto each contract's real traded range, and that has to be a single row read rather than a
`min(ts), max(ts)` scan on every chart load. It also drives the interval pill list.

`status = 'empty'` records `s: "no_data"` as fetched-and-empty. Without it the scheduler
re-requests the same dead range every night forever, and the UI cannot distinguish "this contract
never traded that week" from "we have not looked yet".

### 2.5 Greeks and chain snapshots

```sql
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
    src         VARCHAR NOT NULL   -- 'fyers_include_greeks' | 'chain_snapshot' | 'computed'
);

CREATE TABLE IF NOT EXISTS chain_snapshot (
    snapshot_ts   TIMESTAMP NOT NULL,
    underlying_id INTEGER NOT NULL,
    expiry_date   DATE NOT NULL,
    expiry_flag   VARCHAR,          -- 'W' | 'M', the ONLY authoritative source of the cycle
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
```

`candle_greeks` is created empty and stays empty until `include_greeks` ships on the expired
historical-data endpoint. It exists now so that day is a config flip and not a migration. It is
a separate table so the nine column hot path never widens.

`chain_snapshot` is captured for LIVE contracts, because `expiry_flag` (W or M) is available only
in `/data/options-chain-v3` and never for expired contracts. Accumulating it from day one is the
only way `expiry_cycle` ever becomes authoritative rather than derived.

### 2.6 Symbol master as a slowly changing dimension

```sql
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
```

Sources, all unauthenticated and therefore outside the API budget:
`https://public.fyers.in/sym_details/{NSE_CD,NSE_FO,NSE_COM,NSE_CM,BSE_CM,BSE_FO,MCX_COM}_sym_master.json`.

This is the single most important scheduled job for Phase 2, and it must run from day one. The
three expired endpoints return symbol strings only: no fytoken, no lot size, no tick size, no
freeze quantity. The symbol master contains only currently listed instruments, so an expired
contract's lot size is unrecoverable after the fact, and a backtester cannot size a position
without a dated lot size. A gap in this chain is permanent.

### 2.7 Views

```sql
CREATE OR REPLACE VIEW v_candle AS SELECT * FROM candles;
-- Indirection so a future hot and cold split is invisible to every query above it.

CREATE OR REPLACE VIEW v_contract_full AS
SELECT c.*, u.fyers_symbol AS underlying_symbol, u.display_name AS underlying_name,
       u.instrument_kind AS underlying_kind, e.expiry_cycle_derived, e.contract_id_lo,
       e.contract_id_hi, e.options_count, e.futures_count
  FROM dim_contract c
  JOIN dim_underlying u USING (underlying_id)
  LEFT JOIN dim_expiry e ON e.expiry_id = c.expiry_id;

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
-- Fyers has no daily candles for expired contracts, so daily must be aggregated here.

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
```

### 2.8 The shipped query vocabulary

Checked into `db/duck_macros.sql`, so a future backtester or a plain DuckDB CLI session gets the
same vocabulary the API uses and the two cannot drift.

```sql
CREATE OR REPLACE MACRO bars(p_contract_id, p_res_id, p_from, p_to) AS TABLE
SELECT ts, open, high, low, close, volume, oi
  FROM candles
 WHERE contract_id = p_contract_id
   AND res_id      = p_res_id
   AND ts >= p_from AND ts < p_to
 ORDER BY ts;

CREATE OR REPLACE MACRO spot_at(p_underlying_id, p_res_id, p_ts) AS TABLE
SELECT k.ts, k.close
  FROM candles k
  JOIN dim_underlying u ON u.spot_contract_id = k.contract_id
 WHERE u.underlying_id = p_underlying_id
   AND k.res_id = p_res_id
   AND k.ts <= p_ts
 ORDER BY k.ts DESC
 LIMIT 1;

CREATE OR REPLACE MACRO atm_strike(p_underlying_id, p_expiry, p_res_id, p_ts) AS TABLE
WITH spot AS (SELECT close FROM spot_at(p_underlying_id, p_res_id, p_ts))
SELECT c.strike
  FROM dim_contract c, spot
 WHERE c.underlying_id = p_underlying_id
   AND c.expiry_date   = p_expiry
   AND c.option_type   = 'CE'
 ORDER BY abs(c.strike - spot.close)
 LIMIT 1;

CREATE OR REPLACE MACRO chain_at(p_underlying_id, p_expiry, p_res_id, p_ts) AS TABLE
WITH ids AS (
    SELECT contract_id_lo AS lo, contract_id_hi AS hi
      FROM dim_expiry
     WHERE underlying_id = p_underlying_id AND expiry_date = p_expiry
)
SELECT c.strike, c.option_type, c.fyers_symbol, c.lot_size,
       k.open, k.high, k.low, k.close, k.volume, k.oi
  FROM candles k
  JOIN ids ON k.contract_id BETWEEN ids.lo AND ids.hi
  JOIN dim_contract c USING (contract_id)
 WHERE k.res_id = p_res_id
   AND k.ts     = p_ts
   AND c.kind   = 'OPT'
 ORDER BY c.strike, c.option_type;

CREATE OR REPLACE MACRO chain_window(p_underlying_id, p_expiry, p_res_id, p_from, p_to,
                                     p_strikes_each_side) AS TABLE
WITH atm AS (SELECT strike FROM atm_strike(p_underlying_id, p_expiry, p_res_id, p_from)),
     step AS (SELECT strike_step FROM dim_expiry
               WHERE underlying_id = p_underlying_id AND expiry_date = p_expiry),
     ids  AS (SELECT contract_id_lo AS lo, contract_id_hi AS hi FROM dim_expiry
               WHERE underlying_id = p_underlying_id AND expiry_date = p_expiry)
SELECT k.ts, c.strike, c.option_type, k.close, k.volume, k.oi
  FROM candles k
  JOIN ids ON k.contract_id BETWEEN ids.lo AND ids.hi
  JOIN dim_contract c USING (contract_id), atm, step
 WHERE k.res_id = p_res_id
   AND k.ts >= p_from AND k.ts < p_to
   AND c.kind = 'OPT'
   AND abs(c.strike - atm.strike) <= p_strikes_each_side * step.strike_step
 ORDER BY k.ts, c.strike, c.option_type;
```

---

## 3. The Phase 2 queries the schema is built for

Phase 2 is an options backtesting engine reading this store directly. It is not built now, but
these are the queries it will run and the schema is justified against them.

### 3.1 Chart data for one contract, the hot path today

```sql
SELECT epoch(ts) - 19800 AS time,   -- UTC seconds, exactly what openalgo-charts Bar.time wants
       open, high, low, close, volume, oi
  FROM candles
 WHERE contract_id = ? AND res_id = ?
   AND ts >= ? AND ts < ?
 ORDER BY ts;
```

Leading key predicate, zone map pruned. Measured 0.1 ms class on 5,000,000 rows. This is why the
sort key leads with `contract_id`: the alternative, sorting by `ts`, measured 2.6 ms for the same
query, a 26x penalty on the single query the chart issues most.

### 3.2 The whole option chain at one timestamp, the backtester inner loop

```sql
SELECT c.strike, c.option_type, c.lot_size, k.close, k.oi, k.volume
  FROM candles k
  JOIN dim_contract c USING (contract_id)
 WHERE k.contract_id BETWEEN ? AND ?    -- dim_expiry.contract_id_lo, contract_id_hi
   AND k.res_id = ?
   AND k.ts     = ?
 ORDER BY c.strike, c.option_type;
```

The `BETWEEN` is the whole justification for allocating contract ids in a contiguous padded block
per `(underlying, expiry)`. Without it this query is a catalog join followed by several hundred
scattered id lookups. With it, it prunes on the leading sort key and the zone map, and it touches
a bounded set of row groups. The backtester runs it once per bar per expiry, which is millions of
times over a multi-year run.

### 3.3 ATM lookup, joined to the underlying

```sql
WITH spot AS (
    SELECT k.close
      FROM candles k
      JOIN dim_underlying u ON u.spot_contract_id = k.contract_id
     WHERE u.underlying_id = ? AND k.res_id = ? AND k.ts <= ?
     ORDER BY k.ts DESC LIMIT 1
)
SELECT c.contract_id, c.strike, c.option_type
  FROM dim_contract c, spot
 WHERE c.underlying_id = ? AND c.expiry_date = ? AND c.kind = 'OPT'
 ORDER BY abs(c.strike - spot.close), c.option_type
 LIMIT 2;                              -- the ATM call and the ATM put
```

This is the query that justifies keeping spot bars in the same `candles` table with reserved low
contract ids. The spot lookup is a scan of a handful of row groups at the very front of the file,
in the same table, in the same transaction snapshot, with no second store and no Python round
trip. It is also why `strike` is `DECIMAL(12,4)` and never DOUBLE: strike equality and grouping
is the most common catalog predicate in options work, and floating point equality on strikes is a
known source of missing legs.

### 3.4 A strike band around ATM over a session

`chain_window(...)` above. Because ids inside a block are ordered by strike, a plus-or-minus-N
band is itself a narrow contiguous id range, so the band reads a fraction of the block rather
than the whole chain.

### 3.5 Daily series for a contract that has none

```sql
SELECT * FROM v_candle_daily WHERE contract_id = ? ORDER BY trade_date;
```

Fyers documents daily, weekly and monthly resolutions as coming soon for expired contracts, so
every daily series in this system is aggregated from one minute bars inside DuckDB.

### 3.6 Position sizing, which needs a dated lot size

```sql
SELECT m.min_lot_size, m.tick_size, m.qty_freeze
  FROM dim_instrument_master m
  JOIN dim_contract c ON c.fytoken = m.fytoken
 WHERE c.contract_id = ?
   AND m.valid_from <= ?          -- the trade date
   AND (m.valid_to IS NULL OR m.valid_to > ?);
```

Lot sizes change over time, so this is a slowly changing dimension and not a mutable column.
`dim_contract.lot_size` holds the value observed at discovery time as a convenience; the SCD-2
table is the authority for any date other than that one.

### 3.7 Coverage, which must never scan candles

```sql
SELECT c.fyers_symbol, c.strike, c.option_type,
       min(cov.range_from) AS have_from,
       max(cov.range_to)   AS have_to,
       sum(cov.row_count)  AS rows_held,
       count(*) FILTER (WHERE cov.status = 'empty') AS empty_chunks
  FROM candle_coverage cov
  JOIN dim_contract c USING (contract_id)
 WHERE c.underlying_id = ? AND c.expiry_date = ? AND cov.res_id = ?
 GROUP BY 1, 2, 3
 ORDER BY c.strike, c.option_type;
```

This reads only the small bookkeeping table, so the coverage heatmap stays instant no matter how
large `candles` grows. That is the reason `candle_coverage` exists at all rather than deriving
coverage with `min(ts), max(ts)` over the fact table.

### 3.8 Streaming to a vectorised consumer

Phase 2 should take Arrow rather than pandas: `cur.execute(sql).arrow()` and
`.fetch_record_batch(n)` avoid a pandas materialisation and let a vectorised backtester stream row
groups. `DECIMAL(11,4)` round trips as `decimal128(11,4)` in Arrow, `Decimal` in Polars and
`float64` via `.df()`. Division and `ln()` on DECIMAL auto-promote to DOUBLE, so returns and
greeks math needs no explicit casts.

---

## 4. Metadata captured, and where it comes from

The three expired endpoints are metadata-poor: they return symbol strings and OHLCV arrays and
nothing else. Maximum metadata capture is therefore assembled from four sources.

| Source | What it yields | Where it lands |
|---|---|---|
| Request provenance | endpoint, every query parameter, attempt number, latency, HTTP status, envelope code, response bytes, payload sha256, the verbatim columns array, schema_version, token fingerprint | `sqlite.task`, mirrored to `candle_coverage` |
| Response body, verbatim | the raw fragment per contract, the array it came from and its index | `dim_contract.raw_payload`, `source_array`, `source_array_index`, plus the gzipped body under `~/.expirymanager/raw` when `raw_payload_capture` allows |
| Symbology parsing | root, expiry parts, encoding, strike as DECIMAL plus the raw substring, option right, parse method, parse confidence, warnings | `dim_contract` |
| Symbol master SCD-2 | fytoken, exchange token, ISIN, lot size, tick size, freeze quantity, quantity multiplier, face value, trading session, description, and their validity windows | `dim_instrument_master`, promoted onto `dim_contract` at discovery |

Two provenance columns are load bearing and easy to get wrong:

- `source_expiry_date_requested` is the value passed to Get Expired Contracts. Keeping it separate
  from the parsed `expiry_date` is what makes the pipeline auditable, because a mismatch between
  them is a data quality alarm.
- `data.symbol` in the expiry-dates and underlying-symbols responses is stripped of the exchange
  prefix and the series suffix (send `NSE:SBIN-EQ`, get back `SBIN`). It is stored as
  `underlying_registry.resolved_root_echo` and is never used as a join key. The requested symbol
  is the canonical key.

---

## 5. Maintenance facts that constrain operations

- A DuckDB file only grows. Ten delete-then-insert rewrite cycles grew a 75.5 MB file to 77.6 MB,
  and `VACUUM` then `CHECKPOINT` left it at 77.6 MB. `VACUUM` recomputes statistics; it does not
  reclaim space.
- The only real reclaim is a full rewrite: `ATTACH` a new file, `CREATE TABLE AS SELECT ... ORDER BY
  contract_id, res_id, ts`, fsync, rename, reopen. Measured 48.2 MB down to 34.1 MB in 0.06 s on a
  small file. Because it requires closing and swapping the live file, it is a manual Optimise
  action in Settings with a free-disk-space guard, never a scheduled job.
- Backing up `market.duckdb` without `CHECKPOINT` first, or without its `.wal` sidecar, restores a
  database missing the most recent writes. The Backup action checkpoints first and copies both.
- Export benchmarks at 5,000,000 rows: Parquet ZSTD row group 122880 takes 0.38 s for 37.5 MB;
  Parquet SNAPPY 0.30 s for 75.7 MB; plain CSV 0.37 s for 324.3 MB; gzipped CSV 8.60 s for 78.4 MB.
  Query exports use ZSTD at row group 122880; archive exports use ZSTD at row group 1000000.
- Exports are denormalised (contract and underlying columns joined in) and carry both a formatted
  IST string and the raw UTC epoch, so downstream tools have no timezone ambiguity. The Hive
  archive layout also writes the catalog tables plus a manifest and a schema sidecar, which makes
  an archive export a restorable backup rather than a one-way dump.
