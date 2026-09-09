# ExpiryManager Build Plan

Six phases. Within a phase, the work items are independent and can be built concurrently: no two
items in the same phase write to the same file. Across phases, an item may overwrite a placeholder
created by an earlier phase, which is a sequential dependency and not a conflict.

Two conventions make the concurrency real:

1. **Per-area schema and query modules.** `api/schemas/` and `db/queries/` are packages with one
   file per area, so a route work item never edits a shared `schemas.py`. Their `__init__.py`
   files are empty and are owned by the scaffold item.
2. **Placeholder-then-fill for aggregation points.** `api/v1/__init__.py`, `frontend/src/App.tsx`
   and every `frontend/src/routes/*.tsx` are created in the scaffold phase with the final import
   lists and stub bodies. Later items replace the stub bodies of the files they own.

Repository root is `/Users/openalgo/AIBootcamp2026/Day26/ExpiryManager`. Every path below is
relative to it.

---

## Dependency set at a glance

```
Phase 0  W01 W02 W03 W04 W05 W06                 (no cross dependencies inside the phase)
Phase 1  W07(W02,W04)  W08(W01,W04,W05)  W09(W03)  W10(W05,W08,W09)
Phase 2  W11(W02,W08)  W12(W02,W09,W05)  W13(W08,W09,W11,W12)  W14(W02,W12)
Phase 3  W15(W01,W02,W03,W07,W11,W14)
         W16(W15,W07,W08)  W17(W15,W09,W10)  W18(W15,W12,W11)
         W19(W15,W09)      W20(W15,W09,W14,W11)
Phase 4  W21(W06)  W22(W06,W21)
         W23(W22)  W24(W22)  W25(W22)  W26(W22)  W27(W22)
Phase 5  W28(all backend)  W29(W13,W09)  W30(W26)
Phase 6  W31(all)  W32(all)
```

---

## Phase 0: Foundations

Nothing here talks to Fyers, to the pipeline or to HTTP. These six items can all start at once.

### W01 Backend scaffold, paths and settings

Files owned:
```
backend/pyproject.toml
backend/README.md
backend/expirymanager/__init__.py
backend/expirymanager/version.py
backend/expirymanager/__main__.py
backend/expirymanager/paths.py
backend/expirymanager/settings_store.py
backend/expirymanager/logging_setup.py
backend/expirymanager/security/__init__.py
backend/.gitignore
backend/expirymanager/db/__init__.py
backend/expirymanager/db/queries/__init__.py
backend/expirymanager/brokers/__init__.py
backend/expirymanager/brokers/fyers/__init__.py
backend/expirymanager/pipeline/__init__.py
backend/expirymanager/pipeline/handlers/__init__.py
backend/expirymanager/scheduler/__init__.py
backend/expirymanager/api/__init__.py
backend/expirymanager/api/schemas/__init__.py
scripts/dev-backend.sh
```
Deliverables: pinned dependency set (fastapi 0.141.1, uvicorn 0.52.4, sqlalchemy 2.0.52,
duckdb 1.5.5, pyarrow 25.0.1, httpx 0.28.1, apscheduler 3.11.3, cryptography 50.0.1,
argon2-cffi 25.1.0, limits 5.8.0, secure 2.0.1, sse-starlette 3.4.11, keyring 25.7.0,
pytest 8, pytest-asyncio) and the `expirymanager` console script. `__main__.py` must have
`os.umask(0o077)` as its first executable statement, then `paths.ensure()` (which also ensures the
TLS material through `security/tls.py`), then
`uvicorn.run(..., host="127.0.0.1", port=8000, workers=1, ssl_keyfile=..., ssl_certfile=...)`.
The host, port and https scheme are fixed by the registered Fyers redirect URI and are not
configurable. Print the plain-text self-signed certificate warning once at startup. `paths.py` resolves `~/.expirymanager` and every child, creates it
0700, holds the advisory lock at `expirymanager.lock`, and refuses to start under a cloud sync
root. `settings_store.py` provides typed get and set over the `settings` table with a code default
for every key listed in DATA-MODEL.md section 1.2. `logging_setup.py` wires JSON logging and
installs the filters that W07 provides (import by name, tolerate absence during Phase 0 with a
no-op fallback that is removed in W07).

