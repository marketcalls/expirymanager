# ExpiryManager Download Pipeline

The pipeline is built inside out from one idea: **every outbound Fyers request is one durable row
in the SQLite `task` table, and that row is simultaneously the work queue entry, the retry record
and the request provenance record.** Nothing about a download lives only in memory, so a crash, a
closed laptop lid, a token expiry or a rate-limit block costs at most the handful of requests
currently in flight.

The binding constraint is not disk or CPU. DuckDB ingests at a measured 1,036,582 rows per second
sustained, roughly 100 times faster than the Fyers feed can supply. The constraint is 200 requests
per minute and 100,000 per day, with the account blocked for the rest of the day after a fourth
per-minute overshoot.

---

## 1. Job and task decomposition

A download is decomposed into four levels. Each level's completion enqueues the next, so a 62,000
request NIFTY backfill is a resumable graph rather than a long running coroutine.

```
  Level 0   job                     one user action or one schedule fire
  Level 1   task kind expiry_dates       one per underlying per 366 day window
  Level 2   task kind underlying_symbols one per selected expiry
  Level 3   task kind candle_chunk       one per (contract, resolution, 95 day window)
            task kind spot_chunk         one per (underlying, resolution, 95 day window)
```

Auxiliary task kinds that do not fit the ladder: `symbol_master` (unauthenticated, bypasses the
governor) and `chain_snapshot` (live option chain, one per underlying per fire).

### 1.1 Planning, which costs zero requests

`POST /api/v1/downloads/plan` runs `pipeline/planner.py` in dry run mode. It reads only local
state and never touches Fyers. For each selected expiry:

1. If `dim_expiry.contracts_discovered_at` is NULL, emit one `underlying_symbols` task and
   estimate the downstream candle tasks from the sibling expiries' contract counts.
2. Otherwise take the actual `dim_contract` rows, filtered by the sheet's instrument class,
   option right and strike scope.
3. For each contract and each selected resolution compute the fetch window:
   - `range_to = expiry_date`
   - `range_from = max(underlying.data_from, expiry_date - option_life_days or future_life_days)`
   - clamp `range_from` to the exchange floor from `ref_exchange`
   - for a second resolution, emit nothing unless `expiry_date` is inside the last 30 trading days
     computed against `market_holiday`, because that data does not exist and requesting it burns
     budget for nothing
   - set `range_to` at least one full resolution period before now for any window that reaches
     the present, so a partial forming bar is never persisted
4. Chunk that window into at most 95 calendar day pieces. 95 and not 100, deliberately: the docs
   do not say whether the documented 100 day limit is calendar days or trading days, and a five
   day margin costs about five percent more requests and removes the ambiguity entirely.
5. Subtract every chunk already present in `candle_coverage` with a matching `include_oi` flag and
   status `ok` or `empty`, unless the sheet set `force_refresh`.
6. Skip entirely any contract whose `dim_contract.sealed_at` is set.

The result is a task list plus a `PlanPreview`:

```json
{
  "tasks_total": 4820,
  "requests_estimated": 4820,
  "chunks_skipped_covered": 1130,
  "contracts_sealed_skipped": 212,
  "rows_estimated": 14460000,
  "bytes_estimated": 219935660,
  "eta_seconds": 1701,
  "budget_used_today": 12400,
  "budget_remaining_today": 87600,
  "budget_after": 82780,
  "warnings": ["5S not available for 6 of the 8 selected expiries"]
}
```

`bytes_estimated` uses the measured 15.21 bytes per row. `eta_seconds` uses the governor's
effective 170 requests per minute, not the published 200.

The plan is the product's most important screen. A user who cannot see the cost before committing
will start a job that eats the whole day, and the same planner produces both the estimate and the
committed tasks, so the number cannot drift from reality.

### 1.2 Committing

