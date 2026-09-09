# ExpiryManager

A zero-configuration data platform for Indian expired F&O contracts, built on the Fyers broker
API. It downloads complete expiry and contract catalogs, backfills historical OHLCV plus open
interest for the expiries you choose, keeps everything on a schedule, charts it, and exports it to
CSV and Parquet.

It is the datasource for options and futures research, and it is built so that a Phase 2 options
backtesting engine can read the store directly without a rewrite.

---

## What it does

- **Complete expiry data** for NIFTY, BANKNIFTY, SENSEX and RELIANCE out of the box, and any other
  underlying you add through the UI.
- **Pick and download.** Choose an underlying, tick the expiries you want, choose resolutions, and
  see exactly what the download will cost in Fyers requests, time, rows and disk before it starts.
- **A real pipeline.** Every outbound request is a durable row, so a multi-day backfill survives a
  restart, a token expiry or a closed laptop and resumes at the exact request it stopped on.
- **Schedulers.** Ten built-in schedules keep the catalog current, capture second-resolution data
  inside the only 30 day window in which it exists, and snapshot the Fyers symbol master daily so
  lot sizes and tick sizes are recorded before expired contracts disappear from it.
- **Maximum metadata.** Full request provenance per chunk, the verbatim response fragment per
  contract, a slowly changing dimension for lot and tick size, and every field that can be parsed
  out of the Fyers symbology.
- **Charts.** Every contract renders in openalgo-charts with open interest as a separate pane.
- **Exports.** DuckDB writes denormalised, self-describing Parquet and CSV, and the archive layout
  includes the catalog so an export doubles as a restorable backup.

---

## Zero-config first run

There is no `.env` file and no environment variable to set. Nothing needs editing before the app
runs, and nothing outside the app data directory is ever written.

1. Start the app and open `http://127.0.0.1:8000`.

   Plain HTTP, deliberately. The scheme is not a preference: Fyers matches the
   registered redirect URI character for character, and the one registered for this
   app is `http://127.0.0.1:8000/fyers/callback`. Loopback is the one place http is
   legitimate, since a self-signed certificate on 127.0.0.1 protects nothing that
   matters. If you re-register the URI as https, start the app with `--https` and it
   generates its own certificate; the session cookie then carries `Secure` to match.
2. **Step 1, set a local passcode.** This protects the app itself. It is hashed with Argon2id and
   is never stored in plaintext.
3. **Step 2, paste your Fyers app credentials.** App id and app secret, taken from your Fyers app
   registration. The redirect URL is fixed at `http://127.0.0.1:8000/fyers/callback` and the
   screen shows it with a copy button: register exactly that string on the Fyers dashboard,
   because Fyers matches it character for character. The secret is encrypted with AES-256-GCM before it reaches the database, under a key
   held in a 0600 file outside the database, and it is never displayed again, not even masked.
4. **Step 3, click Connect.** You are sent to Fyers to authenticate with your password and TOTP,
   and returned automatically. If the return does not land, for example because you declined the
   certificate warning in that tab, paste the URL you were redirected to into the fallback box on
   the same screen. It runs exactly the same verification.

This interactive login is the only manual step in the whole system. Everything after it, including
the scheduler, runs without you.

On first boot the app creates everything it needs:

```
~/.expirymanager/
  master.key         the encryption key, 0600 inside a 0700 directory
  tls/server.key     the self-signed TLS key and certificate, 0600, renewed when they expire
  tls/server.crt
  config.sqlite3     settings, credentials, jobs, tasks, schedules
  market.duckdb      the catalog and every candle
  exports/           CSV and Parquet you create
  raw/               archived raw responses, for auditing
  logs/
  tmp/
```

You are then on the dashboard with the four builtin underlyings ready and the schedules enabled.
Nothing has been downloaded yet: go to Expiries, pick an underlying, select some expiries, and
open the download sheet.

---

## Prerequisites

| Requirement | Version | Note |
|---|---|---|
| Python | 3.14.6 | Anything from 3.12 works, 3.14.6 is what this is built and tested against. |
| Node | 26.4.0 | Needed only to build the frontend. Vite 8 requires `^20.19.0 \|\| >=22.12.0`. |
| npm | 11.17.0 | |
| A Fyers account | any | With an app registered whose redirect URI is exactly `http://127.0.0.1:8000/fyers/callback`. |
| Disk | 10 to 20 GB | The four seed underlyings over 2022 to 2026 at one minute land around 6 to 9 GB. |
| Network | outbound https | To `api-t1.fyers.in` and `public.fyers.in`. |

The app runs on loopback only and is designed for a single local user on their own machine.

---

## Running it

### Build the frontend once

```
cd frontend
npm install
npm run build
```

### Run

```
cd backend
uv sync
uv run expirymanager
```

Then open `http://127.0.0.1:8000` and accept the self-signed certificate once.

That is the whole thing: one process serving the API and the built frontend from the same origin.

### Development

Two terminals, because the frontend needs the Vite dev server:

```
# terminal 1
cd backend && uv run expirymanager --reload     # http://127.0.0.1:8000

# terminal 2
cd frontend && npm run dev                      # http://127.0.0.1:5173
```

The dev server serves HTTPS using the same certificate the backend generated, and proxies `/api`
to the backend. Use `http://127.0.0.1:5173` and not `localhost`: cookies ignore the port but not
the host, and the same host is what lets the session cookie set by the OAuth callback on port 8000
be seen by the dev origin on port 5173.

The proxy means the browser talks to exactly one origin in development as well as in production,
so cookies and CSRF behave identically in both.