### W02 SQLite engine, models and migrations

Files owned:
```
backend/expirymanager/db/sqlite.py
backend/expirymanager/db/models.py
backend/expirymanager/db/migrate.py
backend/expirymanager/db/migrations/0001_init.sql
backend/expirymanager/db/migrations/0002_pipeline.sql
backend/expirymanager/db/migrations/0003_reference.sql
backend/expirymanager/db/migrations/0004_underlyings.sql
```
Deliverables: every SQLite table in DATA-MODEL.md section 1 with exact DDL, the per-connection
PRAGMA listener (WAL, synchronous NORMAL, foreign_keys ON, busy_timeout 5000, secure_delete ON,
trusted_schema OFF, cell_size_check ON), the numbered migration runner against `schema_version`,
and the reference and builtin-underlying seeds. Encrypted-column tables use TEXT uuid primary
keys. Every index listed in section 1.6 is created.

### W03 DuckDB store, schema and writer

Files owned:
```
backend/expirymanager/db/duck.py
backend/expirymanager/db/duck_schema.sql
backend/expirymanager/db/duck_macros.sql
backend/expirymanager/db/writer.py
backend/expirymanager/db/reader.py
backend/expirymanager/db/arrow.py
```
Deliverables: the single DuckDB instance with the pinned config from DATA-MODEL.md section 2, the
full DDL and macros applied idempotently at startup, the one writer task consuming an
`asyncio.Queue` of `WriteOp` with one transaction per op, reader helpers on fresh cursors through
`run_in_threadpool` under a semaphore of 6, and `CANDLE_SCHEMA` plus `candles_to_arrow` mapping
by the returned `columns` array with the fixed +19800 IST offset and DECIMAL(11,4) prices, where
a value that would lose precision raises rather than rounds. There is no currency-segment guard:
DECIMAL(11,4) represents the 0.0025 currency tick exactly. `duck.py` raises on any attempt to open read-only or to `ATTACH` the live file.

### W04 Field encryption and the key hierarchy

Files owned:
```
backend/expirymanager/security/crypto.py
backend/expirymanager/security/kek.py
backend/expirymanager/security/keys.py
backend/expirymanager/security/tls.py
```
Deliverables: `tls.py` generates and renews the self-signed 127.0.0.1 certificate
(SAN `IP:127.0.0.1, DNS:localhost`, about one year, both files 0600 with fsync on the file and the
parent directory) as described in SECURITY.md section 3a. Plus the `EM1` envelope, the location-bound AAD, the three KEK providers with the key
file as default (O_EXCL 0600, fsync on file and parent directory, refuse on loose permissions),
DEK generate, load, cache, wrap, rewrap-on-provider-switch and version rotation.

### W05 Symbology, roots and the trading calendar

Files owned:
```
backend/expirymanager/brokers/fyers/symbology.py
backend/expirymanager/brokers/fyers/roots.py
backend/expirymanager/brokers/fyers/calendar.py
backend/tests/fixtures/symbols.json
```
Deliverables: the enumerate-all-splits-then-score parser with optional `expected_root` and
`expected_expiry` hints, the `[1-9OND]` weekly month alphabet, `decimal.Decimal` strikes with the
raw substring retained, the runtime year window (2015 to current year plus 5), a raise on a
top-level scoring tie, and `parse_method` and `parse_confidence` on every result. The root
registry is anchored at the start of the body and tried longest first. `calendar.py` provides IST
trading days over `market_holiday`, exchange floors, the 30 trading day seconds window, the
partial candle offset per resolution and the 95 calendar day chunker with half-open IST day
boundaries. `symbols.json` holds the 38 doc-verbatim symbols plus the constructed edge cases
(NIFTYNXT50 weekly, BANKNIFTY containing NIFTY, MARUTI containing MAR, SENSEX50 monthly, M&M,
BAJAJ-AUTO cash, GBPINR decimal strike, the October letter-O case, and
`BSE:SENSEX2381161000CE`).

