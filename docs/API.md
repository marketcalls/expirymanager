# ExpiryManager HTTP API

All routes are under `/api/v1`, **with exactly one exception**: `GET /fyers/callback` is mounted at
the root, because the Fyers app's registered redirect URI is literally
`https://127.0.0.1:8000/fyers/callback` and Fyers matches it exactly.

The server speaks HTTPS on 127.0.0.1:8000 with a self-signed certificate it generates on first
run. There is exactly one browser origin in development (`https://127.0.0.1:5173`, with the Vite
proxy carrying `/api`) and one in production (`https://127.0.0.1:8000`), so `CORSMiddleware` is
never added to this codebase.

---

## 0. Conventions

**Authentication.** A session cookie `em_session` (HttpOnly, SameSite=Lax). Every route below is
`session` unless the table says otherwise. `public` means reachable with no session. `setup`
means reachable only while the app is unprovisioned.

**CSRF.** Every unsafe method (POST, PUT, PATCH, DELETE) requires the header `X-CSRF-Token`
matching `session.csrf_token`, and passes the `Sec-Fetch-Site` and Origin checks first. The only
exempt route is `GET /fyers/callback`, which is a cross-site top-level
navigation from Fyers and is protected by the single-use `state` parameter instead.

**Rate limits.** Enforced by `security/ratelimit.py` using `limits` 5.8.0 with `MemoryStorage`
and `MovingWindowRateLimiter`. Every limited response carries `RateLimit-Limit`,
`RateLimit-Remaining`, `RateLimit-Reset`, and a 429 additionally carries `Retry-After`.
The global fallback is 300 per minute per session.

**Error envelope.** Every non-2xx returns:

```json
{ "error": { "code": "plan_exceeds_budget", "message": "safe human text",
             "correlation_id": "8f2c...", "detail": { } } }
```

`message` is always safe to render. In production a 500 returns only the correlation id and a
generic message, because a Fyers error body can contain a token and must never reach the browser
console.

**Standard error cases**, not repeated per route: `401 not_authenticated`,
`403 csrf_invalid` / `cross_origin_rejected`, `404 not_found`, `409 needs_reauth` (the broker
token is parked; the frontend raises the banner), `422 validation_error` (FastAPI/Pydantic),
`429 rate_limited`, `503 pipeline_stopped`.

**Cache.** Every `/api` response carries `Cache-Control: no-store`.

---

## 1. Bootstrap and authentication

### GET /api/v1/bootstrap
Auth: `public`. Limit: 60/min.

The only fact the SPA reads before it decides which screen to render.

```json
{ "provisioned": true, "has_user": true, "has_credentials": true,
  "broker_connected": true, "token_state": "active",
  "token_expires_at": "2026-09-10T01:30:00+05:30", "needs_reauth": false,
  "data_dir": "/Users/you/.expirymanager", "app_version": "1.0.0",
  "duckdb_version": "1.5.5" }
```

### POST /api/v1/auth/setup
Auth: `setup`. Limit: 5/hour per ip.
Request `{ "username": str, "password": str (min 12 chars) }`.
Response `{ "user_id": str }` plus `Set-Cookie` for `em_session` and `em_csrf`.
Errors: `409 already_provisioned`.

### POST /api/v1/auth/login
Auth: `public`. Limit: 5 per 15 minutes per (ip, username), plus account lockout after 10 failures.
Request `{ "username": str, "password": str }`.
Response `{ "user_id": str, "username": str }` plus a rotated session cookie.
Errors: `401 invalid_credentials` (identical body and timing for unknown user and wrong password),
`423 account_locked` with `Retry-After`.

### POST /api/v1/auth/logout
Response `204`. Deletes the session row and clears both cookies.

### GET /api/v1/auth/me
Response `{ "user_id": str, "username": str, "session_expires_at": str }`.

### POST /api/v1/auth/password
Limit: 5/hour.
Request `{ "current_password": str, "new_password": str }`.
Response `204`. Deletes every session for the user and issues a fresh one.

---

## 2. Broker credentials and OAuth

### GET /api/v1/broker/fyers
Response:

```json
{ "credential_id": "uuid", "label": "Primary", "app_id": "XXXXXXXXXX-100",
  "redirect_uri": "https://127.0.0.1:8000/fyers/callback",
  "plan": "standard", "app_secret_configured": true, "pin_configured": false,
  "connected": true, "token_state": "active",
  "token_expires_at": "2026-09-10T01:30:00+05:30",
  "token_fingerprint": "3f9a1c2e", "last_error": null }
```

**A secret is never returned, not even masked.** A mask is an oracle and it tempts the frontend
into round-tripping it back on save. Only `*_configured` booleans cross the boundary.

### POST /api/v1/broker/fyers/credentials
Limit: 10/hour.
Request `{ "label": str, "app_id": str, "app_secret": str, "redirect_uri": str,
"plan": "standard"|"prime" }`.
Response `200` with the same shape as `GET /broker/fyers`.
Behaviour: `app_secret` is encrypted with the active DEK before the row is written; the
plaintext never leaves the request handler and never enters a log record. Saving new credentials
revokes any existing token.
There is no PIN field. SEBI discontinued the refresh token flow from 1 April 2026, so unattended
refresh is not possible and a stored PIN would be a fourth secret that buys nothing.
Errors: `400 invalid_redirect_uri` (must be an absolute https URL; the default and the value the
setup wizard offers with a copy button is `https://127.0.0.1:8000/fyers/callback`, which is what
must be registered on the Fyers dashboard because Fyers matches it exactly).

### POST /api/v1/broker/fyers/connect
Limit: 20/min.
Response `{ "authorize_url": str, "state_expires_at": str }`.
Behaviour: generates `secrets.token_urlsafe(32)`, stores only its sha256 in `oauth_state` bound to
the current session and credential with a 10 minute expiry, and returns the
`/api/v3/generate-authcode` URL with `client_id`, `redirect_uri`, `response_type=code` and
`state`. The SPA opens it in a new tab.
Errors: `400 no_credentials`.

### GET /fyers/callback
Auth: `session` (carried because cookies are `SameSite=Lax`, which is exactly why they are not
`Strict`). Limit: 20/min per ip. CSRF exempt by method.
Query: `s`, `code`, `auth_code`, `state`.
Response: `303 See Other` to `/settings?broker=connected` or `/settings?broker=failed&reason=...`.
Behaviour: looks up `sha256(state)`, compares with `hmac.compare_digest`, marks it consumed in the
same transaction that reads it, then POSTs `validate-authcode` with
`appIdHash = sha256(f"{app_id}:{app_secret}").hexdigest()`, encrypts both tokens, decodes the JWT
`exp`, sets `generation + 1`, sets the auth gate and moves `blocked_auth` jobs back to `queued`.
The 303 to a clean URL is mandatory: the callback URL contains `auth_code` in the query string, so
it lands in browser history, the Referer header and the access log.
Errors: all state failures return an identical generic failure reason (missing, expired, already
used, wrong session), because distinguishing them is an oracle.

### POST /api/v1/broker/fyers/callback/manual
Limit: 20/min.
Request `{ "redirected_url": str }`.
Response `200` with the broker status shape.
Behaviour: the fallback for the case where the automatic redirect does not land, for example a
certificate warning the user declines. The user pastes
the full URL they were redirected to; the server parses `auth_code` and `state` from it and runs
the identical code path as the callback.

### POST /api/v1/broker/fyers/test
Limit: 10/min.
Response `{ "ok": true, "endpoint": "expiry-dates", "latency_ms": 214, "requests_used_today": 12 }`.
Behaviour: spends exactly one governed request against expiry-dates for `NSE:NIFTY50-INDEX` over a
one week window.

### POST /api/v1/broker/fyers/disconnect
Response `204`. Revokes the token row (`state='revoked'`, ciphertext deleted) and leaves the
credentials in place.

---

## 3. Underlyings

### GET /api/v1/underlyings
Query: `active_only` (bool, default false).
Response: array of