`POST /api/v1/downloads` writes the job row and every task row in **one** SQLite transaction, then
pushes the job id to the supervisor. If the estimate exceeds the remaining daily budget the UI
offers Queue for tomorrow, which commits the job with status `deferred_budget` instead of `queued`.

### 1.3 Backward probing expansion

A naive planner emits sixteen 95 day chunks per contract to cover 2022 to today. A NIFTY weekly
option trades for days, not years, so that is fifteen wasted requests per contract per resolution,
which is the difference between a feasible backfill and an infeasible one.

Instead the planner emits **one** chunk ending on the expiry date and only decides about the next
one after that chunk returns:

- If the first returned candle sits more than one trading day after the chunk's left edge, the
  contract's whole life is inside the chunk. No further chunk is planned, and the contract is
  sealed.
- If the chunk is full to its left edge, one more chunk is planned further back, clamped to the
  exchange floor and to the configured life window.
- If the response is `s: "no_data"`, every remaining older chunk for that contract is marked
  `skipped` in one statement, and the contract is sealed.

For a NIFTY weekly this turns sixteen requests per contract per resolution into one.

### 1.4 Sealing

`dim_contract.sealed_at` is set once a contract's coverage rows span its whole tradeable life with
status `ok` or `empty`. An expired contract's history is immutable, so re-fetching it is pure
waste. Sealing turns the nightly sweep into a strictly forward-moving frontier, which is what
makes an unattended multi-day backfill converge instead of thrashing. `force_refresh` on the
download sheet clears the seal for the selected scope.

---

## 2. The outbound rate limiter

`brokers/fyers/throttle.py`, class `FyersGovernor`. There is exactly one instance in the process,
constructed in the lifespan and injected everywhere. Any code path that constructs a second
governor, or that calls `httpx` without going through it, is a day-losing bug rather than a
performance bug.

### 2.1 Published limits versus targets

| Limit | Standard | Prime | Our target |
|---|---|---|---|
| per second | 10 | 10 | 8 |
| per minute | 200 | 600 | 170 (510 on Prime) |
| per day | 100,000 | 200,000 | plan limit, with a 70 percent sweep reserve |

The margin is not timidity. The documented rule is that **a user is blocked for the rest of the
day if the per-minute rate limit is exceeded more than three times in that day.** A 15 percent
margin against clock skew, in-flight retries and coarse timer resolution is trivially cheap
compared to losing a trading day of quota mid backfill.

### 2.2 Mechanism

```
acquire():
    await pipeline_mode_is_running          # blocks indefinitely while paused or stopped
    await second_bucket.take()              # async token bucket, capacity 8, refill 8/s
    await minute_bucket.take()              # async token bucket, capacity 170, refill 170/60 s
    await in_flight.acquire()               # asyncio.Semaphore(6)
    daily.increment()                       # in memory counter
    if daily.count % 25 == 0: flush()       # persist to sqlite.api_budget
```

The daily counter is keyed by the IST date and is persisted every 25 requests and on every state
transition, so a restart or a crash loop cannot reset the 100,000 per day ceiling. The violation
counter (`api_budget.minute_violations`) is persisted immediately on every increment.

### 2.3 Mode state machine

```
                 +-------------------------------------------------+
                 |                                                 |
   running  --429/-429-->  paused_rate  --user resume-->  running   |
      |                                                            |
      +--auth error-->  paused_auth  --re-login-->  running --------+
      |
      +--user pause-->  paused_user  --user resume-->  running
      |
      +--budget hit-->  stopped_budget  --00:01 IST reset-->  running
      |
      +--fatal-->  stopped_fatal   (manual intervention only)
```

`paused_rate` requires an **explicit user resume**. Automatic resume after a rate violation is
exactly how a crash loop burns all three strikes in minutes. The UI states how many of the three
daily strikes remain, which makes the cost visible and forces a human decision before the second
strike.

`stopped_budget` lifts automatically at the 00:01 IST budget reset, because a new quota day is not
a fault. It does not lift a violation-based block, because the three strikes rule is per day and a
violation should be reviewed.

