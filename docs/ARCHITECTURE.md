# ExpiryManager Architecture

Version 1.0. This is the authoritative architecture. Where it disagrees with any earlier
proposal or research note, this document wins.

Writing rules for this repository, inherited from openalgo-charts CLAUDE.md and applied to the
whole of ExpiryManager: no emoji or icons anywhere (code, comments, logs, commits, docs, tests,
terminal output), no em dashes or en dashes, comments explain why and not what, Conventional
Commits.

---

## 1. What the system is

ExpiryManager is a single-process, single-user, zero-configuration desktop-class web application
that downloads, stores, charts and exports expired Indian F&O contract data plus the underlying
index and equity series from the Fyers broker API, and does so on a schedule.

It is built around three hard constraints that shape everything else:

1. **DuckDB permits exactly one process to hold the file.** A second OS process cannot open the
   database while a writer holds it, not even read-only. This forces one uvicorn worker, one
   DuckDB instance, an in-process scheduler and a single writer task.
2. **The Fyers rate limit is the binding resource, not disk or CPU.** Standard plan is 10
   requests per second, 200 per minute, 100,000 per day, and the documented penalty for
   exceeding the per-minute limit more than three times in one day is being blocked for the rest
   of the day. A full NIFTY 2022 to 2026 backfill costs roughly 62,000 requests. Every design
   choice that spends a request must be visible, budgeted and resumable.
3. **There is no .env file.** Credentials are entered through the UI and stored encrypted in
   SQLite, and the master key lives in a 0600 file outside the database.

A fourth constraint comes from the developer's already-registered Fyers app, and Fyers matches a
redirect URI exactly, so there is no freedom here (see `docs/LOCAL-TEST-CREDENTIALS.md`):

4. **The registered redirect URI is `https://127.0.0.1:8000/fyers/callback`.** Three consequences
   that are load bearing everywhere below: the backend must listen on 127.0.0.1 port 8000, it must
   serve **HTTPS** and not HTTP, and the callback route must be mounted at the **root** and not
   under the `/api` prefix. Because the project is zero config, the app generates its own
   self-signed TLS material on first run rather than asking anyone to produce a certificate.

---

## 2. Component diagram

```
                             one browser origin
  +-------------------------------------------------------------------------+
  |  Browser (React 19 + Vite 8 + Tailwind 4 + shadcn 4 + openalgo-charts)   |
  |                                                                          |
  |   routes  -> TanStack Query cache <- SSE frames (/api/v1/events/stream)   |
  |   charts  -> ExpiryManagerDataFeed (getBars only, window clamped)         |
  +--------------------------------|-----------------------------------------+
                                   |  fetch, cookies em_session + em_csrf (Secure)
                                   |  dev:  https://127.0.0.1:5173, Vite proxies
                                   |        /api -> https://127.0.0.1:8000 (secure:false)
                                   |  prod: https://127.0.0.1:8000 serves API and SPA
  +--------------------------------v-----------------------------------------+
  |  FastAPI process (uvicorn, workers=1, TLS on 127.0.0.1:8000)              |
  |  root route /fyers/callback sits OUTSIDE the /api prefix, because the      |
  |  registered Fyers redirect URI is exactly that path                       |
  |                                                                           |
  |  middleware: TrustedHost -> SecurityHeaders -> Session -> CSRF -> RateLimit|
  |                                                                           |
  |  +-------------------+   +--------------------+   +---------------------+ |
  |  |  API layer        |   |  PipelineSupervisor|   |  APScheduler        | |
  |  |  api/v1/*.py      |   |  dispatcher        |   |  AsyncIOScheduler   | |
  |  |  reads + commands |-->|  8 worker coros    |<--|  MemoryJobStore     | |
  |  +---------|---------+   |  auth gate Event   |   |  rebuilt from       | |
  |            |             +----|----------|----+   |  sqlite.schedules   | |
  |            |                  |          |        +---------------------+ |
  |            |                  |          |                                |
  |            |     +------------v--+   +---v-------------------+            |
  |            |     | FyersGovernor |   | Fyers HTTP client     |            |
  |            |     | 8/s 170/min   |-->| httpx.AsyncClient     |----------->| api-t1.fyers.in
  |            |     | daily budget  |   | Authorization:        |            |
  |            |     | strike count  |   |   app_id:access_token |            |
  |            |     +---------------+   +-----------------------+            |
  |            |                                    |                         |
  |            |                                    v                         |
  |            |                          +---------------------+             |
  |            |                          | Arrow RecordBatch   |             |
  |            |                          +----------|----------+             |
  |            |                                     |                        |
  |  +---------v-----------+            +------------v------------+           |
  |  | SQLite (SQLAlchemy) |            | DuckWriter (one task)   |           |
  |  | WAL, foreign_keys   |            | asyncio.Queue of WriteOp|           |
  |  | credentials         |            | one txn per op          |           |
  |  | settings            |            +------------|------------+           |
  |  | underlying registry |                         |                        |
  |  | jobs, tasks         |            +------------v------------+           |
  |  | schedules, budget   |            | DuckDB single instance  |           |
  |  | audit, notifications|            | candles, dim_*, coverage|           |
  |  +---------------------+            | readers via con.cursor()|           |
  |                                     +-------------------------+           |
  +---------------------------------------------------------------------------+

  Filesystem, all under ~/.expirymanager (0700):
    master.key (0600)   config.sqlite3 (+ -wal, -shm)   market.duckdb (+ .wal)
    tls/server.key (0600)  tls/server.crt (0600)
    exports/            raw/YYYY/MM/DD/*.json.gz        logs/            tmp/
```