### W06 Frontend scaffold

Files owned:
```
frontend/package.json
frontend/vite.config.ts
frontend/tsconfig.json
frontend/tsconfig.app.json
frontend/tsconfig.node.json
frontend/components.json
frontend/index.html
frontend/src/index.css
frontend/src/main.tsx
frontend/src/App.tsx
frontend/src/vite-env.d.ts
frontend/src/lib/utils.ts
frontend/src/components/ui/**          (generated by the shadcn CLI)
frontend/src/routes/*.tsx              (placeholder stubs only)
scripts/dev-frontend.sh
```
Deliverables: `npm create vite@latest frontend -- --template react-ts --yes`, then
`npm i tailwindcss @tailwindcss/vite`, then
`npx shadcn@latest init -b radix -p nova --no-monorepo -y`, then the component set listed in
ARCHITECTURE.md section 8, then `npm i openalgo-charts@2.1.0 @tanstack/react-query@5.102.8
@tanstack/react-table@9.2.4 react-router-dom@7.18.3 react-hook-form@7.87.0 zod@4.5.4
next-themes@0.4.6`.

Three config traps that must be honoured: do **not** add `"baseUrl": "."` to any tsconfig (the
shadcn Vite page says to, and it is a hard TS5101 error on TypeScript 6 and 7; `paths` alone
works and the shadcn CLI still validates the alias); the Vite proxy uses `changeOrigin: false` so
the browser Origin header stays meaningful; and the dev server must run **https on host
`127.0.0.1` port 5173**, reading `~/.expirymanager/tls/server.{key,crt}`, proxying `/api` to
`https://127.0.0.1:8000` with `secure: false`. The host must be `127.0.0.1` and not `localhost`,
because cookies ignore the port but not the host, and that is what makes the session cookie set by
the OAuth callback on port 8000 visible to the dev origin on port 5173.
`package.json` must already contain the `test` script that W30 relies on. `App.tsx` declares the final
route table importing every file in `src/routes/`; each of those files ships as a stub that
renders its own name, to be replaced in Phase 4. `index.css` adds the global styled scrollbar rule
and the `--oac-*` override block (which needs `!important`, because the widget writes its tokens
as inline declarations on its root).

---

## Phase 1: Core services

### W07 HTTP security primitives
Depends on: W02, W04.
Files owned:
```
backend/expirymanager/security/passwords.py
backend/expirymanager/security/sessions.py
backend/expirymanager/security/csrf.py
backend/expirymanager/security/ratelimit.py
backend/expirymanager/security/headers.py
backend/expirymanager/security/redaction.py
```
Deliverables: everything in SECURITY.md sections 4, 5, 6.2, 8, 9 and 10. `headers.py` must emit
`style-src 'self' 'unsafe-inline'` with the verified reason in a comment, and gate HSTS on
production plus https. `redaction.py` exports the filter that `logging_setup.py` installs; remove
W01's no-op fallback as part of this item.

### W08 Fyers client, governor and tokens
Depends on: W01, W04, W05.
Files owned:
```
backend/expirymanager/brokers/fyers/client.py
backend/expirymanager/brokers/fyers/errors.py
backend/expirymanager/brokers/fyers/endpoints.py
backend/expirymanager/brokers/fyers/throttle.py
backend/expirymanager/brokers/fyers/tokens.py
backend/expirymanager/brokers/fyers/auth.py
```
Deliverables: one shared `httpx.AsyncClient` against `https://api-t1.fyers.in` with the header
`Authorization: app_id:access_token` (no Bearer prefix) and percent-encoded symbol parameters;
**two** envelope parsers, because historical-data has no `data` wrapper and no `code` or `message`
on success and its `s` can be `no_data`; `classify()` returning the six retry classes from
PIPELINE.md section 4; `FyersGovernor` with the 8/s and 170/min buckets, the semaphore of 6, the
durable daily and violation counters and the mode state machine; `TokenBroker` with the
generation-guarded single-flight refresh and local JWT `exp` decoding; and the OAuth URL builder,
`appIdHash` and code exchange.