### 2.4 The sweep reserve

The nightly backfill sweep never spends more than `budget_reserve_fraction` of the daily cap
(default 0.70), leaving headroom for interactive downloads the user starts by hand. When it hits
the reserve it stops cleanly, leaving the remaining tasks `pending` for tomorrow.

---

## 3. Worker concurrency and the lease queue

### 3.1 Leasing

```sql
UPDATE task
   SET state = 'leased',
       lease_owner = :worker_id,
       lease_expires_at = :now_plus_120s,
       started_at = :now
 WHERE task_id IN (
     SELECT task_id FROM task
      WHERE state = 'pending' AND not_before <= :now
      ORDER BY priority, job_id, seq
      LIMIT :n)
RETURNING *;
```

One statement, one index (`idx_task_dispatch`), no read-then-write race. Ordering strictly by
`(priority, job_id, seq)` is what lets a high-priority nightly job overtake a running multi-day
backfill.

Leased rows go into a small in-memory `asyncio.Queue` of capacity `2 * worker_count`. SQLite is
the durable truth; the memory queue is only a prefetch buffer, sized so a hard kill loses at most
sixteen leases.

`reclaim_expired_leases()` runs at startup and every 60 seconds:

```sql
UPDATE task
   SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL
 WHERE state = 'leased' AND lease_expires_at < :now;
```

It does **not** increment `attempt`. A lost lease is our fault, not the task's.

### 3.2 The worker loop

Eight coroutines (`worker_count`, settable). Each one:

```
task = await prefetch_queue.get()
await supervisor.auth_gate.wait()         # an asyncio.Event, set while the token is usable
await governor.acquire()
try:
    result = await handler[task.kind](task)
except ...:
    cls = errors.classify(...)
    map cls onto the right nack
else:
    write outcome columns, ack
```

A worker never retries in place and never sleeps holding a lease. Both would consume a lease slot
for a duration nobody can bound.

Why eight workers behind a semaphore of six: the semaphore bounds concurrent sockets, the worker
count bounds how many tasks are in flight through the whole pipeline including the DuckDB write,
and having slightly more workers than sockets keeps the socket pool saturated while a write
commits.

---

## 4. Error classification, retry and backoff

`brokers/fyers/errors.py` classifies once. Every other module consumes the class, never the raw
code.

| Class | Triggers | Queue transition | Retries |
|---|---|---|---|
| `EMPTY` | `s == "no_data"` | state `empty`, coverage row with `row_count` 0, remaining older chunks for the contract marked `skipped`, contract sealed | none, this is a success |
| `FATAL` | -50 invalid parameters, -300 invalid symbol, -352 invalid App ID, HTTP 400, HTTP 403 | state `failed`, code and message retained | none |
| `TRANSIENT` | HTTP 500, timeouts, connection resets, `httpx.TransportError` | state `pending`, `attempt + 1`, `not_before = now + backoff` | up to `max_attempts` = 4 |
| `AUTH_RECOVERABLE` | -8 token expired, -16 server unable to authenticate token, -17 token invalid or expired | state `pending`, **attempt NOT incremented**, pipeline to `paused_auth` | after re-auth |
| `AUTH_FATAL` | -15 invalid token, HTTP 401 | state `pending`, attempt not incremented, pipeline to `paused_auth`, token state `needs_reauth` | after re-login |
| `RATE_LIMITED` | -429, HTTP 429 | state `pending`, attempt not incremented, pipeline to `paused_rate`, violation counter incremented | after explicit user resume |

Backoff for `TRANSIENT`: `min(60, 2 ** attempt) * (0.5 + random())` seconds, so attempts land at
roughly 1 to 3, 2 to 6, 4 to 12 and 8 to 24 seconds. Every retry counts against the rate buckets
exactly like a fresh request, because it is one.

