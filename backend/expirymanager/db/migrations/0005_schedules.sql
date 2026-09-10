-- 0005_schedules: the builtin schedule rows, and one widened outcome vocabulary.
--
-- Two things happen here.
--
-- 1. schedule_run gains the outcome 'completed'. Four builtin schedules do real work without
--    creating a job at all (token_health, token_logout, budget_reset, maintenance), and the
--    0002 vocabulary had no word for "this ran and finished". Recording those as 'enqueued'
--    would put a lie on the schedules screen, and 'error' is worse. SQLite cannot widen a CHECK
--    in place, so the table is rebuilt. Nothing references schedule_run, and the table is empty
--    on every existing install because no scheduler has ever run.
--
-- 2. The builtin schedules from PIPELINE.md section 10.2 are seeded, plus the three internal
--    timers the same section and section 7.3 name: budget_reset at 00:01, and the 03:00 IST
--    scheduled logout. Every row is editable in the UI; is_builtin only stops a delete and a
--    re-kind, because a builtin whose action code no longer has a row to drive it would be a
--    feature that silently disappeared.
--
-- schedule_id is a stable readable string rather than a uuid. These rows are addressed by name
-- from jobs_def.py and from the tests, and a uuid that differs per install would mean neither
-- could name one. User created schedules still take a uuid.

CREATE TABLE schedule_run_new (
    run_id      TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedule(schedule_id) ON DELETE CASCADE,
    fired_at    TEXT NOT NULL,
    job_id      TEXT REFERENCES job(job_id),
    outcome     TEXT NOT NULL CHECK (outcome IN (
                    'enqueued','completed','skipped_disabled','skipped_holiday',
                    'skipped_needs_auth','skipped_blocked','skipped_budget','error')),
    note        TEXT
);

INSERT INTO schedule_run_new (run_id, schedule_id, fired_at, job_id, outcome, note)
SELECT run_id, schedule_id, fired_at, job_id, outcome, note FROM schedule_run;

DROP INDEX idx_schedule_run;
DROP TABLE schedule_run;
ALTER TABLE schedule_run_new RENAME TO schedule_run;
CREATE INDEX idx_schedule_run ON schedule_run(schedule_id, fired_at DESC);

-- The builtin schedules. Priority lives inside params_json because the schedule table has no
-- priority column and the number belongs to the job the fire creates, not to the trigger.
--
-- An empty underlying_ids array means every active underlying, resolved at fire time. Writing
-- the four builtin ids in here instead would silently ignore a fifth underlying the user adds.
--
-- trading_days_only is 0 for everything that does not touch market data. The symbol master is
-- deliberately in that group: it costs no API budget, a missed day is unrecoverable, and the
-- public files are refreshed on non trading days too.
INSERT INTO schedule (
    schedule_id, name, kind, cron, timezone, params_json, enabled, trading_days_only,
    misfire_grace_seconds, max_requests_per_run, is_builtin, created_at, updated_at
) VALUES
    ('builtin_symbol_master', 'Symbol master snapshot', 'symbol_master',
     '15 8 * * *', 'Asia/Kolkata',
     '{"priority": 30}', 1, 0, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_seconds_capture', 'Seconds capture', 'seconds_capture',
     '15 16 * * 1-5', 'Asia/Kolkata',
     '{"priority": 5, "underlying_ids": [], "resolutions": ["5S"]}', 1, 1, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_expiry_discovery', 'Expiry discovery', 'expiry_discovery',
     '0 18 * * 1-5', 'Asia/Kolkata',
     '{"priority": 20, "underlying_ids": [], "lookback_days": 366, "forward_days": 60}',
     1, 1, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_contract_discovery', 'Contract discovery', 'contract_discovery',
     '15 18 * * 1-5', 'Asia/Kolkata',
     '{"priority": 20, "underlying_ids": [], "max_expiries": 60}', 1, 1, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_rolling_backfill', 'Rolling backfill', 'rolling_backfill',
     '30 18 * * 1-5', 'Asia/Kolkata',
     '{"priority": 40, "underlying_ids": [], "resolutions": [], "max_expiries": 40}',
     1, 1, 3600, 40000, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_underlying_history', 'Underlying spot history', 'underlying_history',
     '45 18 * * 1-5', 'Asia/Kolkata',
     '{"priority": 20, "underlying_ids": [], "resolutions": []}', 1, 1, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_chain_snapshot', 'Option chain snapshot', 'chain_snapshot',
     '25 15 * * 1-5', 'Asia/Kolkata',
     '{"priority": 25, "underlying_ids": []}', 1, 1, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_token_health', 'Token health', 'token_health',
     '0 * * * *', 'Asia/Kolkata',
     '{"warn_seconds": 86400, "park_seconds": 120}', 1, 0, 600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_gap_repair', 'Gap repair', 'gap_repair',
     '0 7 * * 0', 'Asia/Kolkata',
     '{"priority": 60, "underlying_ids": [], "resolutions": [], "max_expiries": 40}',
     1, 0, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    ('builtin_maintenance', 'Maintenance', 'maintenance',
     '0 2 * * *', 'Asia/Kolkata',
     '{"rate_event_retention_days": 30, "raw_payload_retention_days": 7}', 1, 0, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    -- 00:01 IST, before anything else in the new quota day. It inserts the new api_budget row,
    -- clears a budget based block and lifts stopped_budget so an overnight backfill continues.
    ('builtin_budget_reset', 'Budget day roll', 'budget_reset',
     '1 0 * * *', 'Asia/Kolkata',
     '{}', 1, 0, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    -- PIPELINE.md section 7.3. Token loss is a planned event, not a surprise expiry. Running
    -- jobs are parked, never failed, and resume at the exact task after the next login.
    ('builtin_token_logout', 'Scheduled daily logout', 'token_logout',
     '0 3 * * *', 'Asia/Kolkata',
     '{}', 1, 0, 3600, NULL, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'));