### W09 DuckDB writes, ids, queries, exports and maintenance
Depends on: W03.
Files owned:
```
backend/expirymanager/db/writes.py
backend/expirymanager/db/ids.py
backend/expirymanager/db/queries/catalog.py
backend/expirymanager/db/queries/bars.py
backend/expirymanager/db/queries/coverage.py
backend/expirymanager/db/exports.py
backend/expirymanager/db/maintenance.py
```
Deliverables: `upsert_candle_chunk` implementing the exact delete-then-insert transaction with the
half-open right boundary and the payload hash short circuit; the contract id block allocator
(1..999 reserved for spot, padded 1024-blocks per expiry, strike-then-option-type ordering);
catalog upserts including the `dim_underlying` mirror; the read functions backing every endpoint
in API.md sections 3, 4, 5, 7 and the coverage endpoints; `COPY TO` Parquet and CSV with the
documented row group sizes, the atomic rename, the manifest and the schema sidecar; and CHECKPOINT,
the duplicate assertion, coverage reconciliation, the size heuristic and the ATTACH plus CTAS
compaction routine.

### W10 Symbol master snapshot and SCD-2
Depends on: W05, W08, W09.
Files owned:
```
backend/expirymanager/brokers/fyers/symbol_master.py
```
Deliverables: unauthenticated fetch of the seven `https://public.fyers.in/sym_details/*_sym_master.json`
files (bypassing the governor, because they consume no API budget), sha256 per file, the SCD-2
diff into `dim_instrument_master` closing the previous row's `valid_to`, the
`symbol_master_snapshot` rows, and the root registry rebuild feeding `roots.py`. Also resolves the
open question about hyphenated derivative roots by asserting the observed root character class
from `NSE_FO` and recording it in `meta`.

---

## Phase 2: Pipeline

### W11 Queue, dispatcher, workers, supervisor and events
Depends on: W02, W08.
Files owned:
```
backend/expirymanager/pipeline/queue.py
backend/expirymanager/pipeline/dispatcher.py
backend/expirymanager/pipeline/worker.py
backend/expirymanager/pipeline/supervisor.py
backend/expirymanager/pipeline/progress.py
backend/expirymanager/pipeline/events.py
```
Deliverables: the single-statement `UPDATE ... RETURNING` lease, `ack`, `nack_transient`,
`nack_fatal`, `nack_auth` (attempt not incremented) and `reclaim_expired_leases`; the prefetch
buffer of `2 * worker_count`; the worker loop with the auth gate and governor in that order; the
supervisor owning the pool, the `asyncio.Event` auth gate, the reclaim loop and the mode
transitions; the once-per-second `GROUP BY state` progress aggregate; and the `EventBus` with a
512 frame ring buffer and monotonic ids. `worker.py` dispatches on task kind through a registry
that W13 populates.

### W12 Planner and job service
Depends on: W02, W05, W09.
Files owned:
```
backend/expirymanager/pipeline/planner.py
backend/expirymanager/pipeline/jobs.py
backend/expirymanager/api/schemas/downloads.py
```
Deliverables: the three-stage planner from PIPELINE.md section 1 including the 95 day chunker,
the exchange floor clamp, the seconds availability window, the coverage subtraction, seal
skipping, backward probing expansion and the `PlanPreview` arithmetic; and `JobService` with
create, estimate, start, pause, resume, cancel and `retry_failed` (which creates a child job).
`api/schemas/downloads.py` holds the plan request, `PlanPreview` and the download commit models
so W18 does not have to define them.