Two rules that are easy to get wrong and are therefore written down:

- An auth error and a rate-limit error must never consume a retry attempt. Neither is the task's
  fault, and a token expiry at 03:00 must not silently exhaust four contracts' worth of budget.
- `-300 invalid symbol` additionally triggers a symbol encoding assertion, because the documented
  cause is a special character that was not percent encoded (`M&M` must go out as `M%26M`).

`-352` is overloaded in the docs (invalid App ID, and separately "no position available to exit").
Only the invalid App ID meaning applies here.

---

## 5. Idempotent writes

Every candle response becomes one `WriteOp` on the `DuckWriter` queue, and the writer runs it as
one transaction:

```sql
BEGIN;
DELETE FROM candles
 WHERE contract_id = :contract_id
   AND res_id      = :res_id
   AND ts >= :range_from_00_00_00_ist
   AND ts <  :range_to_plus_one_day_00_00_00_ist;
INSERT INTO candles SELECT * FROM arrow_batch;
INSERT OR REPLACE INTO candle_coverage VALUES (...);
INSERT OR REPLACE INTO contract_bounds VALUES (...);
COMMIT;
```

Why delete-then-insert and not `MERGE INTO` or `INSERT ON CONFLICT`:

- Measured 3 ms against 11 ms for `MERGE INTO` on a 5,000,000 row table, and the cost is bounded
  by the range rather than by total table size, because `MERGE` is a join against the target.
- It is the only variant that is semantically correct. If Fyers later returns 2,900 candles where
  it previously returned 3,000, the stale 100 are removed. `MERGE` would silently leave them.
- `INSERT ON CONFLICT` is unusable: it needs a UNIQUE or PRIMARY KEY constraint, and that
  constraint was measured to cost 4.5x file size and 4.5x load time for zero lookup benefit.

**The boundary rule.** Fyers `range_to` is inclusive of that date, so the delete window is half
open on the right at `range_to + 1 day` at 00:00:00 IST. Getting this off by one leaves a
duplicated or missing day at the seam between two chunks, which is the classic bug in chunked
backfills. `tests/test_idempotency.py` asserts the seam explicitly.

**Payload hash short circuit.** If `payload_sha256` matches the existing coverage row, the write
is skipped entirely. That is the common case when a settled expiry is re-fetched, and it avoids
creating row versions that fragment a file which never shrinks.

**Ordering.** DuckDB commit first, task ack second. A crash between them replays a chunk that is
idempotent anyway. The reverse order would lose data on a crash.

**Arrow only.** `db/arrow.py` builds a `pyarrow.RecordBatch` against a pinned schema, mapping
values by the returned `columns` array and never by fixed index (the seventh `open_interest`
element exists only with `include_oi=1`, and `include_greeks` will append more later). Measured
throughput is 3.6 to 11.2 million rows per second through Arrow against 8,350 through
`executemany`. `executemany` is the obvious DB-API method and is banned in code review.

---

## 6. Checkpointing and resume

There is no separate checkpoint mechanism. The task table is the checkpoint, and its granularity
is one HTTP request.

On startup, `lifespan.py` performs recovery in this order:

1. `reclaim_expired_leases()` returns every `leased` row to `pending` without touching `attempt`.
2. Any job left in `running` moves to `paused` with `block_reason = 'interrupted'`.
3. Any job in `deferred_budget` whose IST date has rolled is moved to `queued`.
4. `pipeline_state.mode` is read; if it is `paused_rate` or `stopped_fatal` it is preserved, so a
   restart cannot silently resume into a block.
5. The dashboard shows a Resume prompt for interrupted jobs.

Because progress numbers are always a fresh `GROUP BY state` aggregate over durable rows rather
than an in-memory counter, the displayed progress is correct immediately after any restart.