```json
{ "underlying_id": 1, "fyers_symbol": "NSE:NIFTY50-INDEX", "root": "NIFTY",
  "exchange": "NSE", "segment": "CM", "instrument_kind": "INDEX",
  "display_name": "Nifty 50", "data_from": "2022-01-03",
  "default_resolutions": ["1","5"], "include_oi": true,
  "is_builtin": true, "is_active": true,
  "expiry_count": 214, "contract_count": 61240,
  "first_expiry": "2022-01-06", "last_expiry": "2026-09-04",
  "spot_bars": 512340, "spot_last_ts": "2026-09-08T15:29:00" }
```

### POST /api/v1/underlyings/resolve
Limit: 20/min.
Request `{ "query": str }` (a cash ticker fragment, a root, or a full Fyers symbol).
Response:

```json
{ "candidates": [ { "fyers_symbol": "NSE:NIFTYNXT50-INDEX", "root": "NIFTYNXT50",
    "exchange": "NSE", "segment": "CM", "instrument_kind": "INDEX",
    "under_fytoken": "1010000000026013", "fo_contract_count": 1820,
    "source": "symbol_master" } ],
  "probe": { "attempted": true, "root_echo": "NIFTYNXT50", "expiry_count": 12 } }
```

Behaviour: searches the latest `dim_instrument_master` snapshot, groups the FO master by
`under_symbol`, resolves `under_fytoken` against the CM master to get the exact cash ticker, then
spends exactly one governed expiry-dates request whose `data.symbol` echo is the authoritative
root. Nothing is written.
Errors: `409 needs_reauth` (the master search still returns, the probe does not),
`404 no_candidates`.

### POST /api/v1/underlyings
Limit: 30/min.
Request `{ "fyers_symbol": str, "display_name": str, "default_resolutions": [str],
"include_oi": bool, "option_life_days": int, "future_life_days": int }`.
Response `201` with the underlying object.
Behaviour: allocates the next reserved spot contract id from 1..999, writes
`underlying_registry`, mirrors `dim_underlying` through the writer, and adds the root to the
symbology root registry.
Errors: `409 already_exists`, `400 unresolved_root` (call `/resolve` first),
`507 spot_id_space_exhausted` (more than 999 underlyings, which is not a supported configuration).

### PATCH /api/v1/underlyings/{underlying_id}
Request: any of `display_name`, `default_resolutions`, `include_oi`, `option_life_days`,
`future_life_days`, `is_active`.
Response `200`. `fyers_symbol`, `root` and `spot_contract_id` are immutable.

### DELETE /api/v1/underlyings/{underlying_id}
Query: `purge_data` (bool, default false).
Response `204`.
Errors: `409 builtin_underlying` (the four seeds cannot be deleted, only deactivated),
`409 has_running_jobs`.

---

## 4. Expiries

### GET /api/v1/underlyings/{underlying_id}/expiries
Query: `from` (date), `to` (date), `res_id` (int, for the coverage rollup), `cursor`, `limit`
(default 200).
Response:

```json
{ "items": [ { "expiry_date": "2025-03-27", "expiry_dow": 3,
    "has_futures": true, "has_options": true,
    "futures_count": 3, "options_count": 428, "contract_count": 431,
    "expiry_cycle": "M", "expiry_cycle_source": "derived",
    "contracts_discovered_at": "2025-03-28T18:15:04",
    "contract_id_lo": 41984, "contract_id_hi": 42414,
    "min_strike": 18000.00, "max_strike": 28000.00, "strike_step": 50.00,
    "coverage": { "contracts_with_data": 431, "contracts_sealed": 431,
                  "chunks_ok": 431, "chunks_empty": 0, "chunks_missing": 0,
                  "rows": 1284900 } } ],
  "next_cursor": null }
```

### POST /api/v1/underlyings/{underlying_id}/expiries/discover
Limit: 30/min.
Request `{ "range_from": date, "range_to": date }`.
Response `202` with a job summary.
Behaviour: chunks the range into 366 day windows, creates an `expiry_discovery` job.
Errors: `400 range_before_floor`, `409 needs_reauth`.

