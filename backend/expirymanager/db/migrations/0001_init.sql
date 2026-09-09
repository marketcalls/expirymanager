-- 0001_init: configuration, crypto key ledger, local identity, broker credentials and tokens.
--
-- schema_version is not created here. The migration runner owns it, because it has to exist
-- before the first migration can be recorded as applied.

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Seeded so a fresh install has a complete, inspectable configuration row set rather than
-- values that only exist as Python defaults. settings_store.py still carries a code default
-- for every key, so a row deleted by hand does not break startup.
INSERT INTO settings (key, value_json, updated_at) VALUES
    ('plan_tier',                    '"standard"',            strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('throttle_per_second',          '8',                     strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('throttle_per_minute',          '170',                   strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('throttle_in_flight',           '6',                     strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('daily_budget',                 '100000',                strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('budget_reserve_fraction',      '0.7',                   strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('worker_count',                 '8',                     strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('chunk_days',                   '95',                    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('default_resolutions',          '["1","5","15","60"]',   strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('include_oi_default',           'true',                  strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('option_life_days',             '200',                   strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('future_life_days',             '400',                   strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('estimate_confirm_threshold',   '5000',                  strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('raw_payload_capture',          '"discovery"',           strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('request_log_retention_days',   '90',                    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('cookie_secure',                'true',                  strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('chart_persist',                'true',                  strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('symbol_year_window_lo',        '2015',                  strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('symbol_year_window_hi_offset', '5',                     strftime('%Y-%m-%dT%H:%M:%fZ','now'));

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

-- Encrypted-column tables use TEXT uuid primary keys, because the location-bound AAD needs
-- row_id before the INSERT and AUTOINCREMENT would force an insert-then-update.
CREATE TABLE broker_credential (
    credential_id  TEXT PRIMARY KEY,        -- uuid4, known before encryption
    broker         TEXT NOT NULL DEFAULT 'fyers',
    label          TEXT NOT NULL,
    app_id         TEXT NOT NULL,           -- not secret, of the form 'XXXXXXXXXX-100'
    app_secret_enc BLOB NOT NULL,           -- EM1 envelope
    redirect_uri   TEXT NOT NULL,
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
    generation         INTEGER NOT NULL DEFAULT 1,  -- bumped on every successful login
    token_fingerprint  TEXT NOT NULL,       -- sha256 of the access token, safe to log and to store on coverage rows
    issued_at          TEXT NOT NULL,
    access_expires_at  TEXT,                -- decoded from the JWT exp claim, never assumed
    refresh_expires_at TEXT,
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