Cancellation is cooperative and instant: `cancel` sets `job.cancel_requested`, the dispatcher
stops leasing for that job, in-flight tasks finish and write their data, and remaining `pending`
tasks are bulk updated to `cancelled` in one statement. A cancelled job therefore leaves valid
partial data plus an accurate ledger of exactly what it did and did not fetch.

Retry of failures never rewrites history. `POST /api/v1/jobs/{id}/retry-failed` creates a **child
job** containing copies of exactly the failed tasks, with `parent_job_id` set, so the original
job's record stays intact and the user can see that the retry succeeded where the parent did not.

---

## 7. Token expiry mid job

This is the case the design is most careful about, because eight workers hitting an expired token
simultaneously must produce one refresh, not eight, and must not burn eight retry attempts.

### 7.1 Proactive parking

`brokers/fyers/tokens.py` decodes the access token's JWT `exp` claim locally rather than assuming
an undocumented lifetime. The `token_health` schedule runs every 30 minutes and:

- at 24 hours remaining, raises a `warning` notification,
- at 120 seconds remaining, parks the pipeline proactively so no in-flight request is wasted.

The common case therefore produces no error at all.

### 7.2 Reactive handling

When it happens anyway:

```
worker sees -8 / -15 / -16 / -17 / HTTP 401
  -> token_broker.on_auth_error(generation_the_worker_used)
       -> supervisor.auth_gate.clear()      # every other worker stops at the top of its loop
                                            # BEFORE it can spend another request
       -> queue.nack_auth(task)             # back to pending, attempt NOT incremented
       -> async with refresh_lock:
              if caller_generation < current_generation: return   # somebody already refreshed
              try refresh
```

The generation guard is what collapses N concurrent auth errors into exactly one refresh. The
other seven workers call `on_auth_error` with the same now-stale generation and return
immediately.

**If refresh succeeds:** the new access token is encrypted and written to `broker_token` with
`generation + 1`, `access_expires_at` re-decoded from the new JWT, and `auth_gate.set()`. The
workers resume and re-lease the very same task rows, so the job continues from precisely where it
stopped, with zero lost work and no duplicate writes.

**If refresh is unavailable or fails:** `broker_token.state = 'needs_reauth'`,
`pipeline_state.mode = 'paused_auth'` with a reason string, a `needs_reauth` notification, and an
`auth_required` SSE frame. Every affected job moves to status `blocked_auth`, **not** `failed`.
Tasks stay `pending` with their leases released. The UI shows a persistent banner naming the job
and the parked task count with a one-click re-login. When the OAuth callback completes, the broker
bumps the generation, sets the gate, restores mode `running` and moves `blocked_auth` jobs back to
`queued`. A backfill interrupted at 03:00 resumes the moment the user logs in in the morning, at
the exact task and not at the start of the job.

### 7.3 Why refresh is never assumed

The refresh flow requires the user's PIN (a fourth secret beyond app id, app secret and redirect
URI), returns no rotated refresh token so the 15 day clock is absolute, and is documented as
discontinued from 1 April. The scheduler is therefore architected around `needs_reauth` as a
first-class parked state with visible notification, never around a retry loop that assumes
unattended refresh will work. Storing the PIN is optional and off by default, and the UI states
plainly what storing it buys and what it costs.

---

## 8. Progress to the UI

```
worker task completes
  -> progress.py, at most once per second per active job:
       SELECT state, count(*) FROM task WHERE job_id = ? GROUP BY state
     diff against the previous snapshot
  -> EventBus.publish(frame)          # 512 frame ring buffer, monotonic ids
  -> api/v1/events.py                 # sse-starlette EventSourceResponse
       Cache-Control: no-cache
       X-Accel-Buffering: no
       15 second keepalive comment
       Last-Event-ID replay from the ring buffer
  -> browser useEventStream
       queryClient.setQueryData(['job', id], patch)
```

Progress counters are never incremented in memory. They are always a fresh query over durable
rows, so they are correct after any restart and after any missed frame.