### W13 Task handlers
Depends on: W08, W09, W11, W12.
Files owned:
```
backend/expirymanager/pipeline/handlers/expiry_discovery.py
backend/expirymanager/pipeline/handlers/contract_discovery.py
backend/expirymanager/pipeline/handlers/candle_chunk.py
backend/expirymanager/pipeline/handlers/spot_chunk.py
backend/expirymanager/pipeline/handlers/symbol_master.py
backend/expirymanager/pipeline/handlers/chain_snapshot.py
backend/expirymanager/pipeline/handlers/export.py
```
Deliverables: one handler per task kind, each registering itself with the worker registry. The
candle chunk handler is the hot path and must implement, in order: build the request with
`date_format=1` and the clamped range, call, parse the deviant envelope, map by `columns`, hash the
payload, short circuit on an unchanged hash, build the Arrow batch, enqueue the WriteOp, wait for
the commit, write the provenance columns onto the task row, ack, then ask the planner about
backward expansion.

### W14 Scheduler
Depends on: W02, W12.
Files owned:
```
backend/expirymanager/scheduler/service.py
backend/expirymanager/scheduler/jobs_def.py
backend/expirymanager/db/migrations/0005_schedules.sql
backend/expirymanager/api/schemas/schedules.py
```
Deliverables: `AsyncIOScheduler` with `MemoryJobStore`, `sync()` rebuilding every trigger from
`sqlite.schedule` on startup and after every mutation, `CronTrigger` with `Asia/Kolkata`,
`coalesce=True`, `max_instances=1` and per-row `misfire_grace_time`; the ten builtin schedules
from PIPELINE.md section 10.2 seeded by migration `0005`; and the two guards (needs_reauth and
budget or block) applied at the top of every fire, writing the `schedule_run` outcome.

---

## Phase 3: HTTP surface

### W15 Application assembly
Depends on: W01, W02, W03, W07, W11, W14.
Files owned:
```
backend/expirymanager/app.py
backend/expirymanager/lifespan.py
backend/expirymanager/bootstrap.py
backend/expirymanager/api/deps.py
backend/expirymanager/api/errors.py
backend/expirymanager/api/static.py
backend/expirymanager/api/schemas/common.py
backend/expirymanager/api/v1/__init__.py
backend/expirymanager/api/v1/bootstrap.py
```
Deliverables: the app factory with the middleware order TrustedHost, SecurityHeaders, Session,
CSRF, RateLimit; the lifespan doing open, migrate, start writer, start supervisor, start
scheduler, recover interrupted jobs, and CHECKPOINT on shutdown; idempotent first-run
provisioning; the dependency providers; the correlation-id error handlers; the production
StaticFiles mount with the SPA fallback; the shared error envelope and pagination models;
`api/v1/__init__.py` importing and including **every** router listed in W16 to W20 (write the
final list now, so those items only create their own module); and `app.py` additionally including
`api/oauth_callback.py` at the root prefix. The CSRF middleware exempt list contains exactly one
path, `/fyers/callback`.

### W16 Auth and broker routes
Depends on: W15, W07, W08.
Files owned:
```
backend/expirymanager/api/v1/auth.py
backend/expirymanager/api/v1/broker.py
backend/expirymanager/api/oauth_callback.py
backend/expirymanager/api/schemas/auth.py
backend/expirymanager/api/schemas/broker.py
```
Deliverables: API.md sections 1 and 2 exactly, including the 303 off the callback URL, the manual
callback fallback, and the rule that no secret and no mask is ever returned.
`api/oauth_callback.py` holds `GET /fyers/callback` and is mounted at the **root**, outside the
`/api` prefix, because the registered redirect URI is exactly
`https://127.0.0.1:8000/fyers/callback`. W15 already includes this router in `app.py`, so this
item only creates the module.