Unauthenticated side channel: `https://public.fyers.in/sym_details/*_sym_master.json` is fetched
daily by the symbol master job. It consumes no API budget and keeps working when the broker token
is dead, which is why it is the only metadata source that can be trusted to never have a gap.

---

## 3. Process model

**One OS process. One uvicorn worker. Non negotiable.**

| Concern | Where it runs |
|---|---|
| HTTP API | FastAPI routes on the asyncio event loop |
| Scheduler | APScheduler `AsyncIOScheduler` started in the FastAPI lifespan |
| Download workers | 8 asyncio coroutines owned by `PipelineSupervisor` |
| Fyers HTTP | one shared `httpx.AsyncClient` behind `FyersGovernor` |
| DuckDB writes | exactly one `DuckWriter` task consuming an `asyncio.Queue` |
| DuckDB reads | `con.cursor()` per call, dispatched through `run_in_threadpool` |
| SQLite | SQLAlchemy 2.0 sync engine, all calls through `run_in_threadpool` |
| Argon2 verify | `run_in_threadpool`, because 64 MiB at t=3 blocks the loop for 50 to 100 ms |

Rules that follow, and which code review enforces:

- Never call `duckdb.connect(path, read_only=True)`. It fails in-process once a read-write
  connection exists.
- Never `ATTACH` the live file from a second instance. It fails with a unique file handle conflict.
- Never share a `cursor` between concurrent tasks. A second `execute()` discards the first result.
- Never use `executemany` for candle inserts. Measured 1200 times slower than the Arrow path.
- Never run `uvicorn --workers 2`, a separate scheduler process, or leave a `duckdb` CLI or DBeaver
  session open against `market.duckdb`.

Startup takes an advisory lock file at `~/.expirymanager/expirymanager.lock`. If the lock is held
the process exits with a message naming the likely causes rather than surfacing a DuckDB
`IOException` that reads like corruption.

---

## 4. End to end flow: user clicks Download

### 4.0 Before any of that: first run

```
0a uvicorn is about to bind 127.0.0.1:8000. paths.ensure() finds no tls/server.crt, so
   security/tls.py generates a self-signed certificate with cryptography: CN=127.0.0.1,
   subjectAltName = IP:127.0.0.1, DNS:localhost, about one year of validity, key and
   certificate both written 0600 inside ~/.expirymanager/tls. It regenerates whenever the
   file is missing or expired. The startup banner prints, in plain text, that the browser
   will show a certificate warning on first visit and that this is expected.
0b The browser lands on https://127.0.0.1:8000, GET /api/v1/bootstrap says provisioned:false,
   and the three step wizard runs: passcode, credentials, Connect.
```