Discrete frames are published immediately rather than on the one-second tick:
`job_started`, `job_finished`, `job_blocked`, `auth_required`, `rate_limited`, `budget_warning`,
`budget_exhausted`, `schedule_fired`, `export_ready`, `notification`.

REST stays authoritative. `GET /api/v1/jobs/{id}` returns the identical aggregate, and the job
detail query still refetches on a slow interval, so a dropped stream degrades the refresh rate and
never the correctness of what is displayed.

SSE was verified to pass through the Vite 8 dev proxy unbuffered, frame by frame, with
`Set-Cookie` intact. WebSocket is deliberately not used: every command already has a REST
endpoint so a bidirectional socket buys nothing, and Vite does not check the origin of proxied
WebSocket upgrades, which its own docs flag as a CSRF risk.

---

## 9. Job and task states

### 9.1 Job status

| Status | Meaning | Exits to |
|---|---|---|
| `draft` | created by the planner but not committed | `queued`, `cancelled` |
| `queued` | tasks written, waiting for the dispatcher | `running`, `cancelled` |
| `running` | at least one task leased or pending and the pipeline is running | any terminal, `paused`, `blocked_*` |
| `paused` | user paused, or recovered from an interrupted run | `running`, `cancelled` |
| `blocked_auth` | pipeline is in `paused_auth`; tasks remain pending | `running` after re-auth |
| `blocked_rate` | pipeline is in `paused_rate`; tasks remain pending | `running` after explicit resume |
| `deferred_budget` | committed but held for the next quota day | `queued` at 00:01 IST |
| `completed` | every task terminal, zero failures | terminal |
| `completed_with_errors` | every task terminal, at least one `failed` | terminal |
| `cancelled` | user cancelled; partial data is valid and the ledger is accurate | terminal |
| `failed` | the job itself could not run (planner error, fatal pipeline stop) | terminal |

### 9.2 Task state

| State | Meaning |
|---|---|
| `pending` | eligible for lease once `not_before` has passed |
| `leased` | held by a worker until `lease_expires_at`; reclaimed automatically if it lapses |
| `done` | request succeeded and rows were written |
| `empty` | request succeeded and Fyers returned `no_data`. A success, rendered distinctly from failed |
| `failed` | FATAL class, or TRANSIENT exhausted `max_attempts` |
| `skipped` | not requested: already covered, sealed, or older than a `no_data` boundary |
| `cancelled` | the job was cancelled before this task ran |

Terminal states are `done`, `empty`, `failed`, `skipped`, `cancelled`.

---

## 10. Scheduler

`APScheduler 3.11.3 AsyncIOScheduler` runs inside the FastAPI lifespan in the same process as the
DuckDB instance, because a second OS process cannot open the file at all while the writer holds
it.

**Jobstore: `MemoryJobStore` only.** `sqlite.schedule` is the persistent source of truth, and
`SchedulerService.sync()` rebuilds every APScheduler job from that table on startup and after
every create, update, enable, disable or delete. A persistent APScheduler jobstore would be a
second, opaque source of truth that the UI cannot render or repair, and pickled triggers survive
across upgrades in surprising ways. With this design, what the user sees on the schedules screen
is exactly what will fire, and a schedule they deleted cannot come back.

Every trigger is a `CronTrigger` with `timezone='Asia/Kolkata'`, `coalesce=True`,
`max_instances=1` and `misfire_grace_time` from the row (default 3600), so a laptop that was
asleep at 18:00 runs the job once when it wakes rather than four times or not at all.

**A fire never fetches anything itself.** It calls the same planner the UI calls, enqueues a
normal job, and writes a `schedule_run` row linking the two. A scheduled download therefore
appears in the same job list, with the same progress, the same retry button and the same task
detail as a manual one.

### 10.1 Two guards above every fire

1. If `broker_token.state = 'needs_reauth'` or `pipeline_state.mode` is `paused_auth`, the run is
   recorded as `skipped_needs_auth` with a notification instead of failing.