### W17 Catalog routes
Depends on: W15, W09, W10.
Files owned:
```
backend/expirymanager/api/v1/underlyings.py
backend/expirymanager/api/v1/expiries.py
backend/expirymanager/api/v1/contracts.py
backend/expirymanager/api/schemas/catalog.py
```
Deliverables: API.md sections 3, 4 and 5, including `/underlyings/resolve` (symbol master search
plus one governed expiry-dates probe) and `/contracts/{id}/bounds` returning UTC seconds.

### W18 Download and job routes
Depends on: W15, W11, W12.
Files owned:
```
backend/expirymanager/api/v1/downloads.py
backend/expirymanager/api/v1/jobs.py
backend/expirymanager/api/v1/coverage.py
backend/expirymanager/api/schemas/jobs.py
```
Deliverables: API.md section 6, including the `confirm_requests` equality check that rejects a
stale plan with 409, and the coverage grid and gaps endpoints.

### W19 Data and chart routes
Depends on: W15, W09.
Files owned:
```
backend/expirymanager/api/v1/bars.py
backend/expirymanager/api/v1/chain.py
backend/expirymanager/api/schemas/bars.py
```
Deliverables: API.md section 7. Bars are columnar `{columns, candles}` in Fyers column order with
`epoch(ts) - 19800`, and the chain endpoints call the DuckDB macros rather than reimplementing
ATM in Python.

### W20 Export, schedule, system and event routes
Depends on: W15, W09, W11, W14.
Files owned:
```
backend/expirymanager/api/v1/exports.py
backend/expirymanager/api/v1/schedules.py
backend/expirymanager/api/v1/system.py
backend/expirymanager/api/v1/events.py
backend/expirymanager/api/schemas/system.py
backend/expirymanager/api/schemas/exports.py
```
Deliverables: API.md sections 8, 9, 10 and 11. The export file route must re-derive and
`Path.resolve()`-assert the path inside the exports directory. The SSE stream sets
`Cache-Control: no-cache` and `X-Accel-Buffering: no` and supports `Last-Event-ID` replay.

---

## Phase 4: Frontend

### W21 Client, types and shared hooks
Depends on: W06.
Files owned:
```
frontend/src/lib/api/client.ts
frontend/src/lib/api/types.ts
frontend/src/lib/api/keys.ts
frontend/src/lib/events/useEventStream.ts
frontend/src/lib/tables/features.ts
frontend/src/lib/format.ts
```
Deliverables: the fetch wrapper with same-origin credentials, `X-CSRF-Token` read from the
`em_csrf` cookie on every unsafe method, a typed `ApiError` carrying the correlation id, a 401
route to `/login` and a 409 `needs_reauth` that raises the banner; hand-written types mirroring
every response in API.md; centralised query keys (each route builds its own `useQuery` from these,
so no shared queries file exists to conflict on); the single `EventSource` hook patching the
TanStack Query cache; and the shared TanStack Table v9 `tableFeatures()` bundle (v9 registers row
models inside features, not as table options, and the factories take zero arguments).

### W22 Shell and shared components
Depends on: W06, W21.
Files owned:
```
frontend/src/components/layout/AppShell.tsx
frontend/src/components/layout/BootstrapGate.tsx
frontend/src/components/common/BudgetGauge.tsx
frontend/src/components/common/TokenBanner.tsx
frontend/src/components/common/CoverageBar.tsx
frontend/src/components/common/PageHeader.tsx
frontend/src/components/common/DataTable.tsx
frontend/src/components/common/EmptyState.tsx
```
Deliverables: the sidebar and top bar carrying the two facts that can ruin a session (budget and
token state); the gate that reads `/api/v1/bootstrap` once and routes to `/setup`, `/login` or the
app; and a generic server-driven `DataTable` over `features.ts` used by four screens.