```
1  Browser: user selects underlying NSE:NIFTY50-INDEX on /expiries, ticks 8 expiries,
   opens the download sheet, picks resolutions 1 and 5, options only, include OI.

2  POST /api/v1/downloads/plan          (costs zero Fyers requests)
   planner.plan(request) reads only local state:
     - dim_expiry rows for the 8 selected expiries
     - dim_contract rows where contracts_discovered_at is not null
     - candle_coverage for every (contract, res, chunk) already held
     - dim_contract.sealed_at, to skip contracts whose life is fully covered
     - ref_exchange floors: NSE 2022-01-03, BSE 2023-08-07, MCX 2022-01-03
     - resolution availability: 5S only when expiry_date is inside the last 30 trading days
   returns PlanPreview {
     tasks_total, requests_estimated, chunks_skipped_covered, rows_estimated,
     bytes_estimated (rows * 15.21), eta_seconds (requests / 170 per minute),
     budget_used_today, budget_remaining_today, budget_after, warnings[]
   }

3  Browser renders PlanPreview. Start is disabled with an inline reason if the plan would
   exceed the remaining daily budget; the alternative offered is Queue for tomorrow.

4  POST /api/v1/downloads
   jobs.create() writes, in ONE SQLite transaction:
     job row (status queued)  +  every task row (state pending, seq ordered)
   and pushes the job id onto the supervisor.

5  Dispatcher loop:
   queue.lease(worker_id, n) is a single
     UPDATE task SET state='leased', lease_owner=?, lease_expires_at=now+120s
      WHERE id IN (SELECT id FROM task
                    WHERE state='pending' AND not_before <= now
                    ORDER BY priority, job_id, seq LIMIT ?)
     RETURNING *
   Leased rows go into a small in-memory asyncio.Queue of capacity 2 * worker_count.
   The SQLite table is the durable truth; the memory queue is only a prefetch buffer.

6  Worker coroutine:
   await supervisor.auth_gate.wait()      # cleared the instant a token error is seen
   await governor.acquire()               # token bucket 8/s and 170/min, semaphore 6,
                                          # durable daily counter, violation counter
   response = await endpoints.expired_historical_data(
        symbol=..., resolution=..., date_format='1',
        range_from=..., range_to=..., include_oi='1')

7  Parse. historical-data uses its own envelope: top level s, symbol, resolution, columns,
   candles, schema_version. No data wrapper, no code, no message on success.
     s == 'ok'      -> map candles by the RETURNED columns array, never by index
     s == 'no_data' -> task state empty, coverage row with row_count 0, and the planner
                       cancels the remaining older chunks for this contract as skipped
     s == 'error'   -> errors.classify(...) decides the queue transition

8  Build pyarrow.RecordBatch with the pinned CANDLE_SCHEMA. Convert epoch seconds to naive IST
   by adding 19800 seconds. Prices to decimal128(11,4); a value that would lose precision raises
   rather than rounds.

9  Enqueue one WriteOp on the DuckWriter queue. The writer runs, in one transaction:
     BEGIN;
     DELETE FROM candles
      WHERE contract_id = ? AND res_id = ?
        AND ts >= range_from 00:00:00 AND ts < range_to + 1 day 00:00:00;
     INSERT INTO candles SELECT * FROM arrow_batch;
     INSERT OR REPLACE INTO candle_coverage VALUES (... api provenance ...);
     UPDATE contract_bounds ...;
     COMMIT;
   The delete predicate is literally the request that produced the data, so the operation
   converges after any crash and correctly removes rows when Fyers returns fewer candles.

10 Only after the DuckDB commit does the worker ack the task, writing http_status, fyers_code,
   latency_ms, response_bytes, row_count, first_ts, last_ts, columns_json and payload_sha256
   onto the same task row. Commit first, ack second: a crash between them replays a chunk that
   is idempotent anyway.

11 progress.py runs a GROUP BY state aggregate over the task table for each active job at most
   once per second, diffs it, and publishes a job_progress frame on the in-process EventBus.

12 events.py streams the frame over SSE. The browser's useEventStream writes it into the
   TanStack Query cache with setQueryData. No screen polls. REST remains authoritative: the
   job detail query still refetches slowly, so a dropped stream degrades refresh rate and
   never correctness.

13 When the last task of a job reaches a terminal state the supervisor sets the job status to
   completed or completed_with_errors, writes an ingest_runs row into DuckDB, and CHECKPOINTs.
```

---

## 5. The trust boundaries