### GET /api/v1/expiries/{underlying_id}/{expiry_date}/contracts
Query: `kind` (FUT|OPT), `option_type` (CE|PE), `strike_min`, `strike_max`, `cursor`, `limit`.
Response: paged `dim_contract` rows joined with `contract_bounds`.

---

## 5. Contracts

### GET /api/v1/contracts
Server-driven listing. Query: `underlying_id`, `expiry_from`, `expiry_to`, `kind`, `option_type`,
`strike_min`, `strike_max`, `symbol_contains`, `has_data` (bool), `sealed` (bool),
`sort` (one of `expiry_date`, `strike`, `fyers_symbol`, `rows`), `dir` (`asc`|`desc`),
`cursor`, `limit` (default 100, max 500).
Response:

```json
{ "items": [ { "contract_id": 42101, "fyers_symbol": "NSE:NIFTY25MAR23000CE",
    "underlying_id": 1, "kind": "OPT", "instrument_class": "OPTIDX",
    "expiry_date": "2025-03-27", "strike": 23000.00, "strike_raw": "23000",
    "option_type": "CE", "lot_size": 75, "tick_size": 0.0500,
    "fytoken": "101125032723000", "symbol_expiry_encoding": "MONTHLY_CODED",
    "expiry_cycle": "M", "parse_confidence": "exact",
    "sealed_at": "2025-03-28T18:41:12",
    "resolutions": [ { "res_id": 2, "fyers_code": "1", "rows": 2980,
                       "first_ts": "2025-01-02T09:15:00", "last_ts": "2025-03-27T15:29:00" } ] } ],
  "next_cursor": "eyJrIjo0MjEwMX0" }
```

### GET /api/v1/contracts/{contract_id}
Response: the full `v_contract_full` row plus every `contract_bounds` row and the coverage summary.

### GET /api/v1/contracts/{contract_id}/bounds
Limit: 240/min (the chart calls this on every symbol change).
Response:

```json
{ "contract_id": 42101, "fyers_symbol": "NSE:NIFTY25MAR23000CE",
  "resolutions": [ { "res_id": 2, "fyers_code": "1", "chart_interval": "1m",
                     "first_ts": 1735782300, "last_ts": 1743067140, "rows": 2980 } ] }
```

`first_ts` and `last_ts` are **UTC seconds**, ready for the chart adapter's window clamp with no
conversion. This is the single lookup that stops every expired contract rendering as "No bars".

---

## 6. Downloads and jobs

### POST /api/v1/downloads/plan
Limit: 60/min. **Costs zero Fyers requests.**
Request:

```json
{ "underlying_id": 1,
  "expiry_dates": ["2025-03-06","2025-03-13"],
  "resolutions": ["1","5"],
  "instrument_class": "OPT",          // "FUT" | "OPT" | "BOTH"
  "option_types": ["CE","PE"],
  "strike_scope": { "mode": "all" },  // or {"mode":"atm_band","steps":10}
                                      // or {"mode":"explicit","strikes":[23000,23100]}
  "include_oi": true,
  "force_refresh": false,
  "range_from": null, "range_to": null }
```

Response: the `PlanPreview` object documented in PIPELINE.md section 1.1.
Errors: `400 no_contracts_discovered` (with the count of discovery tasks the plan would add),
`400 strike_scope_needs_spot` (an ATM band needs underlying history for the expiry day; the
response names the one-click remedy), `409 needs_reauth`.

### POST /api/v1/downloads
Limit: 30/min.
Request: the same body plus `{ "confirm_requests": int }`, which must equal
`PlanPreview.requests_estimated` from the immediately preceding plan call. A mismatch is a `409`,
which is what stops a stale preview committing a job the user did not see priced.
Response `202`:

```json
{ "job_id": "uuid", "status": "queued", "total_tasks": 4820,
  "est_requests": 4820, "deferred": false }
```

Errors: `409 plan_changed`, `409 exceeds_budget` (unless `defer_to_tomorrow` is set, in which case
the job is created with status `deferred_budget`), `503 pipeline_stopped`.

### GET /api/v1/jobs
Query: `status`, `kind`, `since`, `cursor`, `limit` (default 25).
Response: paged job rows with progress counters and a computed `throughput_per_minute` and
`eta_seconds`.