### W23 Auth, setup and settings screens
Depends on: W22.
Files owned:
```
frontend/src/routes/login.tsx
frontend/src/routes/setup.tsx
frontend/src/routes/settings.tsx
frontend/src/components/settings/BrokerPanel.tsx
frontend/src/components/settings/SecurityPanel.tsx
frontend/src/components/settings/DataPanel.tsx
frontend/src/components/settings/StoragePanel.tsx
frontend/src/components/settings/DiagnosticsPanel.tsx
```
Deliverables: the three step first-run wizard (passcode, credentials with the exact redirect URL
and a copy button, connect with the paste-the-redirected-URL fallback), and the five settings
tabs. Forms are built from `field` plus react-hook-form, because the shadcn `form` registry entry
ships no files.

### W24 Underlyings, expiries and the download flow
Depends on: W22.
Files owned:
```
frontend/src/routes/underlyings.tsx
frontend/src/routes/expiries.tsx
frontend/src/components/underlyings/AddUnderlyingDialog.tsx
frontend/src/components/download/ExpirySelectTable.tsx
frontend/src/components/download/SelectionBar.tsx
frontend/src/components/download/DownloadSheet.tsx
frontend/src/components/download/PlanPreview.tsx
```
Deliverables: the core screen and the plan-before-fetch flow. `PlanPreview` disables Start with an
inline reason when the plan exceeds the remaining budget and offers Queue for tomorrow instead.
The ATM strike-scope option is rendered disabled with a visible reason when underlying history is
missing for the expiry day, with a one-click remedy, rather than silently falling back to all
strikes. Multi-select is built from popover plus command plus checkbox, or from row selection on
the table; there is no shadcn multi-select component.

### W25 Jobs screens
Depends on: W22.
Files owned:
```
frontend/src/routes/jobs.tsx
frontend/src/routes/job-detail.tsx
frontend/src/components/jobs/TaskTable.tsx
frontend/src/components/jobs/JobProgressHeader.tsx
frontend/src/components/jobs/FailedTaskDrawer.tsx
```
Deliverables: the live job list and detail, the task tabs (All, Failed, Empty, Skipped) with
`empty` rendered distinctly from `failed`, per-task error code and message, and the retry, pause,
resume and cancel actions.

### W26 Contracts, chart and chain
Depends on: W22.
Files owned:
```
frontend/src/routes/contracts.tsx
frontend/src/routes/chart.tsx
frontend/src/routes/chain.tsx
frontend/src/lib/charts/expiryFeed.ts
frontend/src/lib/charts/intervals.ts
frontend/src/components/charts/ExpiryChart.tsx
frontend/src/components/charts/oiIndicator.ts
frontend/src/components/chain/ChainGrid.tsx
```
Deliverables: the DataFeed with `getBars` only (`subscribeBars` and `subscribeDepth` **omitted**,
not stubbed), the bounds cache and the window clamp, `withBarCache` with a long ttl because every
bar of an expired contract is closed forever, and `setHistoryLoader` calling
`chart.historyLoadComplete()` on **every** exit path including the empty and error paths.
`ExpiryChart.tsx` follows the React rules exactly: `createWidget` in a mount effect with an empty
dep array, the widget held in a `useRef` and never in `useState`, symbol, interval, theme and
chart type driven through the imperative setters from separate effects, `destroy()` in cleanup, a
container with a resolved non-zero pixel height, and a bare `import 'openalgo-charts/indicators'`
so the picker is not empty. Never deep-import into `dist/`.

### W27 Dashboard, exports and schedules
Depends on: W22.
Files owned:
```
frontend/src/routes/dashboard.tsx
frontend/src/routes/exports.tsx
frontend/src/routes/schedules.tsx
frontend/src/components/schedules/ScheduleDialog.tsx
frontend/src/components/schedules/RunHistoryDrawer.tsx
frontend/src/components/exports/ExportDialog.tsx
```
Deliverables: the coverage tiles, running jobs, next fires and interrupted-job resume prompt; the
export builder whose resolution pills are derived from what has actually been downloaded; and the
schedule table with a plain-English cron preview, enable switches, run-now and run history.