```
  Boundary A: the browser
    Untrusted input crosses here. Every request is authenticated by an opaque session cookie,
    every unsafe method carries a CSRF synchronizer token, every route has a rate limit.
    Nothing secret is ever returned across it: the API returns secret_configured booleans,
    never a secret and never a mask.

  Boundary B: the Fyers API
    Untrusted output crosses here. Response bodies may contain tokens and are never placed in
    an HTTPException detail, never logged unredacted, and never echoed to the browser. Errors
    are classified into a fixed enum, and the browser sees a code plus a safe message.

  Boundary B2: the transport
    The server speaks HTTPS on 127.0.0.1:8000 with a self-signed certificate it generates
    itself. That is mandated by the registered Fyers redirect URI, and it also means the
    session and CSRF cookies carry Secure in development as well as in production. HSTS is
    still never sent, because pinning HSTS on 127.0.0.1 poisons that origin for every other
    local development server the user runs, and a self-signed certificate makes that worse.

  Boundary C: the filesystem
    ~/.expirymanager is 0700 and every file inside is 0600 (os.umask(0o077) is the first
    statement of the entry point, because SQLite creates the -wal and -shm sidecars itself and
    a later chmod does not touch them). master.key is refused if st_mode & 0o077 is non zero,
    the way sshd refuses a loose private key. Startup refuses to run if the data directory
    resolves under a known cloud sync root (iCloud, Dropbox, OneDrive, Google Drive), because
    WAL plus a sync client corrupts both databases and leaks the key.

  Boundary D: the process
    Anything running as the user can read master.key and the database. This is stated plainly
    in the UI rather than implied away. FileVault or equivalent full disk encryption covers the
    powered-off laptop case; nothing in-process covers a compromised user account.
```

Full detail is in SECURITY.md.

---

## 6. Storage split

**SQLite owns what a human authored and what must survive a DuckDB rebuild.**
Credentials, tokens, OAuth state, settings, the declared underlying registry, jobs, tasks,
schedules, the daily API budget, exports metadata, market holidays, notifications, audit log.

**DuckDB owns what the market gave us.**
`dim_underlying` (a writer-maintained mirror of the SQLite registry), `dim_expiry`,
`dim_contract`, `dim_resolution`, `dim_trading_day`, `dim_instrument_master` (SCD-2 symbol
master), `candles`, `candle_greeks`, `chain_snapshot`, `candle_coverage`, `contract_bounds`,
`ingest_runs`, `export_manifest`, `meta`.

The mirror exists so that no query ever joins across the two engines through Python. It is
written by the same writer task in the same transaction as the catalog write, so it cannot
diverge, and it can be rebuilt from SQLite at any time.

Full DDL is in DATA-MODEL.md.

---

## 7. Backend module layout

Package root `backend/expirymanager/`. One line of purpose per file.

### Entry, paths, config

| File | Purpose |
|---|---|
| `backend/pyproject.toml` | Pinned dependency set and the `expirymanager` console script. |
| `expirymanager/__main__.py` | Entry point. `os.umask(0o077)` first, then paths, then `uvicorn.run(workers=1)`. |
| `expirymanager/version.py` | Single source of the app version string, written into DuckDB `meta`. |
| `expirymanager/paths.py` | Resolves `~/.expirymanager` and every child path, creates 0700, refuses cloud sync roots, holds the advisory lock. |
| `expirymanager/security/tls.py` | Generates and renews the self-signed 127.0.0.1 certificate that uvicorn binds, 0600. |
| `expirymanager/app.py` | FastAPI application factory and middleware ordering. |
| `expirymanager/lifespan.py` | Opens SQLite and DuckDB, runs migrations, starts writer, supervisor and scheduler, CHECKPOINTs on shutdown. |
| `expirymanager/bootstrap.py` | Idempotent first-run provisioning: key file, DEK, migrations, reference seeds, four builtin underlyings. |
| `expirymanager/settings_store.py` | Typed accessors over `sqlite.settings`. The replacement for .env. Every key has a code default. |
| `expirymanager/logging_setup.py` | JSON logging plus the redaction filter and the OAuth callback query-string scrubber. |

### Security

| File | Purpose |
|---|---|
| `security/crypto.py` | AES-256-GCM envelope `EM1` plus key_ver plus nonce plus ct/tag, with location-bound AAD. |
| `security/kek.py` | `KekProvider` protocol and the keyfile, keyring and passphrase implementations. |
| `security/keys.py` | DEK load, cache, wrap, rewrap on provider switch, version rotation. |
| `security/passwords.py` | argon2-cffi hashing and verification, offloaded to the thread pool. |
| `security/sessions.py` | Opaque session issue, lookup by sha256, sliding idle and absolute expiry, rotation. |
| `security/csrf.py` | Sec-Fetch-Site check, Origin allowlist, synchronizer token comparison. |
| `security/ratelimit.py` | Per-route inbound limiters built on `limits` MemoryStorage. |
| `security/headers.py` | CSP and the rest of the response header set, HSTS gated on production https. |
| `security/redaction.py` | The JWT-shape regex and named-key scrubber shared by logs and error handlers. |

### Storage

