# ExpiryManager

A zero-configuration data pipeline and warehouse for expired Indian F&O contract data from the
Fyers API. It downloads, stores, charts and exports historical options and futures data, plus the
underlying index and equity series, on a schedule.

Built to be the datasource for options and futures research. An options backtesting engine reading
this warehouse is the planned second phase.

Status: in active development. The design is complete and the implementation is under way. See
`docs/` for the full architecture.

## What it does

Pick an underlying, pick the expiries you want, and it downloads every contract for those expiries
into DuckDB, with the metadata to make the data queryable years later.

```
Fyers API  ->  Data Pipeline  ->  DuckDB  ->  CSV and Parquet exports
                                          ->  charts
```

- Ships with NIFTY, BANKNIFTY, SENSEX and RELIANCE. You can add your own underlyings.
- Every expired option and future contract symbol is decomposed into structured columns:
  underlying, expiry date, strike, option right, weekly or monthly, exchange, segment.
- Open interest is captured alongside OHLCV.
- Full provenance on every row: which job fetched it, when, from which request, and the checksum of
  the response.
- Exports to CSV and Parquet.
- Charts through [openalgo-charts](https://github.com/marketcalls/openalgo-charts).
- A scheduler for recurring downloads.

## Why the design looks the way it does

Three constraints drive nearly every decision.

**DuckDB allows exactly one process to hold the database file.** A second process cannot open it
while a writer holds it, not even read only. So the app is a single uvicorn worker with one DuckDB
instance, an in-process scheduler and a single writer task. This is deliberate, not a limitation
waiting to be fixed.

**The Fyers rate limit is the binding resource, not disk or CPU.** Market data APIs allow 10
requests per second, 200 per minute and 100,000 per day, and exceeding the per-minute limit more
than three times in a day gets you blocked for the rest of the day. A full NIFTY 2022 to 2026
backfill costs roughly 62,000 requests. Every request the app spends is therefore budgeted,
visible and resumable.

Because of that, downloads are planned before they are run. Asking for a plan costs zero Fyers
requests: the planner answers from local state alone and returns how many requests the job needs,
how many rows it will produce, how long it will take and how much of today's budget is left. The
start button stays disabled, with a reason, if the plan would exceed the remaining budget.

**There is no .env file.** The Fyers app id, secret and redirect URL are entered in the UI and
stored encrypted in SQLite. Credentials never live in a file you might commit.

## Security

- Secrets are encrypted at rest with AES-256-GCM. The additional authenticated data binds each
  ciphertext to its table, column, row and key version, so ciphertext cannot be moved between rows.
- The master key lives in a 0600 file outside the database, so the database file leaking on its own
  (a backup, a sync folder, a stray `git add .`) does not leak credentials. An OS keyring provider
  and a passphrase provider are available as options.
- The app secret is write only in the UI. After it is saved the field reports that it is configured
  and never renders the value. No API response and no log line ever contains it.
- Session cookies are paired with a CSRF token and an origin check. The Vite dev proxy gives a
  single browser origin in development and in production, so CORS is not needed at all.
- Inbound rate limiting, security headers, and SQLite hardening with WAL and 0600 file modes.

The threat model is written down honestly in `docs/SECURITY.md`, including what this design does
not defend against. Malware running as your own user account is not defendable here, and the app
does not pretend otherwise.

## Authentication and the daily login

SEBI's retail algorithmic trading framework, effective 1 April 2026, discontinued the refresh
token flow and made a daily two factor login mandatory. ExpiryManager is built around that rather
than against it.

- You log in to Fyers once a day through the app.
- At 03:00 IST the app logs itself out and clears the access token.
- Running jobs are not failed by this. They checkpoint, move to `awaiting_authentication`, and
  resume automatically after your next successful login.
- The download planner sizes work against both the remaining daily request budget and the time
  left before the next logout.

Note that the SEBI restrictions on static IP whitelisting, single app registration and order types
apply to order placement. ExpiryManager only uses non-transactional market data APIs, so they do
not apply to it.

## Requirements

- Python 3.13 or newer
- Node 22 or newer
- A Fyers account with an app created at the
  [API Dashboard](https://fyers.in/web/api-dashboard/user-apps)

The registered redirect URI must be `https://127.0.0.1:8000/fyers/callback`. Fyers matches it
exactly. Note the `https`: the app generates its own self signed certificate on first run, so
expect a browser certificate warning the first time you visit.

## Getting started

Instructions will be added as the implementation lands. The intended first run is:

1. Start the app. It creates its data directory, its databases and its TLS certificate on its own.
2. Open the UI. A setup wizard asks for your Fyers app id, secret and redirect URL, and stores them
   encrypted.
3. Log in to Fyers. This is the one interactive step, and it is required once a day.
4. Pick an underlying, pick expiries, review the plan, and start the download.

There is nothing to edit by hand at any point.

## Documentation

| Document | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | Components, process model, end to end flow, module layout, trust boundaries |
| `docs/DATA-MODEL.md` | Every SQLite and DuckDB table with DDL, and the queries the backtester will run |
| `docs/PIPELINE.md` | Job decomposition, the rate limit governor, retries, checkpointing, resume, scheduling |
| `docs/API.md` | Every REST endpoint with request and response models |
| `docs/SECURITY.md` | Threat model, encryption scheme, key management, CSRF, headers |
| `docs/BUILD-PLAN.md` | Implementation phases and work items |
| `docs/research/` | Source research notes behind the design decisions |

## Roadmap

Phase 1, in progress: the data pipeline, the warehouse, charts, exports and the scheduler.

Phase 2, planned: an options backtesting engine reading this warehouse directly.

## License

MIT. See [LICENSE](LICENSE).