### GET /api/v1/jobs/{job_id}
Response: the job row plus the live `GROUP BY state` aggregate over its tasks, the schedule that
fired it (if any), and the parent or child job ids.

### GET /api/v1/jobs/{job_id}/tasks
Query: `state`, `kind`, `contract_id`, `cursor`, `limit` (default 100).
Response: paged task rows. `request_params_json` is returned with any token-shaped value already
scrubbed; `raw_body_path` is returned as a boolean `has_raw_body`, never as a path the browser
could ask for.

### POST /api/v1/jobs/{job_id}/pause
### POST /api/v1/jobs/{job_id}/resume
### POST /api/v1/jobs/{job_id}/cancel
Limit: 30/min each. Response `200` with the updated job.
`cancel` sets `cancel_requested`; in-flight tasks finish and write their data.

### POST /api/v1/jobs/{job_id}/retry-failed
Limit: 30/min.
Response `202` `{ "job_id": "new-uuid", "parent_job_id": "...", "total_tasks": 37 }`.
Creates a child job containing copies of exactly the failed tasks. The parent's record is never
rewritten.

### GET /api/v1/coverage/grid
Query: `underlying_id` (required), `res_id`, `expiry_from`, `expiry_to`.
Response: the expiry-by-resolution matrix the heatmap renders, read entirely from
`candle_coverage` so it stays instant as `candles` grows.

### GET /api/v1/coverage/gaps
Query: `underlying_id`, `res_id`, `limit`.
Response: rows from `v_coverage_gaps` joined to `dim_contract`.

---

## 7. Data and charts

### GET /api/v1/bars
Limit: 240/min.
Query: `contract_id` (or `symbol`), `resolution` (Fyers code) or `interval` (chart code),
`from` (UTC seconds), `to` (UTC seconds), `include_oi` (bool, default true).
Response:

```json
{ "contract_id": 42101, "symbol": "NSE:NIFTY25MAR23000CE", "resolution": "1",
  "columns": ["timestamp","open","high","low","close","volume","open_interest"],
  "candles": [[1735782300, 145.20, 152.00, 141.05, 149.30, 187500, 4521225]] }
```

Fyers column order, epoch UTC seconds first, exactly `epoch(ts) - 19800`. The columnar shape is
the smallest representation on the wire, comes out of DuckDB with no per-row object construction,
and forces the frontend to map by the `columns` array exactly as the ingest path does, so adding
open interest or greeks later is a non-event on both sides.
Errors: `404 unknown_contract`, `400 unknown_resolution`, `400 range_too_large` (over 500,000
candles; the response names the maximum).

### GET /api/v1/bars/before
Query: `contract_id`, `resolution`, `before` (UTC seconds), `count` (default 500, max 5000).
Response: the same shape. Backs `chart.setHistoryLoader` paging.

### GET /api/v1/bars/oi
Query: `contract_id`, `resolution`, `from`, `to`.
Response `{ "points": [[1735782300, 4521225]] }`. Feeds the Tier-2 open interest indicator,
because `Bar` has no open-interest field.

### GET /api/v1/chain
Query: `underlying_id`, `expiry_date`, `resolution`, `ts` (UTC seconds).
Response:

```json
{ "underlying_id": 1, "expiry_date": "2025-03-27", "ts": 1743060900,
  "spot": 23142.55, "atm_strike": 23150.00,
  "rows": [ { "strike": 23100.00, "lot_size": 75,
              "ce": { "contract_id": 42101, "close": 92.4, "volume": 18200, "oi": 421275 },
              "pe": { "contract_id": 42315, "close": 61.8, "volume": 22100, "oi": 512400 } } ] }
```

Backed by the `chain_at` and `atm_strike` DuckDB macros, so the API, the export builder and any
future backtester share one definition of a chain slice and one definition of ATM.

### GET /api/v1/chain/atm
Query: `underlying_id`, `expiry_date`, `resolution`, `ts`.
Response `{ "spot": 23142.55, "atm_strike": 23150.00, "ce_contract_id": ..., "pe_contract_id": ... }`.