| File | Purpose |
|---|---|
| `db/sqlite.py` | SQLAlchemy engine and the per-connection PRAGMA listener. |
| `db/models.py` | SQLAlchemy 2.0 declarative models for every SQLite table. |
| `db/migrate.py` | Numbered plain SQL migration runner against `schema_version`. |
| `db/migrations/0001_init.sql` | Core SQLite schema: crypto, users, sessions, credentials, settings. |
| `db/migrations/0002_pipeline.sql` | jobs, tasks, schedules, schedule_runs, api_budget, notifications, audit. |
| `db/migrations/0003_reference.sql` | ref_exchange, ref_segment, ref_instrument_type, ref_resolution seeds. |
| `db/migrations/0004_underlyings.sql` | The four builtin underlyings and the seeded market holidays. |
| `db/duck.py` | The single DuckDB instance, pinned config, reader cursor helper, writer queue handle. |
| `db/duck_schema.sql` | Full DuckDB DDL, sequences and views, applied idempotently at startup. |
| `db/duck_macros.sql` | The shipped query vocabulary: `bars`, `spot_at`, `atm_strike`, `chain_at`, `chain_window`. |
| `db/writer.py` | The one writer task, its `WriteOp` record type and its transaction discipline. |
| `db/reader.py` | Reader helpers on fresh cursors through `run_in_threadpool`, under a semaphore of 6. |
| `db/arrow.py` | `CANDLE_SCHEMA` and `candles_to_arrow`, mapping by the returned columns array. |
| `db/writes.py` | `upsert_candle_chunk`, `upsert_contracts`, `upsert_expiries`, `upsert_symbol_master`, mirror writes. |
| `db/ids.py` | Contract id block allocator: 1..999 reserved for spot, padded 1024-blocks per expiry. |
| `db/queries.py` | Every read the API serves, parameterised. |
| `db/exports.py` | `COPY TO` Parquet and CSV, denormalised, atomic rename, manifest and schema sidecar. |
| `db/maintenance.py` | CHECKPOINT, duplicate assertion, coverage reconciliation, size heuristic, ATTACH plus CTAS compaction. |

### Fyers broker

| File | Purpose |
|---|---|
| `brokers/fyers/client.py` | The only place httpx touches Fyers: base URL, auth header, percent encoding, two envelope parsers. |
| `brokers/fyers/auth.py` | authcode URL construction, appIdHash, code exchange, refresh attempt, local JWT exp decode. |
| `brokers/fyers/tokens.py` | `TokenBroker`: cached decrypted token, generation counter, single-flight refresh, park on failure. |
| `brokers/fyers/throttle.py` | `FyersGovernor`: token buckets, in-flight semaphore, durable budget, strike counter, mode machine. |
| `brokers/fyers/endpoints.py` | Typed wrappers for the three expired endpoints plus history, quotes, options-chain-v3. |
| `brokers/fyers/errors.py` | `classify(status, code, message)` into the fixed retry classes. |
| `brokers/fyers/symbology.py` | The enumerate-all-splits-then-score symbol parser. |
| `brokers/fyers/roots.py` | The root registry, seeded from the symbol master and the underlying registry, longest-first. |
| `brokers/fyers/symbol_master.py` | Unauthenticated daily pull of the seven public JSON masters and the SCD-2 diff. |
| `brokers/fyers/calendar.py` | IST trading days, holidays, exchange floors, partial candle offsets, the 95 day chunker. |

### Pipeline

| File | Purpose |
|---|---|
| `pipeline/jobs.py` | `JobService`: create, estimate, start, pause, resume, cancel, retry_failed. |
| `pipeline/planner.py` | Expands a selection into tasks and an estimate. Owns backward probing expansion. |
| `pipeline/queue.py` | The SQLite lease queue: lease, ack, nack_transient, nack_fatal, nack_auth, reclaim. |
| `pipeline/dispatcher.py` | Refills the in-memory prefetch buffer from the lease queue in priority order. |
| `pipeline/worker.py` | The worker coroutine: auth gate, governor, handler dispatch, error mapping. |
| `pipeline/supervisor.py` | Owns the pool, the auth gate Event, the reclaim loop and the pipeline mode. |
| `pipeline/progress.py` | Aggregates task state per active job at most once a second and publishes diffs. |
| `pipeline/events.py` | In-process `EventBus` with a 512-frame ring buffer and monotonic ids for SSE replay. |
| `pipeline/handlers/expiry_discovery.py` | Calls expiry-dates, upserts `dim_expiry`, enqueues contract discovery. |
| `pipeline/handlers/contract_discovery.py` | Calls underlying-symbols, allocates the id block, parses, writes `dim_contract`. |
| `pipeline/handlers/candle_chunk.py` | The hot path: expired historical-data to Arrow to the writer, plus backward expansion. |
| `pipeline/handlers/spot_chunk.py` | The same shape against `/data/history` for index and equity series. |
| `pipeline/handlers/symbol_master.py` | Runs the unauthenticated snapshot, bypassing the governor. |
| `pipeline/handlers/chain_snapshot.py` | Captures options-chain-v3 for the authoritative W/M flag and live greeks. |
| `pipeline/handlers/export.py` | Runs an export as a normal job so it reports progress like a download. |