---

## One rule that matters

**Only one process may hold `market.duckdb` at a time.** DuckDB refuses a second opener while a
writer holds the file, not even read-only. That means:

- Do not run `uvicorn` with more than one worker.
- Do not run the scheduler as a separate process. It runs inside the app.
- Do not leave a `duckdb` CLI session, a DBeaver connection or a notebook open against the file
  while the app is running.

If startup reports that the lock could not be taken, one of those three is the cause. Close it and
start again.

---

## Understanding the Fyers budget

The app's most important number is on the top bar at all times. The Fyers Standard plan allows
10 requests per second, 200 per minute and 100,000 per day, and **exceeding the per-minute limit
more than three times in one day blocks the account for the rest of the day.**

ExpiryManager therefore:

- runs its own limiter at 8 per second and 170 per minute, under the published caps,
- keeps the daily counter and the strike counter in the database so a restart cannot reset them,
- stops the entire pipeline on the first rate-limit response and waits for you to resume, rather
  than retrying and spending a second strike,
- reserves 30 percent of the daily budget for downloads you start by hand, so a nightly sweep can
  never consume the whole day,
- and never starts a download without first showing you the request cost.

A full NIFTY 2022 to 2026 backfill is roughly 62,000 requests, about two thirds of one day of
Standard quota. Plan for it to span an evening, and let the scheduler continue it the next day.

---

## Things the broker limits, not us

- **Second-resolution data exists only for the last 30 trading days.** It can never be backfilled.
  The `seconds_capture` schedule runs daily and is the highest priority job in the system; if the
  machine is off for a month, that month of 5S data is permanently gone.
- **Daily, weekly and monthly candles are not available for expired contracts.** Any daily series
  in this app is aggregated from one minute bars inside DuckDB.
- **Data starts on 03 January 2022 for NSE and MCX, and 07 August 2023 for BSE.** Requests are
  clamped to those floors.
- **The one manual step is the first Fyers login.** It needs an interactive browser login with
  your account password and TOTP, which nothing can automate. Everything after it, including the
  scheduler, runs without you.
- **Refresh tokens are documented as discontinued from 1 April**, require your Fyers PIN, and
  issue no rotated token. The app therefore treats a dead token as a first-class parked state with
  a visible banner and a one-click re-login, and never as a retry loop. Jobs park rather than
  fail, and resume at the exact request when you log back in.
- **Expired contracts vanish from the Fyers symbol master.** Lot size and tick size for a contract
  can only ever be captured while it is live, which is why the daily symbol master snapshot runs
  from day one and is the one job that keeps working when the broker token is dead.

---

## Where the data lives

`market.duckdb` is the source of truth. CSV and Parquet are exports, never inputs.

The candles table is nine columns wide (`contract_id`, `res_id`, `ts`, four `DECIMAL(9,2)` prices,
`volume`, `oi`), physically sorted by `(contract_id, res_id, ts)`, with no primary key and no
index, because that layout measured 15.21 bytes per row and 0.1 ms per-contract queries. All
descriptive richness lives in a small catalog joined at query time. Timestamps are naive
`TIMESTAMP` holding IST wall clock, so no query depends on a session timezone setting.

You can open the file with any DuckDB client while the app is **not** running, and the shipped
macros give you the same vocabulary the app uses:

```sql
SELECT * FROM bars(42101, 2, TIMESTAMP '2025-03-01', TIMESTAMP '2025-03-28');
SELECT * FROM chain_at(1, DATE '2025-03-27', 2, TIMESTAMP '2025-03-27 14:30:00');
SELECT * FROM atm_strike(1, DATE '2025-03-27', 2, TIMESTAMP '2025-03-27 14:30:00');
```

---

## Maintenance

- **Storage** in Settings shows the file size against the modelled size. A DuckDB file only grows,
  and `VACUUM` reclaims nothing, so heavy re-downloading raises the high water mark.
- **Optimise** rewrites the database sorted, which is the only real way to reclaim space. It has
  to close and swap the file, so it is a deliberate manual action and not a schedule.
- **Backup** checkpoints first and copies the DuckDB file, its WAL and the SQLite files together.
  Copying the `.duckdb` alone, or without checkpointing, restores a database missing the most
  recent writes.
- Do not put `~/.expirymanager` inside iCloud Drive, Dropbox, OneDrive or Google Drive. The app
  refuses to start there, because a sync client plus WAL corrupts both databases.

---

## Documentation

| Document | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | Components, process model, end-to-end flow, module layout, trust boundaries. |
| `docs/DATA-MODEL.md` | Every table with exact DDL, the metadata captured, and the Phase 2 queries the schema is built for. |
| `docs/PIPELINE.md` | Job and task decomposition, the rate limiter, retry policy, resume, idempotent writes, token expiry, the scheduler. |
| `docs/API.md` | Every endpoint with method, path, models, auth, rate limit and error cases. |
| `docs/SECURITY.md` | Encryption scheme, key location, OAuth, sessions, CSRF, headers, and the never-log and never-return lists. |
| `docs/BUILD-PLAN.md` | Ordered implementation phases and the parallelisable work items. |
| `docs/research/` | The verified research notes the design is built on. Every number in them was measured. |

---

## Writing rules for this repository

Inherited from openalgo-charts and applied project-wide:

- No emoji and no icons anywhere: code, comments, log messages, commit messages, docs, tests or
  terminal output. Plain text labels only.
- No em dashes and no en dashes. Use a comma, a colon, parentheses or a full stop.
- Comments explain why, not what.
- Conventional Commits.