2. If `api_budget.blocked_until` is in the future, or the sweep reserve is exhausted, the run is
   recorded as `skipped_blocked` or `skipped_budget`.

The scheduler can therefore never be the thing that spends the third strike.

### 10.2 The ten built-in schedules

All seeded by migration with `is_builtin = 1`, all editable in the UI.

| Kind | Cron (IST) | Priority | Enabled | Purpose |
|---|---|---|---|---|
| `symbol_master` | `15 8 * * *` | 30 | yes | Pull the seven unauthenticated public JSON masters and SCD-2 diff them into `dim_instrument_master`. Consumes no API budget, so it keeps working when the token is dead. This is the only chance to record lot size and tick size before a contract expires and vanishes from the file, and a gap is unrecoverable. |
| `seconds_capture` | `15 16 * * 1-5` | 5 | yes | Capture 5S for contracts that expired inside the last 30 trading days. The highest priority job in the system, because this is the only window in which second-resolution data exists at all and it is genuinely lossy if it does not run. |
| `expiry_discovery` | `0 18 * * 1-5` | 20 | yes | Refresh expiry dates for every active underlying over a trailing 366 day window plus a forward window. |
| `contract_discovery` | `15 18 * * 1-5` | 20 | yes | For any expiry whose `contracts_discovered_at` is NULL and whose date has passed, call underlying-symbols and allocate the id block. |
| `rolling_backfill` | `30 18 * * 1-5` | 40 | yes | The budget-aware workhorse. Pulls the highest priority unsealed gaps from `v_coverage_gaps` and newly expired contracts at each underlying's default resolutions, bounded by `max_requests_per_run` and the 70 percent sweep reserve, then stops cleanly leaving the rest queued for tomorrow. |
| `underlying_history` | `45 18 * * 1-5` | 20 | yes | Incremental spot bars for every active underlying through `/data/history`, resuming from `contract_bounds.last_ts` and applying the partial candle offset. |
| `chain_snapshot` | `25 15 * * 1-5` | 25 | yes | One `options-chain-v3` call per active underlying, for the authoritative W/M `expiry_flag` and live greeks. About four requests a day. |
| `token_health` | `0 * * * *` | 1 | yes | Decode the JWT `exp` locally, warn at 24 hours, park at 120 seconds, re-emit `auth_required` so the banner survives a page reload. |
| `gap_repair` | `0 7 * * 0` | 60 | yes | Re-request coverage rows with status `error` and holes in `v_coverage_gaps`. Never retries FATAL tasks, because those need a human to read the error code. |
| `maintenance` | `0 2 * * *` | 90 | yes | CHECKPOINT, the unscoped duplicate assertion, coverage reconciliation, `rate_event` and raw payload pruning, and the size-versus-model heuristic that raises `compaction_suggested` past 1.4x. |

Plus one non-cron internal timer: `budget_reset` at `1 0 * * *` inserts the new `api_budget` row
for the new IST date, clears a budget-based `blocked_until`, and lifts `stopped_budget` back to
`running` so an overnight backfill continues automatically at the start of the new quota day.

Compaction is deliberately **not** scheduled. It requires closing and swapping the live file, so
it is a manual Optimise action in Settings, guarded by a free-disk-space check.

---

## 11. What the pipeline refuses to do

- It never requests a second resolution for an expiry older than 30 trading days.
- It never requests a range before the exchange availability floor.
- It never requests a sealed contract without `force_refresh`.
- It never indexes a candle array positionally.
- It never uses `executemany`.
- It never retries a single request after a 429; it stops the whole pipeline.
- It never assumes a token refresh will succeed.
- It never infers an expiry day from a last-Thursday or last-Tuesday rule. NSE and BSE expiry
  weekdays have changed several times since 2022 and any hardcoded rule silently corrupts
  historical data. The expiry date always comes from the request that discovered the contract.