### Scheduler and API

| File | Purpose |
|---|---|
| `scheduler/service.py` | AsyncIOScheduler with MemoryJobStore, rebuilt from `sqlite.schedules` on every mutation. |
| `scheduler/jobs_def.py` | The ten built-in schedule kinds. Each one only plans and enqueues, never fetches. |
| `api/deps.py` | `current_user`, `current_session`, `csrf_required`, store accessors, `require_broker_connected`. |
| `api/schemas.py` | Pydantic v2 request and response models. Secrets never appear in a response model. |
| `api/errors.py` | Exception handlers returning a correlation id and a safe message. |
| `api/static.py` | Production StaticFiles mount of `frontend/dist` with the SPA fallback. |
| `api/v1/bootstrap.py` | `GET /api/v1/bootstrap`, the only route reachable before setup. |
| `api/oauth_callback.py` | `GET /fyers/callback`, mounted at the ROOT because the registered redirect URI says so. |
| `api/v1/auth.py` | Local passcode setup, login, logout, me. |
| `api/v1/broker.py` | Fyers credentials, connect, callback, manual callback, disconnect, test. |
| `api/v1/underlyings.py` | The declared universe plus the resolve-a-new-root flow. |
| `api/v1/expiries.py` | Expiry listing with coverage rollups and expiry discovery. |
| `api/v1/contracts.py` | Server-driven contract browsing plus `/{id}/bounds` for the chart clamp. |
| `api/v1/downloads.py` | `POST /downloads/plan` (free) and `POST /downloads` (commits). |
| `api/v1/jobs.py` | Job list, detail, tasks, cancel, pause, resume, retry-failed. |
| `api/v1/bars.py` | Columnar candles for the chart plus the OI series for the Tier-2 indicator. |
| `api/v1/chain.py` | Option chain at a timestamp and ATM lookup, backed by the DuckDB macros. |
| `api/v1/coverage.py` | The coverage grid and gap report the heatmap renders. |
| `api/v1/exports.py` | Export creation, listing, file streaming and deletion. |
| `api/v1/schedules.py` | Schedule CRUD, enable, run-now, run history. |
| `api/v1/system.py` | Budget, storage, health, checkpoint, optimise, notifications. |
| `api/v1/events.py` | The one multiplexed SSE stream. |

### Tests

| File | Purpose |
|---|---|
| `tests/conftest.py` | Temp data dir, in-memory-ish fixtures, a fake Fyers transport. |
| `tests/fixtures/symbols.json` | The 38 doc-verbatim symbols plus the constructed edge cases. |
| `tests/test_symbology.py` | Golden-file parse of the corpus, including the SENSEX2381161000CE ambiguity. |
| `tests/test_calendar.py` | Chunker seams, exchange floors, 30 trading day 5S window, partial candle offset. |
| `tests/test_planner.py` | Coverage subtraction, sealing, backward expansion, estimate arithmetic. |
| `tests/test_queue.py` | Kill a worker mid-lease and assert exactly-once completion after reclaim. |
| `tests/test_idempotency.py` | Same chunk twice, then a shrinking correction, asserting convergence. |
| `tests/test_throttle.py` | 200 concurrent callers never exceed the buckets; the first 429 halts the pipeline. |
| `tests/test_tokens.py` | N concurrent token errors trigger exactly one refresh; attempts are not consumed. |
| `tests/test_crypto.py` | Envelope round trip, AAD relocation rejection, DEK rewrap on provider switch. |
| `tests/test_security_http.py` | CSRF rejection matrix, rate limit headers, header set, redaction. |
| `tests/test_api_smoke.py` | Bootstrap to login to plan to job creation against the fake transport. |

---

## 8. Frontend module layout

Root `frontend/`. React 19.2.8, Vite 8.2.2, Tailwind 4.3.3, shadcn 4.21.0 (`-b radix -p nova`,
style `radix-nova`, baseColor neutral), TanStack Query 5.102.8, TanStack Table 9.2.4,
react-router-dom 7.18.3, openalgo-charts 2.1.0 pinned from npm.