### GET /api/v1/spot/bars
Query: `underlying_id`, `resolution`, `from`, `to`. Same columnar shape as `/bars`. Serves the
underlying index and equity series, which live in the same `candles` table as contracts of kind
`SPOT`.

---

## 8. Exports

### POST /api/v1/exports
Limit: 10/min.
Request:

```json
{ "format": "parquet",            // "parquet" | "csv"
  "layout": "single",             // "single" | "hive"
  "compression": "zstd",
  "scope": { "underlying_id": 1, "expiry_from": "2025-01-01", "expiry_to": "2025-03-31",
             "resolutions": ["1"], "kind": "OPT", "include_catalog": true },
  "denormalise": true }
```

Response `202` `{ "export_id": "uuid", "job_id": "uuid", "status": "queued" }`.
Behaviour: runs as a normal job so it reports progress like a download. `COPY TO` with
`ROW_GROUP_SIZE 122880` for a single query export and `1000000` for a Hive archive. CSV carries
both a formatted IST string and the raw UTC epoch. `include_catalog` writes `dim_contract`,
`dim_underlying`, `dim_expiry` and `dim_resolution` alongside, which makes the archive a
restorable backup rather than a one-way dump. Written to a temp name and atomically renamed.
Errors: `507 insufficient_disk` (the estimate plus a 20 percent margin exceeds free space).

### GET /api/v1/exports
Response: paged `export_job` rows with `row_count`, `byte_size`, `sha256` and `status`.

### GET /api/v1/exports/{export_id}/file
Response: `FileResponse` with `Content-Disposition: attachment`. The path is re-derived from
`export_job.file_path` and asserted to resolve inside `~/.expirymanager/exports` after
`Path.resolve()`, so a stored path can never traverse out.
Errors: `409 not_ready`, `410 file_missing`.

### DELETE /api/v1/exports/{export_id}
Response `204`. Deletes the file and marks the row `deleted`.

---

## 9. Schedules

### GET /api/v1/schedules
Response: array of

```json
{ "schedule_id": "uuid", "name": "Rolling backfill", "kind": "rolling_backfill",
  "cron": "30 18 * * 1-5", "timezone": "Asia/Kolkata", "enabled": true,
  "trading_days_only": true, "misfire_grace_seconds": 3600,
  "max_requests_per_run": 40000, "is_builtin": true,
  "params": { "underlying_ids": [1,2,3,4] },
  "next_fire_at": "2026-09-09T18:30:00+05:30",
  "last_fired_at": "2026-09-08T18:30:00+05:30",
  "last_outcome": "enqueued", "last_job_id": "uuid" }
```