---

## Phase 5: Verification and packaging

### W28 Backend test suite
Depends on: every backend item.
Files owned:
```
backend/tests/conftest.py
backend/tests/fake_fyers.py
backend/tests/test_symbology.py
backend/tests/test_calendar.py
backend/tests/test_planner.py
backend/tests/test_queue.py
backend/tests/test_idempotency.py
backend/tests/test_throttle.py
backend/tests/test_tokens.py
backend/tests/test_crypto.py
backend/tests/test_security_http.py
backend/tests/test_api_smoke.py
backend/pytest.ini
```
Deliverables: `fake_fyers.py` is an `httpx.MockTransport` that reproduces both response envelopes,
`no_data`, every documented error code and a 429. The mandatory assertions are the ones listed in
ARCHITECTURE.md section 7 and SECURITY.md section 13, plus: the chunk seam produces no duplicated
or missing day; the same chunk twice yields identical row counts; a shrinking correction removes
the stale rows; 200 concurrent governor callers never exceed 8 per second or 170 per minute; N
concurrent auth errors trigger exactly one refresh and consume no retry attempt.

### W29 Export and maintenance verification
Depends on: W09, W13.
Files owned:
```
backend/tests/test_exports.py
backend/tests/test_maintenance.py
```
Deliverables: a Parquet round trip that reads back with identical values including DECIMAL; a CSV
carrying both the IST string and the UTC epoch; the Hive archive containing the catalog tables and
a manifest; and a compaction run that preserves every row and reduces the file.

### W30 Frontend checks
Depends on: W26.
Files owned:
```
frontend/src/lib/charts/expiryFeed.test.ts
frontend/src/lib/api/client.test.ts
frontend/vitest.config.ts
frontend/package.json is NOT owned here; add the test script via W06 up front
```
Deliverables: the clamp maths for a contract that expired months ago returns a non-empty window;
`subscribeBars` is genuinely absent from the feed instance; the client attaches `X-CSRF-Token` on
POST and not on GET.

Note: the `test` script must already exist in `frontend/package.json` from W06, so this item never
edits that file.

---

## Phase 6: Documentation and release

### W31 README, run scripts and first-run walkthrough
Depends on: everything.
Files owned:
```
README.md
scripts/run.sh
scripts/build.sh
.gitignore
```
The root `.gitignore` must exclude, before the first commit: `.cred`, `*.cred`, `data/`,
`*.duckdb`, `*.duckdb.wal`, `*.sqlite3`, `*.sqlite3-wal`, `*.sqlite3-shm`, `*.key`, `*.crt`,
`*.pem`, `master.key`, `tls/`, `node_modules/`, `dist/`, `__pycache__/`, `.venv/`.
The developer's real Fyers credentials live at `/Users/openalgo/AIBootcamp2026/Day26/.cred`,
outside this repository. They are used only to seed a running app through its own credential API
or UI, exactly as a real user would. **No application code may read that file at runtime**, and
its values must never appear in any file in this repository, any fixture, any log line, any commit
or any API response.

### W32 House-rule sweep
Depends on: everything.
Files owned: none exclusively. This item **edits** files across the tree and must therefore run
alone, after every other item has merged.
Deliverables: a repository-wide check and fix for the writing rules: no emoji or icons in code,
comments, log messages, commit messages, docs, tests or terminal output; no em dashes or en
dashes; comments explain why and not what. Add the check as a pre-commit hook so it does not have
to be repeated.

---

## Ordering advice for the build workflow

- Phase 0 is six independent items and should be dispatched as six parallel agents.
- Phase 1 is four independent items. W10 is the smallest and can be folded into W09's agent if
  parallelism is limited.
- Phase 2 must respect W11 and W12 before W13; W14 is independent of W13.
- Phase 3 is one assembly item followed by five independent route items.
- Phase 4 is two setup items followed by five independent screen items.
- W32 must be last and must run alone.