| File | Purpose |
|---|---|
| `vite.config.ts` | React and Tailwind plugins, `@` alias, HTTPS on `127.0.0.1:5173` using the app's own certificate, and the `/api` proxy to `https://127.0.0.1:8000` with `changeOrigin: false` and `secure: false`. |
| `tsconfig.json`, `tsconfig.app.json` | `paths` only, no `baseUrl` (baseUrl is a hard TS5101 error on TypeScript 6 and 7). |
| `components.json` | shadcn config. `baseColor` is locked after init, so it is chosen deliberately here. |
| `index.html` | Single page shell plus the theme-flash-avoidance inline script. |
| `src/index.css` | shadcn 4 import header, dark custom variant, oklch tokens, styled scrollbars, `--oac-*` overrides. |
| `src/main.tsx` | QueryClient (bar queries capped at 60 s gcTime), next-themes provider, router, Toaster. |
| `src/App.tsx` | Route table plus `BootstrapGate`. |
| `src/lib/api/client.ts` | fetch wrapper: same-origin credentials, `X-CSRF-Token` from the `em_csrf` cookie, typed `ApiError`. |
| `src/lib/api/types.ts` | Hand-written response types mirroring `api/schemas.py`. |
| `src/lib/api/queries.ts` | Query keys and query functions, one place so cache patching is coherent. |
| `src/lib/events/useEventStream.ts` | One `EventSource`, `Last-Event-ID` resume, frames into `setQueryData`. |
| `src/lib/tables/features.ts` | The shared TanStack Table v9 `tableFeatures()` bundle. |
| `src/lib/charts/expiryFeed.ts` | The DataFeed: `getBars` only, bounds cache, window clamp, `withBarCache`. |
| `src/lib/charts/intervals.ts` | Chart interval to Fyers resolution mapping and the downloaded-only pill filter. |
| `src/components/layout/AppShell.tsx` | Sidebar, breadcrumb, top bar carrying budget and token status. |
| `src/components/layout/BootstrapGate.tsx` | Reads `/api/v1/bootstrap` once and routes to setup, login or the app. |
| `src/components/common/BudgetGauge.tsx` | Requests used today, per-minute headroom, strikes used out of three. |
| `src/components/common/TokenBanner.tsx` | Persistent needs_reauth banner with one-click re-login. |
| `src/components/common/CoverageBar.tsx` | Segmented downloaded / empty / missing bar, reused on three screens. |
| `src/components/download/SelectionBar.tsx` | Sticky bottom bar summarising the current expiry selection. |
| `src/components/download/DownloadSheet.tsx` | Resolutions, instrument class, option right, strike scope, include OI, force refresh. |
| `src/components/download/PlanPreview.tsx` | The estimate card that gates Start. |
| `src/components/download/ExpirySelectTable.tsx` | The v9 table with row selection over expiries. |
| `src/components/charts/ExpiryChart.tsx` | `createWidget` in a mount effect, ref-held, imperative setters, `destroy()` in cleanup. |
| `src/components/charts/oiIndicator.ts` | Open interest through `createTier2Indicator`. |
| `src/routes/login.tsx` | Local passcode login with lockout and Retry-After display. |
| `src/routes/setup.tsx` | The three step first run wizard. |
| `src/routes/dashboard.tsx` | Coverage tiles, running jobs, next fires, interrupted-job resume, storage. |
| `src/routes/underlyings.tsx` | The declared universe and the add-your-own resolve flow. |
| `src/routes/expiries.tsx` | The core screen: underlying picker, expiry table, selection, download sheet. |
| `src/routes/jobs.tsx` | Job list driven by SSE. |
| `src/routes/job-detail.tsx` | Live progress, task tabs, failed task drill-through, retry and cancel. |
| `src/routes/contracts.tsx` | Server-driven contract browser with filters and cursor paging. |
| `src/routes/chart.tsx` | Chart workbench: contract picker, downloaded-only interval pills, OI toggle. |
| `src/routes/chain.tsx` | Option chain at a timestamp with a session scrubber and the ATM row highlighted. |
| `src/routes/exports.tsx` | Export builder and export history with download links. |
| `src/routes/schedules.tsx` | Schedule table, enable switches, cron preview, run-now, run history. |
| `src/routes/settings.tsx` | Broker, Security, Data, Storage and Diagnostics tabs. |
| `src/components/ui/*` | shadcn generated components. Not hand edited except where a generated file is patched. |