### POST /api/v1/schedules
Limit: 30/min.
Request: `name`, `kind`, `cron`, `timezone`, `params`, `enabled`, `trading_days_only`,
`misfire_grace_seconds`, `max_requests_per_run`.
Response `201`. Writes the row then calls `SchedulerService.sync()`.
Errors: `400 invalid_cron` (validated with APScheduler's own `CronTrigger.from_crontab`),
`400 unknown_kind`.

### PATCH /api/v1/schedules/{schedule_id}
Any editable field. Builtin schedules may be disabled and re-timed but not deleted or re-kinded.

### DELETE /api/v1/schedules/{schedule_id}
Response `204`. Errors: `409 builtin_schedule`.

### POST /api/v1/schedules/{schedule_id}/run-now
Limit: 20/min.
Response `202` `{ "job_id": "uuid", "outcome": "enqueued" }` or
`{ "job_id": null, "outcome": "skipped_needs_auth" }`.
Runs the identical body the cron trigger would run, including both guards.

### GET /api/v1/schedules/{schedule_id}/runs
Query: `limit` (default 50). Response: `schedule_run` rows newest first.

---

## 10. System

### GET /api/v1/system/budget
Limit: 240/min (the top bar polls this as an SSE fallback).

```json
{ "ist_date": "2026-09-09", "plan": "standard",
  "requests_used": 12400, "plan_limit_day": 100000,
  "remaining": 87600, "minute_headroom": 170,
  "minute_violations": 0, "strikes_remaining": 3,
  "blocked_until": null,
  "pipeline_mode": "running", "pipeline_reason": null,
  "sweep_reserve_fraction": 0.70 }
```

### GET /api/v1/system/storage

```json
{ "duckdb_bytes": 7412340224, "duckdb_wal_bytes": 4194304,
  "sqlite_bytes": 12582912, "exports_bytes": 1073741824,
  "raw_payload_bytes": 209715200,
  "candle_rows": 487213004, "bytes_per_row": 15.21,
  "modelled_bytes": 7410509290, "bloat_ratio": 1.0002,
  "compaction_suggested": false, "free_disk_bytes": 214748364800 }
```

### GET /api/v1/system/health
Response: the `v_data_health` rows plus the last `maintenance` run outcome.

### POST /api/v1/system/checkpoint
Limit: 10/min. Response `{ "wal_bytes_before": ..., "wal_bytes_after": 0 }`.

### POST /api/v1/system/optimise
Limit: 2/hour.
Request `{ "confirm": true }`.
Response `202` `{ "job_id": "uuid" }`.
Behaviour: quiesces the writer queue, then `ATTACH` a new file, `CREATE TABLE AS SELECT ... ORDER
BY contract_id, res_id, ts`, fsync, rename and reopen. `VACUUM` is never used because it was
measured to reclaim nothing.
Errors: `409 pipeline_busy`, `507 insufficient_disk` (needs free space equal to the current file
plus 20 percent).

### POST /api/v1/system/backup
Limit: 2/hour.
Request `{ "target_dir": str|null }`.
Response `202`. Checkpoints first and copies the `.duckdb`, its `.wal`, the `.sqlite3` and its
sidecars together. A copy without the WAL, or without checkpointing first, restores a database
missing the most recent writes.

### GET /api/v1/system/notifications
Query: `unread_only`. Response: `notification` rows newest first.

### POST /api/v1/system/notifications/{id}/read
### POST /api/v1/system/notifications/{id}/dismiss
Response `204`.

### GET /api/v1/system/settings
### PATCH /api/v1/system/settings
Typed settings from `sqlite.settings`. `PATCH` accepts a partial object and validates each key
against its schema. This is the replacement for a .env file.
Errors: `400 unknown_setting`, `400 setting_out_of_range`.

### GET /api/v1/system/requests
Query: `since`, `endpoint`, `outcome`, `limit`. Response: recent `task` rows projected as a
request log for the Diagnostics tab, with parameters scrubbed.

---

## 11. Events

### GET /api/v1/events/stream
Auth: `session`. Limit: 10 concurrent streams per session.
Response: `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`,
a 15 second keepalive comment, and `Last-Event-ID` replay from a 512 frame ring buffer.

Frame types, each with a monotonic `id`:

| event | data |
|---|---|
| `job_progress` | `{ job_id, status, total, done, empty, failed, skipped, requests_used, rows_written, eta_seconds }` |
| `job_started` / `job_finished` / `job_blocked` | `{ job_id, status, reason }` |
| `task_completed` | `{ job_id, task_id, kind, state, fyers_symbol, row_count, latency_ms }` |
| `budget` | the `/system/budget` body |
| `auth_required` | `{ token_state, reason, parked_jobs, parked_tasks }` |
| `rate_limited` | `{ strikes_used, strikes_remaining, blocked_until }` |
| `pipeline_mode` | `{ mode, reason }` |
| `schedule_fired` | `{ schedule_id, job_id, outcome }` |
| `export_ready` | `{ export_id, byte_size, row_count }` |
| `notification` | the notification row |

The stream is a refresh accelerator, never the source of truth. Every frame has a REST equivalent
and the SPA keeps a slow background refetch, so a dropped stream degrades the refresh rate and
never the correctness of what is displayed.

---

## 12. Static

### GET /{path:path}
Production only. `StaticFiles` over `frontend/dist` with an SPA fallback to `index.html` for any
path that does not start with `/api` and does not resolve to a real file. Carries the full
security header set including the CSP.
