-- 0002_pipeline: the declared underlying registry, schedules, the job and task ledger, the
-- durable request budget, pipeline mode, exports metadata, notifications and the audit log.
--
-- schedule is created before job because job.schedule_id references it. SQLite resolves foreign
-- keys lazily, but declaring in dependency order keeps the file readable as a schema.

CREATE TABLE underlying_registry (
    underlying_id        INTEGER PRIMARY KEY,   -- mirrors dim_underlying.underlying_id
    fyers_symbol         TEXT NOT NULL UNIQUE,  -- 'NSE:NIFTY50-INDEX'
    root                 TEXT NOT NULL,         -- 'NIFTY', authoritative from the expiry-dates echo
    exchange             TEXT NOT NULL CHECK (exchange IN ('NSE','BSE','MCX')),
    segment              TEXT NOT NULL CHECK (segment IN ('CM','FO','CD','COM')),
    -- CURRENCY is admissible because the currency-derivative guard was removed: a user may
    -- register a CD underlying, and the symbol parser already classifies one.
    instrument_kind      TEXT NOT NULL CHECK (instrument_kind IN ('INDEX','EQUITY','COMMODITY','CURRENCY')),
    display_name         TEXT NOT NULL,
    data_from            TEXT NOT NULL,         -- exchange availability floor as yyyy-mm-dd
    default_resolutions  TEXT NOT NULL,         -- JSON array of Fyers resolution codes
    include_oi           INTEGER NOT NULL DEFAULT 1,
    option_life_days     INTEGER NOT NULL DEFAULT 200,
    future_life_days     INTEGER NOT NULL DEFAULT 400,
    spot_contract_id     INTEGER NOT NULL,      -- reserved id in 1..999
    resolved_root_echo   TEXT,                  -- exact data.symbol returned by expiry-dates
    resolved_at          TEXT,
    is_builtin           INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    notes                TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

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

-- One row is one outbound Fyers request. The same row is the work queue entry, the retry record
-- and the request provenance record, so checkpoint granularity equals request granularity.
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

-- idx_task_dispatch is the lease index. The lease statement orders by (priority, job_id, seq)
-- over pending rows only, so the partial index both filters and supplies the ordering.
CREATE INDEX idx_task_dispatch  ON task(priority, job_id, seq) WHERE state = 'pending';
CREATE INDEX idx_task_ready     ON task(not_before)            WHERE state = 'pending';
CREATE INDEX idx_task_lease     ON task(lease_expires_at)      WHERE state = 'leased';
CREATE INDEX idx_task_job       ON task(job_id, state);
CREATE INDEX idx_task_contract  ON task(contract_id, resolution, range_from);

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
INSERT INTO pipeline_state (id, mode, reason, changed_at, changed_by)
VALUES (1, 'running', NULL, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'migration');

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