shadcn components installed: button card table badge input label checkbox select dialog
alert-dialog dropdown-menu tabs progress sonner tooltip popover command field separator
scroll-area skeleton sheet alert calendar combobox spinner empty sidebar pagination switch
textarea toggle-group radio-group breadcrumb input-group item kbd. Forms are built from `field`
plus react-hook-form 7.87 and zod 4.5, because the shadcn `form` registry entry ships no files.

---

## 9. Charting integration

The DataFeed implements `getBars` only. `subscribeBars` and `subscribeDepth` are deliberately
absent, because callers feature-detect them and a no-op stub makes a history-only feed look live.

The single most important chart fact: `createWidget` computes its request window as
`lookbackBars` back from `Date.now()`. A contract that expired in March 2025 has zero bars in
that window, so an adapter that honours `from` and `to` literally renders every expired contract
as "No bars". The adapter therefore fetches `/api/v1/contracts/{contract_id}/bounds` once per
symbol, caches it, and slides the requested span back so it ends at `last_ts`:

```
to   = min(req.to, last_ts)
from = max(req.from - (req.to - to), first_ts)
```

`reload()` always calls `series.setData` then `chart.fitContent()`, so a clamped window renders
correctly. `Bar.time` is integer UTC seconds, which is exactly what the bars endpoint emits
(`epoch(ts) - 19800`), so the value passes from Fyers through DuckDB to the chart unconverted.

Open interest has no field on `Bar` and goes through `createTier2Indicator` from
`openalgo-charts/indicators`. A bare `import 'openalgo-charts/indicators'` is required or the
indicator picker is empty. Never deep-import into `dist/`: a second registry instance is a
correctness bug that presents as "indicator not registered".

The interval pill list is derived from `contract_bounds.resolutions`, honouring the house rule
that a control is never shipped with nothing behind it. A resolution that exists but has no data
for the selected contract is rendered disabled with its state visible, not hidden.

---

## 10. Security model summary

Two-level key hierarchy. A random 32-byte DEK encrypts individual secret fields with AES-256-GCM
under a location-bound AAD. The DEK is wrapped by a KEK from a pluggable provider. The default
provider is a generated key file at `~/.expirymanager/master.key`, 0600 inside a 0700 directory,
because it is the only candidate that is both zero-config and unattended after a reboot. Keyring
and passphrase providers are opt-in with an explicit threat model shown in the UI. Switching
provider rewraps 32 bytes and re-encrypts no field.

Local login is argon2-cffi with library defaults. Sessions are opaque `token_urlsafe(32)` values
of which only the sha256 is stored, with an 8 hour sliding idle window and a 7 day absolute one.
CSRF is a synchronizer token on the session row, delivered as the readable `em_csrf` cookie and
echoed in `X-CSRF-Token`, layered over `Sec-Fetch-Site` and an Origin allowlist. Cookies are
`SameSite=Lax` specifically so the Fyers OAuth redirect still carries the session.

There is exactly one browser origin in development (`https://127.0.0.1:5173`, with the Vite proxy
carrying `/api`) and one in production (`https://127.0.0.1:8000`), so `CORSMiddleware` is never
added to this codebase at all. The development origin deliberately uses the host `127.0.0.1` and
not `localhost`, because cookies ignore the port but not the host: same-host different-port means
the session cookie set by the backend on the OAuth callback is visible to the Vite origin.

CSP is `script-src 'self'` and `style-src 'self' 'unsafe-inline'`. The `unsafe-inline` on styles
is required and verified: `openalgo-charts/src/widget/styles.ts` injects a scoped `<style>`
element with `textContent`, and `src/widget/tokens.ts` writes `--oac-*` tokens with
`el.style.setProperty`. There is no `eval` or `new Function` anywhere in the library, so
`script-src` stays strict.

Full detail, including the never-log and never-return lists, is in SECURITY.md.

---

## 11. Deployment shape

Development:

```
terminal 1:  cd backend  && uv run expirymanager --reload   # https://127.0.0.1:8000
terminal 2:  cd frontend && npm run dev                     # https://127.0.0.1:5173
```

Production on the user's own machine:

```
cd frontend && npm run build          # emits frontend/dist
cd backend  && uv run expirymanager   # serves the API and dist from https://127.0.0.1:8000
```

Both modes show a browser certificate warning on first visit, because the certificate is
self-signed. The startup banner says so in plain text so it is not mistaken for a fault.

The app binds loopback only. Running it on a shared machine or a VPS reopens TLS termination,
HSTS, real multi-user auth and the SQLCipher decision, and is out of scope for version 1.0.
