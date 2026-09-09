# DuckDB at Scale for ExpiryManager

Research notes for storing and querying a very large options and futures OHLCV time series
in DuckDB, driven from a Python FastAPI service with a background scheduler.

Every number in this document was measured on this machine unless it is explicitly labelled
as an estimate. Benchmark method and raw output are in the appendix.

## 0. Verified environment

| Item | Value | How verified |
|---|---|---|
| DuckDB Python package, latest stable | 1.5.5 | PyPI JSON index query |
| DuckDB engine reported by `SELECT version()` | v1.5.5 | executed |
| pyarrow | 25.0.1 | installed and executed |
| polars | 1.44.2 | installed and executed |
| pandas | 3.0.5 | installed and executed |
| Python | 3.14.3 (project target 3.14.6) | executed |
| Host | Apple Silicon, 10 DuckDB worker threads, 12.7 GiB default memory limit | `duckdb_settings()` |
| DuckDB default storage block size | 262144 bytes (256 KiB) | `PRAGMA database_size` |
| DuckDB native row group size | 122880 rows | DuckDB storage constant, matches observed segment counts |
| `checkpoint_threshold` / `wal_autocheckpoint` default | 16.0 MiB | `duckdb_settings()` |
| Default session `TimeZone` | inherited from the OS, here `Asia/Kolkata` | `current_setting('TimeZone')` |

DuckDB 1.5.5 is the newest stable release on PyPI. Everything above 1.5.5 on the index is a
`1.6.0.devN` prerelease. Pin `duckdb==1.5.5` in the backend requirements. The on disk storage
format is stable and forward compatible within the v1.x line, but a file written by a newer
minor version cannot always be opened by an older one, so pin it and record the version in the
catalog metadata.

Fyers payload shape confirmed from `24-expired-f-o-contracts-data.md`:

```
"candles": [[1742960700, 730.00, 750.00, 645.00, 706.00, 359100, 5131225], ...]
"columns": ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
```

So a candle arrives as `[epoch_seconds_utc, open, high, low, close, volume, open_interest]`,
with `open_interest` present only when `include_oi=1`. Epoch 1742960700 renders as
2025-03-26 09:15:00 IST, which confirms the epoch is a true UTC instant and the bar label is
the bar open in IST.

## 1. Executive recommendations

1. **One monolithic DuckDB file is the store.** Parquet and CSV are exports, never the source
   of truth. See section 5 for the full argument and the escape hatch.
2. **Keep the candles table at nine columns.** `contract_id INTEGER`, `res_id UTINYINT`,
   `ts TIMESTAMP`, four price columns, `volume`, `oi`. Everything else lives in a small
   `contracts` catalog joined at query time.
3. **Prices are `DECIMAL(9,2)`, not DOUBLE and definitely not FLOAT.** Measured 15.21 bytes per
   row against 17.36 for DOUBLE and 22.28 for FLOAT, and it is exact for NSE and BSE tick sizes.
4. **`ts` is a naive `TIMESTAMP` holding IST wall clock.** Not `TIMESTAMPTZ`. The conversion from
   the Fyers epoch is a fixed `+19800` seconds, done vectorised in Arrow at ingest.
5. **Never put a PRIMARY KEY or UNIQUE constraint on the candles table.** Measured: adding
   `PRIMARY KEY (contract_id, ts)` to a 5,000,000 row table grew the file from 75.5 MB to
   341.6 MB (4.5x) and slowed the load from 0.45 s to 2.02 s (4.5x). At the target scale that
   alone would be tens of gigabytes of ART index.
6. **Idempotency is delete then insert inside one transaction, scoped to
   `(contract_id, res_id, [range_from, range_to])`.** Measured 3 ms versus 11 ms for
   `MERGE INTO` on a 5,000,000 row table, and unlike `MERGE` its cost does not grow with total
   table size.
7. **Bulk insert via Arrow, never `executemany`.** Measured 8,350 rows per second for
   `executemany` against 9.9 to 11.2 million rows per second for an Arrow table scan. That is a
   1200x gap, and it is the single largest performance decision in the ingestion path.
8. **Exactly one process opens the DuckDB file read-write, and inside it exactly one writer
   task owns all writes.** This is not a style preference. A second OS process cannot open the
   file at all while a writer holds it, not even read-only (verified, `IOException: Could not
   set lock on file`).
9. **Readers use `connection.cursor()` off the single cached database instance.** Measured
   ~10,900 queries per second across 8 threads, and readers were not blocked while the writer
   committed.
10. **Sort key is `(contract_id, res_id, ts)`.** Measured 0.1 ms versus 2.6 ms for a single
    contract aggregate on 5,000,000 rows (26x) purely from zone map pruning.
11. **Periodic offline compaction by `ATTACH` plus `CREATE TABLE AS SELECT ... ORDER BY`.**
    `VACUUM` does not shrink a DuckDB file. Measured 48.2 MB compacting to 34.1 MB (29%).

## 2. Sizing model

Measured on disk cost of the recommended schema, after `CHECKPOINT`, using tick aligned
synthetic prices (multiples of 0.05, which is what NSE and BSE actually produce):

**15.21 bytes per row.**

Applying that to the workload:

| Scenario | Rows | DuckDB file |
|---|---|---|
| One NIFTY weekly expiry, ~300 traded contracts, ~3000 one minute bars each | 0.9 M | 14 MB |
| NIFTY, one year of weeklies plus monthlies | ~47 M | 0.7 GB |
| NIFTY, 2022 to 2026 | ~190 M | 2.9 GB |
| NIFTY + BANKNIFTY + SENSEX + RELIANCE, 2022 to 2026 | ~400 M to 600 M | 6 to 9 GB |
| Pessimistic ceiling with more underlyings and more resolutions | 1 B | ~15 GB |

A 15 GB single file is entirely routine for DuckDB. It is not a reason to shard. Memory is the
thing to watch, not file size: DuckDB defaults its memory limit to 80 percent of RAM and spills
to `temp_directory` beyond that, so set both explicitly (section 7.5).

Cross check against the feed side. Fyers Standard plan allows 10 requests per second, 200 per
minute, 100,000 per day. One `historical-data` call covers one contract for up to 100 days at
minute resolution, so it returns on the order of 3000 usable candles for a weekly option.

- Feed ceiling: 200 requests/min x 3000 candles = 600,000 candles/min = **10,000 rows/s**.
- DuckDB sustained ingest, measured: **1,036,582 rows/s** with one transaction per 3000 row batch.

DuckDB is roughly 100x faster than the fastest the broker will ever feed it. This is the
justification for keeping the write path simple: a single writer is not a bottleneck and never
will be. The daily request cap is the real constraint. A full NIFTY 2022 to 2026 backfill is
roughly 52 x 4 x 300 = 62,000 contract requests, which is about two thirds of one day of
Standard plan quota, so the scheduler needs a durable request budget, not a faster database.

## 3. Schema

### 3.1 The candles table

```sql
CREATE TABLE IF NOT EXISTS candles (
    contract_id  INTEGER      NOT NULL,   -- FK to contracts.contract_id
    res_id       UTINYINT     NOT NULL,   -- FK to resolutions.res_id
    ts           TIMESTAMP    NOT NULL,   -- bar open, naive, IST wall clock
    open         DECIMAL(9,2) NOT NULL,
    high         DECIMAL(9,2) NOT NULL,
    low          DECIMAL(9,2) NOT NULL,
    close        DECIMAL(9,2) NOT NULL,
    volume       BIGINT       NOT NULL,
    oi           BIGINT                   -- NULL when include_oi was 0, or for indices
);
```

Nine columns, no constraints, no indexes. Rationale for each decision follows.

### 3.2 Key design: `contract_id`, not the symbol string

Measured, 5,000,000 rows, sorted, DOUBLE prices:

| Row key | File size |
|---|---|
| `contract_id INTEGER` | 87.0 MB |
| `sym VARCHAR` (Fyers symbol, ~22 chars) | 89.9 MB |

The size delta looks small because DuckDB dictionary compresses a sorted low cardinality
VARCHAR column very well. Size is not the reason to use an integer id. The reasons are:

- **Predicate cost.** `contract_id = 317` is an integer compare against a zone map min/max.
  `sym = 'NSE:NIFTY25MAR23000CE'` is a string compare, and string zone maps only prune on the
  first 8 bytes of the value, which for Fyers symbols is the common prefix `NSE:NIFT`. Pruning
  collapses to nothing for the exact case that matters most.
- **Join and group cost.** Every phase 2 backtest operation (build a chain, pick ATM, pair a
  call and a put) groups by contract. Integer grouping is materially cheaper.
- **Renaming and correction.** If Fyers ever changes a symbol string or the catalog needs a
  correction, an integer id means editing one catalog row instead of rewriting hundreds of
  millions of candle rows.
- **The dictionary is not free at scale.** With 500 distinct symbols the dictionary is trivial.
  With 500,000 distinct expired contracts across four years it is not, and it is rebuilt per
  row group.

Assign `contract_id` from a sequence at the moment an expiry's contract list is first ingested
from `/underlying-symbols`. This is deliberate: it makes ids for one `(underlying, expiry)`
contiguous, which combined with the `ORDER BY contract_id` sort key gives physical clustering
for the "scan the whole chain for this expiry" query that the backtester will live on, without
storing `underlying_id` or `expiry_date` in the candles table.

`res_id UTINYINT` rather than the resolution string. Measured compression for `res_id` is
`Constant` inside every row group, so it costs effectively zero bytes when a row group holds a
single resolution, which it will. Do not split into one table per resolution: it multiplies
DDL, breaks the single sort order, and buys nothing that the `Constant` compression and the
zone map do not already give.

### 3.3 Timestamp handling and timezone

**Store `ts` as a naive `TIMESTAMP` containing the IST wall clock of the bar open.**

The trap, verified:

```
SELECT current_setting('TimeZone');            -- 'Asia/Kolkata' on this machine, from the OS
SELECT typeof(to_timestamp(1742960700));       -- TIMESTAMP WITH TIME ZONE
SELECT to_timestamp(1742960700);               -- 2025-03-26 09:15:00+05:30
```

DuckDB's session `TimeZone` defaults to the host OS zone, and `to_timestamp` returns
`TIMESTAMPTZ`. That means the same query text renders different strings on a developer laptop
in UTC, a container with `TZ` unset, and the user's machine. For a system whose whole purpose
is Indian market sessions, that is an unacceptable source of silent off by 5.5 hour bugs.

Options considered:

| Option | Bytes | Verdict |
|---|---|---|
| `TIMESTAMPTZ` (UTC instant, rendered per session TZ) | 8 | Correct but display and literal comparison depend on a global session setting. Every reader would have to `SET TimeZone='Asia/Kolkata'`, and a missed `SET` is a silent wrong answer. Arrow round trips carry tz metadata that pandas turns into tz aware indexes, which openalgo-charts and the backtester then have to normalise again. Rejected. |
| Naive `TIMESTAMP` holding UTC | 8 | Every chart axis, every session filter (09:15 to 15:30), every expiry day cutoff would need `+ INTERVAL 330 MINUTE`. Rejected. |
| Naive `TIMESTAMP` holding IST | 8 | Recommended. |
| `TIMESTAMP_S` (second precision) | 8 | Measured 73.9 MB against 76.0 MB for `TIMESTAMP`, a 2.8 percent saving. Not worth the weaker function coverage and the Arrow interop friction. Rejected. |
| `INTEGER` epoch seconds | 4 | Smallest, but every query needs a conversion function and charts and exports become unreadable. Rejected. |

India has no daylight saving time and has had a fixed UTC+05:30 offset since 1945, so naive IST
is lossless. There is no ambiguous or nonexistent local time to worry about, which is the usual
reason to avoid naive local timestamps.

The conversion at ingest is a fixed integer offset, done vectorised:

```python
IST_OFFSET_SECONDS = 19800  # UTC+05:30, fixed, India has no DST

ts_micros = pa.array(
    [(row[0] + IST_OFFSET_SECONDS) * 1_000_000 for row in candles],
    type=pa.int64(),
)
ts_col = ts_micros.cast(pa.timestamp("us"))   # no tz -> maps to DuckDB TIMESTAMP
```

Going back to a true epoch, when the chart or an export needs one:

```sql
SELECT epoch(ts) - 19800 AS epoch_utc FROM candles;
```

Record the convention in the catalog so it cannot be forgotten: add a
`meta(key VARCHAR, value VARCHAR)` table with `('candles.ts.timezone', 'Asia/Kolkata')` and
`('candles.ts.semantics', 'naive local wall clock, bar open')`.

Also `SET TimeZone='Asia/Kolkata'` on every connection anyway, as belt and braces for any
`TIMESTAMPTZ` that sneaks into an ad hoc query.

### 3.4 Price type: DECIMAL vs DOUBLE

Measured, 5,000,000 rows, tick aligned prices (multiples of 0.05), sorted by
`(contract_id, ts)`, after `CHECKPOINT`:

| Price type | File | Bytes/row | Compression chosen by DuckDB |
|---|---|---|---|
| `DECIMAL(9,2)` | 76.0 MB | 15.21 | BitPacking |
| `DECIMAL(12,2)` | 78.1 MB | 15.63 | BitPacking |
| `DOUBLE` | 86.8 MB | 17.36 | ALP |
| `DECIMAL(11,4)` | 96.5 MB | 19.30 | BitPacking |
| `DECIMAL(18,4)` | 96.5 MB | 19.30 | BitPacking |
| `FLOAT` | 111.4 MB | 22.28 | ALPRD |

Three things to take from this.

- **`FLOAT` is worse than `DOUBLE`, by 28 percent.** This is counterintuitive and it is the
  reason to measure rather than assume. DuckDB compresses floating point with ALP (Adaptive
  Lossless floating Point). For a `DOUBLE` holding `k * 0.05` ALP finds an exponent that makes
  the value an exact integer and bit packs it. The 32 bit variant does that far less
  effectively on this data and falls back to ALPRD, the "real double" path, which is close to
  storing raw bits. Never use `FLOAT` for prices here.
- **`DECIMAL(9,2)` is the sweet spot.** DuckDB stores `DECIMAL` with precision 1 to 4 in INT16,
  5 to 9 in INT32, 10 to 18 in INT64 and 19 to 38 in INT128. Precision 9 keeps it in INT32,
  which then bit packs to the actual value range. Precision 10 or more doubles the physical
  width, which is why `DECIMAL(11,4)` and `DECIMAL(18,4)` measured identically.
- **`DECIMAL(9,2)` range is 0.01 to 9,999,999.99**, which comfortably covers NIFTY (~26,000),
  SENSEX (~85,000), BANKNIFTY (~58,000), any option premium, and even the highest priced Indian
  single stock future. It is exact for the NSE and BSE tick of 0.05.

Ergonomics were checked and are fine:

```
DECIMAL(9,2) -> arrow decimal128(9,2) -> pandas float64 -> polars Decimal
100.05::DECIMAL(9,2) / 99.95::DECIMAL(9,2)  ->  DOUBLE (auto promoted)
ln(100.05::DECIMAL(9,2))                    ->  DOUBLE (auto promoted)
```

Division, logs and any analytic function auto promote to DOUBLE, so returns and greeks math
needs no explicit casts. `.df()` hands pandas a plain float64 column.

**Caveat to write down now:** currency derivatives (USDINR and friends) quote to 4 decimals with
a 0.0025 tick. If the user ever adds a currency underlying, `DECIMAL(9,2)` silently truncates.
Guard it at ingest with an explicit segment allowlist and raise rather than round. If currency
support becomes real, add a second table `candles_fx` with `DECIMAL(11,4)` rather than widening
the main table and paying 27 percent more disk on 600 million equity and index rows.

`volume BIGINT` and `oi BIGINT` cost nothing extra: DuckDB bit packs them to the observed range
(measured `BitPacking`), so declaring BIGINT buys headroom for free. `oi` is nullable because
`include_oi` is optional and indices have no open interest. Nullability was measured to cost
nothing here (76.0 MB either way) because the validity mask compresses to a constant when there
are no nulls.

### 3.5 The catalog tables

Keep these in the same DuckDB file, not in SQLite. They are joined against `candles` on every
query and a cross database join would force a round trip through Python. They are small
(hundreds of thousands of rows at most), so they cost nothing.

SQLite remains the store for configuration, encrypted Fyers credentials, job and scheduler
state, and user preferences, exactly as the stack decision says. The line is: **SQLite owns
things a human edits and things that must survive a DuckDB rebuild. DuckDB owns things derived
from market data.**

```sql
CREATE SEQUENCE IF NOT EXISTS seq_contract_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_underlying_id START 1;

CREATE TABLE IF NOT EXISTS underlyings (
    underlying_id   INTEGER PRIMARY KEY,
    fyers_symbol    VARCHAR NOT NULL UNIQUE,   -- 'NSE:NIFTY50-INDEX'
    exchange        VARCHAR NOT NULL,          -- 'NSE' | 'BSE' | 'MCX'
    segment         VARCHAR NOT NULL,          -- 'CM' | 'FO' | 'CD' | 'COM'
    short_name      VARCHAR NOT NULL,          -- 'NIFTY'
    display_name    VARCHAR NOT NULL,          -- 'Nifty 50'
    instrument_kind VARCHAR NOT NULL,          -- 'INDEX' | 'EQUITY' | 'COMMODITY'
    lot_size        INTEGER,
    tick_size       DECIMAL(9,4),
    data_from       DATE NOT NULL,             -- NSE 2022-01-03, BSE 2023-08-07, MCX 2022-01-03
    is_builtin      BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP NOT NULL DEFAULT current_localtimestamp(),
    updated_at      TIMESTAMP NOT NULL DEFAULT current_localtimestamp()
);

CREATE TABLE IF NOT EXISTS contracts (
    contract_id     INTEGER PRIMARY KEY,       -- from seq_contract_id, allocated per expiry batch
    underlying_id   INTEGER NOT NULL,
    fyers_symbol    VARCHAR NOT NULL UNIQUE,   -- 'NSE:NIFTY25MAR23000CE'
    fytoken         VARCHAR,                   -- see 29-appendix.md, capture it when available
    exchange        VARCHAR NOT NULL,
    segment         VARCHAR NOT NULL,
    instrument_type VARCHAR NOT NULL,          -- 'FUT' | 'CE' | 'PE'
    expiry_date     DATE    NOT NULL,
    strike          DECIMAL(12,2),             -- NULL for futures
    option_type     VARCHAR,                   -- 'CE' | 'PE', NULL for futures
    lot_size        INTEGER,
    tick_size       DECIMAL(9,4),
    expiry_kind     VARCHAR,                   -- 'WEEKLY' | 'MONTHLY' | 'QUARTERLY'
    is_expired      BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at   TIMESTAMP NOT NULL DEFAULT current_localtimestamp(),
    source_endpoint VARCHAR NOT NULL DEFAULT 'expired/underlying-symbols',
    raw_payload     JSON                       -- maximum metadata capture, see note
);

CREATE TABLE IF NOT EXISTS resolutions (
    res_id       UTINYINT PRIMARY KEY,
    fyers_code   VARCHAR NOT NULL UNIQUE,      -- '1', '5', '60', '5S'
    seconds      INTEGER NOT NULL,
    max_days_per_request INTEGER NOT NULL,     -- 100 for minute codes
    is_intraday  BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS expiries (
    underlying_id INTEGER NOT NULL,
    expiry_date   DATE    NOT NULL,
    has_futures   BOOLEAN NOT NULL DEFAULT FALSE,
    has_options   BOOLEAN NOT NULL DEFAULT FALSE,
    contract_count INTEGER,
    discovered_at TIMESTAMP NOT NULL DEFAULT current_localtimestamp(),
    PRIMARY KEY (underlying_id, expiry_date)
);
```

`strike` is `DECIMAL(12,2)`, deliberately not DOUBLE. Strike equality and grouping is the single
most common catalog predicate in options work (ATM selection, vertical spreads, strike ladders)
and floating point equality on strikes is a known source of missing legs. Half point strikes do
exist on some underlyings, so integer storage is not safe either.

Primary keys on the catalog tables are fine and wanted. The ART index cost measured in section
1.5 is a function of row count, and these tables have on the order of 10^5 rows, not 10^9.

`raw_payload JSON` on `contracts` is how "capture the maximum amount of metadata" is satisfied
without widening the hot table: keep the verbatim Fyers response fragment for the contract so
that a field Fyers adds later is not lost, and promote fields to real columns as they become
useful. DuckDB's `JSON` type is compressed like a VARCHAR and this table is small.

### 3.6 Ingestion bookkeeping (what makes re-download safe)

```sql
CREATE TABLE IF NOT EXISTS candle_coverage (
    contract_id   INTEGER NOT NULL,
    res_id        UTINYINT NOT NULL,
    range_from    DATE NOT NULL,
    range_to      DATE NOT NULL,
    row_count     BIGINT NOT NULL,
    first_ts      TIMESTAMP,
    last_ts       TIMESTAMP,
    fetched_at    TIMESTAMP NOT NULL DEFAULT current_localtimestamp(),
    http_status   INTEGER,
    fyers_code    INTEGER,
    payload_sha256 VARCHAR,          -- dedup identical re-downloads without touching candles
    include_oi    BOOLEAN NOT NULL,
    PRIMARY KEY (contract_id, res_id, range_from, range_to)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        BIGINT PRIMARY KEY,
    job_kind      VARCHAR NOT NULL,   -- 'expiry_discovery' | 'contract_discovery' | 'candle_backfill'
    underlying_id INTEGER,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    status        VARCHAR NOT NULL,   -- 'running' | 'ok' | 'failed' | 'cancelled'
    requests_made INTEGER NOT NULL DEFAULT 0,
    rows_written  BIGINT  NOT NULL DEFAULT 0,
    error_text    VARCHAR
);
```

`candle_coverage` is the thing that makes the whole pipeline resumable and idempotent, and it
is what the UI reads to draw "you have 2022-01-03 to 2024-11-28 for this contract". Keep the
scheduler's own job definitions and cron state in SQLite; keep this data derived table in DuckDB
next to the candles it describes.

## 4. Storage topology: one DuckDB file, Parquet as export

The user's own diagram already has this right. DuckDB is the store, CSV and Parquet are exports.
This section justifies that against the alternatives rather than just agreeing with it.

### Option A: one monolithic `.duckdb` file (recommended)

Pros, several of them measured:

- **Fast enough by a wide margin.** Single contract aggregate over a 5,000,000 row table:
  0.1 ms when sorted on the leading key. Full `count(*)`: 0.2 ms (metadata only).
- **Real transactions.** Delete plus insert for a re-download is atomic. A Parquet directory
  cannot give you that without a table format layer such as Iceberg or Delta, which is a large
  amount of machinery for a single user desktop application.
- **MVCC readers.** Verified: a reader cursor sees a consistent snapshot while the writer has an
  open transaction, and sees the new state immediately after commit. Charts never render a torn
  half written expiry.
- **Better compression than Parquet on this data.** 5,000,000 rows: 75.5 MB native against
  ~37.5 MB Parquet ZSTD at the default row group size for the same rows, but the native file
  also carries the catalog tables, zone maps, and free space for future writes. Per column the
  native encodings (ALP, BitPacking, RLE, Constant, measured via `pragma_storage_info`) are in
  the same class as Parquet's.
- **Zone maps beat Parquet footers for point-ish queries.** Measured single contract scan:
  0.4 ms native against 4.7 ms over a Parquet file of the same content. Parquet has to read and
  parse the footer and per row group statistics before it can prune.
- **One file to back up, one file to hand to a colleague.**

Cons:

- **Exactly one read-write process.** This is the real constraint and section 7 is entirely
  about designing around it.
- **The file never shrinks in place.** Section 10 handles this.

### Option B: Parquet lake, DuckDB as a pure query layer

Layout would be `data/candles/underlying=NIFTY/expiry=2025-03-27/res=1/part-*.parquet`, queried
with `read_parquet('data/candles/**/*.parquet', hive_partitioning=true)`.

Pros: many concurrent reader processes, trivially incremental (write a new file per download),
directly consumable by anything that speaks Parquet, and the "delete a partition" idempotency
story is a `shutil.rmtree` of a directory.

Cons that rule it out as the primary store here:

- **No transactions.** A crash mid write leaves a partial `.parquet` file that
  `read_parquet('**')` will happily pick up and fail on, or worse, silently include.
- **Small file problem.** One HTTP response is roughly 3000 candles. Writing one Parquet file per
  response across 500,000 contracts produces 500,000 files of ~40 KB. Directory listing alone
  becomes the dominant query cost, and on macOS with a large Time Machine or iCloud managed
  folder it gets worse.
- **10x slower on the query the UI actually runs.** Measured 4.7 ms against 0.4 ms.
- **The catalog still has to live somewhere transactional**, so you end up with DuckDB or SQLite
  anyway, and now with a consistency problem between the catalog and the files.
- **Compaction is manual and unsafe.** Merging small files into large ones while readers are
  scanning the directory has no atomic swap.

### Option C: hybrid, hot in DuckDB and cold in Parquet

Recent or actively researched expiries in the DuckDB file, older ones exported to Parquet and
attached as views, with a `UNION ALL` view spanning both.

This is the right pattern at true multi terabyte scale. It is the wrong pattern at 15 GB. It
doubles the write path, doubles the failure modes, requires a tier migration job, and makes
"delete and re-download this range" ambiguous about which tier owns the range. Revisit only if
the file exceeds roughly 200 GB or if multi machine concurrent access becomes a requirement.

### Recommendation

**Option A. One `expiry_manager.duckdb` file.** Ship a first class "Export" feature that writes
Parquet (ZSTD, optionally Hive partitioned by underlying and expiry) and CSV, and use those
exports as the interchange format for the user's other research tools and for backups. Keep the
export code path well factored so that Option C remains available later without a rewrite: the
query layer should already go through a single `candles` view name rather than the table name
directly.

```sql
-- Query through this, never through the base table, so a future hot/cold split is invisible.
CREATE OR REPLACE VIEW v_candles AS SELECT * FROM candles;
```

## 5. Ingestion throughput

### 5.1 Measured

Insert one batch into a nine column table, best of five runs, in memory, DuckDB 1.5.5.

Batch of **3,000 rows** (the realistic size of one Fyers `historical-data` response):

| Method | Best | Rows/s |
|---|---|---|
| `executemany("INSERT ... VALUES (?,...)", rows)` | 359.02 ms | 8,356 |
| `con.append("candles", pandas_df)` | 1.42 ms | 2,114,165 |
| `con.register("b", arrow_tbl)` then `INSERT INTO ... SELECT * FROM b` | 1.36 ms | 2,200,557 |
| Arrow replacement scan: local var `b`, `INSERT INTO ... SELECT * FROM b` | **0.83 ms** | **3,633,976** |
| Polars replacement scan | 0.84 ms | 3,575,153 |
| Pandas replacement scan | 1.04 ms | 2,897,618 |

Batch of **40,000 rows** (a full 100 day minute pull for an active contract):

| Method | Best | Rows/s |
|---|---|---|
| `executemany` | 4790.14 ms | 8,350 |
| `con.append(pandas_df)` | 5.24 ms | 7,629,645 |
| `register(arrow)` + `INSERT SELECT` | **3.56 ms** | **11,229,908** |
| Arrow replacement scan | 4.05 ms | 9,874,917 |
| Polars replacement scan | 4.12 ms | 9,697,362 |
| Pandas replacement scan | 3.58 ms | 11,169,805 |

Sustained, on disk, 300 sequential batches of 3,000 rows, one transaction per batch:
**0.87 s for 900,000 rows, 1,036,582 rows/s.**

### 5.2 Conclusions

- **`executemany` is a trap.** It is the obvious DB-API method and it is 430x slower at 3,000
  rows and 1,345x slower at 40,000 rows. It binds and executes row by row through the prepared
  statement path with no vectorisation. Ban it from the ingest path in code review.
- **Everything Arrow shaped is within noise of everything else Arrow shaped.** Arrow, Polars and
  pandas all land in the same 3 to 11 million rows per second band because DuckDB zero copies
  from the Arrow C data interface in all three cases. Pick on ergonomics, not speed.
- **Arrow is the right choice on ergonomics.** Building a `pa.Table` from the Fyers list of lists
  needs no pandas dependency in the ingest path, lets you pin exact column types (`int32`,
  `uint8`, `timestamp('us')`, `decimal128(9,2)`, `int64`) so DuckDB never has to guess or cast,
  and avoids pandas' habit of promoting an integer column with a null to float64.
- **`con.append()` only accepts a pandas DataFrame.** Verified: passing a `pa.Table` raises
  `TypeError: append(): incompatible function arguments`. The DuckDB C Appender API is not
  exposed to Python. So "use the Appender" is not actionable advice in Python; the Arrow path is
  the Python equivalent and is at least as fast.
- **`COPY FROM` a temp Parquet or CSV file is strictly worse here.** It adds a filesystem round
  trip and a serialise/parse cycle for data that is already in memory. Reserve `COPY FROM` for
  restoring an export or importing a third party dump.
- **One transaction per HTTP response is correct.** At 1 ms per insert and roughly 100 ms per
  HTTP call, transaction overhead is invisible. Do not batch multiple contracts into one
  transaction just to save commits: it makes a mid batch failure roll back work that succeeded,
  and it makes `candle_coverage` bookkeeping harder to keep atomic with the data.

### 5.3 Recommended ingest code

```python
import pyarrow as pa

IST_OFFSET_SECONDS = 19800

CANDLE_SCHEMA = pa.schema([
    ("contract_id", pa.int32()),
    ("res_id",      pa.uint8()),
    ("ts",          pa.timestamp("us")),
    ("open",        pa.decimal128(9, 2)),
    ("high",        pa.decimal128(9, 2)),
    ("low",         pa.decimal128(9, 2)),
    ("close",       pa.decimal128(9, 2)),
    ("volume",      pa.int64()),
    ("oi",          pa.int64()),
])


def candles_to_arrow(candles, contract_id, res_id, has_oi):
    """Fyers rows are [epoch_utc_seconds, o, h, l, c, volume, (open_interest)]."""
    n = len(candles)
    # Column extraction stays in Python but touches each value once. At 3000 rows
    # this is well under a millisecond and is not the bottleneck; the HTTP call is.
    ts = pa.array(
        [(row[0] + IST_OFFSET_SECONDS) * 1_000_000 for row in candles],
        type=pa.int64(),
    ).cast(pa.timestamp("us"))

    def dec(idx):
        # Decimal128 from float goes through a rounding cast. Fyers sends 2dp values,
        # so round explicitly rather than relying on the cast to do the right thing.
        return pa.array([round(row[idx], 2) for row in candles],
                        type=pa.float64()).cast(pa.decimal128(9, 2))

    return pa.table(
        {
            "contract_id": pa.array([contract_id] * n, pa.int32()),
            "res_id":      pa.array([res_id] * n, pa.uint8()),
            "ts":          ts,
            "open":  dec(1), "high": dec(2), "low": dec(3), "close": dec(4),
            "volume": pa.array([int(row[5]) for row in candles], pa.int64()),
            "oi": (pa.array([int(row[6]) for row in candles], pa.int64())
                   if has_oi else pa.nulls(n, pa.int64())),
        },
        schema=CANDLE_SCHEMA,
    )
```

Then, inside the writer (section 7), one atomic unit per response:

```python
def write_batch(cur, contract_id, res_id, range_from, range_to, batch, meta):
    cur.execute("BEGIN")
    try:
        cur.execute(
            "DELETE FROM candles "
            "WHERE contract_id = ? AND res_id = ? AND ts >= ? AND ts < ?",
            [contract_id, res_id, range_from, range_to_exclusive],
        )
        # batch is a local name, so DuckDB's Python replacement scan resolves it.
        cur.execute("INSERT INTO candles SELECT * FROM batch")
        cur.execute(
            "INSERT OR REPLACE INTO candle_coverage "
            "(contract_id, res_id, range_from, range_to, row_count, first_ts, last_ts, "
            " http_status, fyers_code, payload_sha256, include_oi) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            meta,
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
```

**Replacement scan gotcha, verified.** `con.register("name", table)` creates a view scoped to
that one connection. A `con.cursor()` child does **not** see it:
`CatalogException: Table with name reg does not exist`. The Python local variable replacement
scan **does** work from a cursor, because DuckDB inspects the calling Python frame. So either
register on the same cursor you execute on, or use a plain local variable as above. Do not
register on the parent and execute on a cursor.

`INSERT INTO t BY NAME SELECT * FROM x` works and tolerates column order mismatch. Prefer it if
the Arrow builder and the DDL might drift, though pinning `CANDLE_SCHEMA` makes that unnecessary.

## 6. Concurrency (the critical section)

### 6.1 Measured facts about DuckDB's locking model

All verified on DuckDB 1.5.5.

| Situation | Result |
|---|---|
| Process A holds a read-write connection, process B opens read-write | `IOException: Could not set lock on file ...: Conflicting lock` |
| Process A holds a read-write connection, process B opens **read-only** | `IOException: Could not set lock on file ...: Conflicting lock` |
| Process A holds read-only, process B opens read-only | **Both succeed** |
| Process A holds read-only, process B opens read-write | `IOException: Could not set lock on file` |
| Same process, `duckdb.connect(path)` called twice, both read-write | **Both succeed**, they share one cached database instance |
| Same process, read-write open, then `duckdb.connect(path, read_only=True)` | `ConnectionException: Can't open a connection to same database file with a different configuration than existing connections` |
| Same process, separate in memory instance tries `ATTACH '<path>' AS ro (READ_ONLY)` | `BinderException: Unique file handle conflict: Cannot attach ...` |
| `con.cursor()` on a read-write connection | Independent transaction context sharing the same instance |
| Writer inside an open transaction; sibling cursor reads | Sibling sees the **pre transaction** snapshot (100000), writer sees 100001 |
| After writer commits, sibling cursor reads again | Sees 100001 immediately |
| Two cursors update the same rows concurrently | `TransactionException: TransactionContext Error: Conflict on update!` on the second statement |

The important and slightly surprising one is row 2: **a read-only process cannot open the file
while a writer holds it.** The DuckDB file lock is a single lock with two modes, exclusive for
read-write and shared for read-only, and the two modes are mutually exclusive. Any design that
assumes "the API can open it read-only while the scheduler writes" is wrong and will fail at
runtime with an IOException, not degrade gracefully.

### 6.2 Therefore: one process, one database instance, one writer task

```
                 FastAPI process (uvicorn, single worker)
   +---------------------------------------------------------------+
   |                                                               |
   |   duckdb.connect("expiry_manager.duckdb")   <- ONE instance    |
   |            |                                                   |
   |            +-- writer cursor  (owned by one asyncio task,       |
   |            |                   fed by an asyncio.Queue)         |
   |            |                                                   |
   |            +-- reader cursor pool (N cursors, checked out by    |
   |                                    request handlers via         |
   |                                    run_in_threadpool)           |
   |                                                               |
   |   APScheduler jobs run IN THIS PROCESS and enqueue writes.     |
   +---------------------------------------------------------------+
```

Non negotiable consequences:

- **`uvicorn --workers 1`.** Two uvicorn workers are two OS processes and the second one will
  fail to open the database. If horizontal scaling is ever needed, put the DuckDB access behind
  an internal single process service, not behind more uvicorn workers.
- **The scheduler runs in process.** APScheduler's `AsyncIOScheduler` inside the FastAPI lifespan,
  not a separate `python scheduler.py`. A separate scheduler process is the single most likely
  way this design gets broken.
  If a separate process is truly required later, it must reach the database over HTTP against
  the FastAPI writer endpoint, never by opening the file.
- **Do not use `duckdb.connect(path, read_only=True)` anywhere in the app.** Verified to fail
  inside the same process once a read-write connection exists.

### 6.3 The writer

Every write goes through one `asyncio.Queue` consumed by one long lived task. This gives
serialisation for free (no locks to get wrong), natural backpressure, and a single place to put
retry, metrics and the `ingest_runs` bookkeeping.

```python
import asyncio
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool

class DuckStore:
    def __init__(self, path: str):
        self._con = duckdb.connect(path)
        self._con.execute("SET TimeZone='Asia/Kolkata'")
        self._con.execute("SET threads=6")
        self._con.execute("SET memory_limit='4GB'")
        self._con.execute("SET temp_directory='./data/duckdb_tmp'")
        self._con.execute("SET preserve_insertion_order=false")
        self._write_q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._writer_cur = None
        self._writer_task = None
        self._reader_sem = asyncio.Semaphore(6)   # bound reader threads

    async def start(self):
        self._writer_cur = self._con.cursor()
        self._writer_cur.execute("SET TimeZone='Asia/Kolkata'")
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def _writer_loop(self):
        while True:
            job = await self._write_q.get()
            if job is None:
                return
            fn, fut = job
            try:
                # DuckDB releases the GIL, so this must not run on the event loop thread.
                result = await run_in_threadpool(fn, self._writer_cur)
                fut.set_result(result)
            except Exception as exc:
                fut.set_exception(exc)
            finally:
                self._write_q.task_done()

    async def write(self, fn):
        """fn(cursor) -> Any. Runs serialised on the single writer cursor."""
        fut = asyncio.get_running_loop().create_future()
        await self._write_q.put((fn, fut))
        return await fut

    async def read(self, fn):
        """fn(cursor) -> Any. Runs on a fresh cursor in the thread pool."""
        async with self._reader_sem:
            def run():
                cur = self._con.cursor()
                try:
                    cur.execute("SET TimeZone='Asia/Kolkata'")
                    return fn(cur)
                finally:
                    cur.close()
            return await run_in_threadpool(run)

    async def close(self):
        await self._write_q.put(None)
        if self._writer_task:
            await self._writer_task
        await run_in_threadpool(lambda: self._con.execute("CHECKPOINT"))
        self._con.close()
```

Two details that matter:

- **Never call DuckDB directly from an `async def` handler.** A 200 ms scan on the event loop
  thread stalls every other request. Always go through `run_in_threadpool`, which is what the
  code above does. DuckDB releases the GIL during execution, so real parallelism is achieved.
- **`SET` is per connection, and a `cursor()` starts from the instance defaults.** Verified: the
  registered view test showed cursors have their own catalog scope. Apply `SET TimeZone` on
  every cursor, or set it once on the instance via the `config` dict passed to
  `duckdb.connect(path, config={"TimeZone": "Asia/Kolkata"})` so it becomes the instance default.

### 6.4 Reader performance, measured

Reader cursors on the shared instance, 20 single contract aggregates per thread against a
5,000,000 row table:

| Threads | Wall time | Queries/s |
|---|---|---|
| 1 | 10.6 ms | 1,882 |
| 4 | 8.1 ms | 9,937 |
| 8 | 14.7 ms | 10,889 |
| 6 readers while a writer commits delete plus insert in a loop | 12.3 ms | 9,718 |

The last row is the one that answers the question directly: **a busy writer does not block
readers.** Throughput with a concurrently committing writer (9,718 q/s) was statistically
indistinguishable from the same six readers with no writer. DuckDB's MVCC gives readers a
consistent snapshot without taking a lock the writer needs.

Set `threads` deliberately. With `SET threads=10` a single analytical query will use all ten
cores and starve concurrent short queries. For a UI serving many small chart queries plus one
background ingest, `SET threads=4` to `6` on a 10 core machine gives better tail latency.

### 6.5 Cursor pooling

`con.cursor()` is cheap (it allocates a client context, not a file handle) so a strict pool is
optional. A `Semaphore` bounding concurrent readers, as above, is enough and avoids the classic
pool bug of leaking a cursor on an exception path. If you do build a pool, cursors must never be
shared across concurrent tasks, because a cursor holds the result of the last `execute` and a
second `execute` on the same cursor discards it.

### 6.6 What about write conflicts

Verified: two cursors updating overlapping rows produce
`TransactionException: TransactionContext Error: Conflict on update!` on the second statement,
and DuckDB does **not** retry. The single writer task design makes this structurally impossible,
which is exactly why it is the recommendation rather than "take a lock and hope".

If a future refactor introduces a second writer, it must catch `duckdb.TransactionException` and
retry the whole transaction. Do not do this. Use the queue.

## 7. Idempotency and dedup

### 7.1 The requirement

Re-downloading a range for a contract must converge to the same state, whether the previous
attempt succeeded, partially succeeded, or crashed. Fyers can also correct data, so a
re-download must be able to *change* existing rows, not only fill gaps.

### 7.2 Options, measured on a 5,000,000 row table replacing 10,000 rows

| Strategy | Time | Notes |
|---|---|---|
| `MERGE INTO ... ON ... WHEN MATCHED UPDATE WHEN NOT MATCHED INSERT` | 11 ms | Works without any index. Cost is a join against the target, so it grows with total table size. |
| `INSERT ... ON CONFLICT` | not usable | `BinderException: There are no UNIQUE/PRIMARY KEY constraints that refer to this table`. Requires the index we rejected in section 1.5. |
| `DELETE` by range then `INSERT` in one transaction | **3 ms** | Cost is a zone map pruned delete plus an append. Bounded by the size of the range, not the table. |
| `PRIMARY KEY (contract_id, ts)` for dedup | rejected | 4.5x file size, 4.5x slower load. |

`MERGE INTO` is genuinely supported in DuckDB 1.5.5, both the `USING (col, col)` shorthand and
the explicit `ON` form, and both work with no unique index. It is the right tool for a
row-level upsert where you do not know the affected key range. It is the wrong tool here,
because we always know the range: the download request itself defines `[range_from, range_to]`.

### 7.3 Recommended: delete then insert per contract, resolution and range

```sql
BEGIN;
DELETE FROM candles
 WHERE contract_id = $contract_id
   AND res_id      = $res_id
   AND ts >= $range_from_ist        -- inclusive, 00:00:00 of range_from
   AND ts <  $range_to_exclusive;   -- exclusive, 00:00:00 of range_to + 1 day
INSERT INTO candles SELECT * FROM batch;
INSERT OR REPLACE INTO candle_coverage VALUES (...);
COMMIT;
```

Why this is correct and safe:

- **Atomic.** Verified MVCC behaviour means a reader either sees the entire old range or the
  entire new range, never a half deleted one.
- **Converges.** Running it twice produces the same result. Running it after a crash produces
  the same result. There is no "did the previous run insert 1,700 of the 3,000 rows" state.
- **Handles corrections and shrinkage.** If Fyers now returns 2,900 candles where it previously
  returned 3,000, the extra 100 are removed. A `MERGE` would leave them behind, silently.
- **Bounded cost.** The `DELETE` predicate leads with `contract_id`, which is the leading sort
  key, so zone maps prune to a handful of row groups. Measured 3 ms against 5,000,000 rows and
  it does not degrade as the table grows.
- **Aligns with the API.** The Fyers request is defined by
  `(symbol, resolution, range_from, range_to)`. The delete predicate is literally the request.

Range boundary rule: the delete window must exactly match the requested window, using IST day
boundaries, half open on the right. Fyers `range_to` is inclusive of that date, so
`range_to_exclusive = range_to + 1 day` at `00:00:00`. Getting this off by one leaves a
duplicated or missing day at the seam between two 100 day chunks, which is the classic bug in
chunked backfills.

### 7.4 Guarding against duplicates anyway

No unique constraint means nothing physically prevents duplicates if a bug slips through. Add a
cheap assertion the scheduler runs after each ingest run, and expose it in the UI as a data
health check:

```sql
-- Should always return zero rows.
SELECT contract_id, res_id, ts, count(*) AS n
  FROM candles
 GROUP BY 1, 2, 3
HAVING count(*) > 1
 LIMIT 100;
```

Scope it to the contracts touched by the run rather than the whole table for routine checks, and
run the unscoped version as a nightly maintenance job.

`payload_sha256` in `candle_coverage` lets the scheduler skip the write entirely when a
re-download returns byte identical content, which is the common case for a settled expiry. That
saves both the delete and the insert, and more importantly avoids creating new row versions that
fragment the file.

## 8. Compression, sort keys, zone maps and row groups

### 8.1 How DuckDB actually stores this

Confirmed with `pragma_storage_info('candles')` on the recommended schema:

| Column | Compression chosen |
|---|---|
| `contract_id` | RLE, Constant |
| `res_id` | Constant |
| `ts` | BitPacking, Constant |
| `open`, `high`, `low`, `close` (DECIMAL(9,2)) | BitPacking, Constant |
| `open`, `high`, `low`, `close` (DOUBLE) | ALP, Constant |
| `open`, `high`, `low`, `close` (FLOAT) | **ALPRD**, Constant |
| `volume`, `oi` | BitPacking, Constant |

`Constant` appears wherever a whole segment holds one value, which is exactly what the
`(contract_id, res_id, ts)` sort order produces for `res_id` and often for `contract_id`. This
is compression the sort key buys for free.

There is no `COMPRESSION` clause to set. DuckDB picks per column per row group by trying the
candidates and keeping the smallest. `force_compression` and `disabled_compression_methods`
exist but are debugging tools. Leave them alone.

### 8.2 Sort key and zone maps

DuckDB keeps a min/max zone map per column per row group (122,880 rows). It has no secondary
indexes for range scans, so **physical order is the only index that matters.**

Measured, 5,000,000 rows, 500 contracts:

| Query | Sorted by (contract_id, ts) | Inserted in ts order |
|---|---|---|
| `WHERE contract_id = 317` | **0.1 ms** | 2.6 ms |
| `WHERE contract_id = 317 AND ts BETWEEN ...` | **0.1 ms** | 0.6 ms |
| `WHERE ts BETWEEN ...` (all contracts) | 1.7 ms | **0.1 ms** |

The tradeoff is explicit: sorting by contract makes per contract queries 26x faster and cross
sectional time slice queries 17x slower.

**Sort by `(contract_id, res_id, ts)`** because the workload is overwhelmingly per contract:
draw a chart for one option, backtest one strategy leg, export one contract. The cross sectional
"what did every strike do at 09:20 on this day" query is a real phase 2 need, but the contract
ids for one expiry are contiguous (section 3.2), so it becomes
`WHERE contract_id BETWEEN lo AND hi AND ts BETWEEN ...`, which prunes on the leading key and
gets the fast path anyway. That contiguity is the whole reason to allocate `contract_id` per
expiry batch.

Physical order is achieved at write time, not by a `CREATE INDEX`:

- Inserts arrive one contract at a time, so they are already grouped by `contract_id` and
  ascending in `ts` within a contract. Natural ordering is close to ideal.
- `SET preserve_insertion_order=false` lets DuckDB reorder within a query for speed; it does not
  reorder committed data.
- The delete then insert cycle appends the new rows at the end of the table, so after many
  re-downloads a contract's rows are physically scattered. This is what the periodic compaction
  in section 10 fixes, by rewriting in full sort order.

Do **not** create an ART index on `(contract_id, ts)` hoping to help range scans. ART is a point
lookup structure. Measured point lookup with the index: 0.41 ms. Without any index: 0.40 ms.
The index gave nothing and cost 4.5x the file size.

### 8.3 Row groups

Native storage row group size is 122,880 rows and is not tunable per table. What is tunable and
worth setting on the `ATTACH` for a fresh database:

```sql
ATTACH 'expiry_manager.duckdb' AS db (BLOCK_SIZE 262144);
```

262144 (the default) is right for this workload. The smaller 16384 block size exists for
databases with many tiny tables and would hurt here.

For Parquet exports, row group size **is** tunable and matters:

| Row group size | Effect |
|---|---|
| 122,880 (DuckDB default) | Finest min/max pruning, most metadata and dictionary overhead |
| 1,000,000 | Better compression ratio and less footer parsing, coarser pruning |

Measured file sizes at the two settings differed by a factor that is an artefact of the
synthetic generator, so do not quote a ratio. The structural point stands: larger row groups
amortise the per row group dictionary and statistics, smaller ones prune better. **Use 122,880
for exports intended to be queried, and 1,000,000 for exports intended as archives.**

### 8.4 Nulls

`oi` is the only nullable column. The validity mask costs one bit per row and compresses to a
constant when a segment has no nulls. Measured: nullable and NOT NULL variants of the same table
were byte identical at 76.0 MB. So declare `NOT NULL` where it is true for correctness value, not
for size.

## 9. Maintenance: checkpoint, WAL, vacuum, file growth

### 9.1 What was measured

- Sustained ingest of 900,000 rows in 300 transactions left `db=32.8 MB` plus `wal=4.40 MB`.
  After `CHECKPOINT`: `db=48.2 MB`, `wal=0.00 MB`.
- Rewriting the same contract 10 times (delete plus insert): file went 75.5 MB to 77.6 MB and
  then **stayed at 77.6 MB**. It never came back down.
- `VACUUM` then `CHECKPOINT`: still 77.6 MB. **`VACUUM` does not reclaim space in DuckDB.**
- `ATTACH` a new file plus `CREATE TABLE AS SELECT ... ORDER BY contract_id, res_id, ts`:
  48.2 MB became 34.1 MB in 0.06 s (29 percent reclaimed), and 77.6 MB became 75.2 MB.

### 9.2 The rules

**CHECKPOINT.** DuckDB writes committed transactions to a `.wal` sidecar and folds it into the
main file at a checkpoint. Automatic checkpoint fires at `checkpoint_threshold` (16 MiB of WAL by
default) and on clean shutdown. Explicitly:

- `CHECKPOINT` after each scheduler run finishes, so a crash never costs more than one run.
- `CHECKPOINT` in the FastAPI lifespan shutdown before `con.close()`.
- Consider raising `checkpoint_threshold` to `'256MB'` during a large backfill so the writer is
  not interrupted, then checkpointing once at the end. Measure before doing this; at 1 million
  rows per second the checkpoints were not visible in the throughput number anyway.
- `FORCE CHECKPOINT` additionally aborts other transactions to guarantee the checkpoint happens.
  Only use it during controlled maintenance, never in a request handler.

**The WAL is not a backup.** Do not copy `expiry_manager.duckdb` without also copying
`expiry_manager.duckdb.wal`, or you will restore a file that is missing the most recent writes.
Better: `CHECKPOINT` first, confirm the `.wal` is gone or zero length, then copy the single file.

**VACUUM is not what you think.** In DuckDB, `VACUUM` recomputes statistics. It does not compact
the file and it does not return space to the filesystem. This was measured directly. There is no
in place compaction command.

**The file only grows.** Freed blocks are tracked and reused by later writes, so the file grows
sub-linearly with churn, but the high water mark is permanent for the life of that file. Given
the ingest pattern (append heavy, occasional range rewrites), growth is modest. The measured 10x
rewrite cycle added 2.1 MB to a 75.5 MB file and then plateaued as blocks were reused.

**Compaction is a full rewrite into a new file.** This is the only way to shrink, and it doubles
as the way to restore sort order after churn:

```python
def compact(store_path: str) -> str:
    tmp = store_path + ".compacting"
    con = duckdb.connect(store_path)          # the single writer instance
    con.execute("CHECKPOINT")
    con.execute(f"ATTACH '{tmp}' AS newdb")
    con.execute("CREATE TABLE newdb.candles AS "
                "SELECT * FROM candles ORDER BY contract_id, res_id, ts")
    # repeat for every catalog table, then copy sequences forward
    con.execute("CHECKPOINT newdb")
    con.execute("DETACH newdb")
    return tmp   # caller stops the app, swaps the files, restarts
```

Because a swap requires closing the live file, expose this as an explicit "Optimise database"
maintenance action in the UI that quiesces the writer queue, runs the rewrite, closes, renames,
and reopens. Do not schedule it silently. Guard it: require free disk space greater than the
current file size, write to a temp name, `fsync`, and only then rename over the original.

Trigger heuristic for suggesting it: track `sum(row_count)` from `candle_coverage` against
`SELECT count(*) FROM candles`, and file size against `count(*) * 15.21 bytes`. When the file is
more than about 1.4x the modelled size, suggest compaction.

### 9.3 Settings to pin explicitly

```python
duckdb.connect(path, config={
    "TimeZone": "Asia/Kolkata",
    "threads": "6",                 # leave cores for FastAPI and the OS
    "memory_limit": "4GB",          # default is 80% of RAM, too greedy for a desktop app
    "temp_directory": "./data/duckdb_tmp",   # default '.tmp' relative to cwd, which is fragile
    "preserve_insertion_order": "false",     # lets large scans and exports parallelise freely
    "checkpoint_threshold": "64MB",
})
```

`temp_directory` matters: the default is `.tmp` relative to the process working directory, so a
large export or sort spilling to disk lands wherever uvicorn happened to be started. Pin it
inside the app's data directory and make sure it is on a volume with room.

## 10. Exporting to CSV and Parquet

### 10.1 Measured, 5,000,000 rows

| Export | Time | Size |
|---|---|---|
| Parquet, ZSTD, row group 122,880 | 0.38 s | 37.5 MB |
| Parquet, SNAPPY, row group 122,880 | 0.30 s | 75.7 MB |
| CSV, plain | 0.37 s | 324.3 MB |
| CSV, GZIP | **8.60 s** | 78.4 MB |
| Parquet, ZSTD, Hive partitioned | 0.22 s | 38.7 MB |

ZSTD Parquet is half the size of Snappy for a 27 percent time cost, and it is 8.6x smaller than
plain CSV. Gzipped CSV is the worst of all worlds: 23x slower than Parquet, twice the size, and
not directly queryable.

### 10.2 Recommended export SQL

Single Parquet file, for a contract or an expiry:

```sql
COPY (
    SELECT c.fyers_symbol,
           k.ts,
           k.open, k.high, k.low, k.close, k.volume, k.oi,
           c.expiry_date, c.strike, c.option_type, c.instrument_type,
           u.short_name AS underlying
      FROM candles k
      JOIN contracts   c ON c.contract_id  = k.contract_id
      JOIN underlyings u ON u.underlying_id = c.underlying_id
      JOIN resolutions r ON r.res_id       = k.res_id
     WHERE c.underlying_id = $underlying_id
       AND c.expiry_date   = $expiry_date
       AND k.res_id        = $res_id
     ORDER BY k.contract_id, k.ts
) TO 'exports/NIFTY_2025-03-27_1min.parquet'
  (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880);
```

Denormalising the catalog columns into the export is right: the export leaves the system and has
to stand alone. Keeping `candles` narrow is about the store, not the interchange format.

Hive partitioned archive export, for the whole database or one underlying:

```sql
COPY (
    SELECT k.*, u.short_name AS underlying, c.expiry_date
      FROM candles k
      JOIN contracts c ON c.contract_id = k.contract_id
      JOIN underlyings u ON u.underlying_id = c.underlying_id
) TO 'exports/lake'
  (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 1000000,
   PARTITION_BY (underlying, expiry_date),
   OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'part_{uuid}');
```

Read it back, for verification or for a future hot/cold split:

```sql
SELECT * FROM read_parquet('exports/lake/**/*.parquet', hive_partitioning = true)
 WHERE underlying = 'NIFTY' AND expiry_date = DATE '2025-03-27';
```

CSV export, with an explicit timestamp format so Excel and the user's other tools do not guess:

```sql
COPY (
    SELECT c.fyers_symbol AS symbol,
           strftime(k.ts, '%Y-%m-%d %H:%M:%S') AS timestamp_ist,
           epoch(k.ts) - 19800 AS epoch_utc,
           k.open, k.high, k.low, k.close, k.volume, k.oi
      FROM candles k JOIN contracts c USING (contract_id)
     WHERE k.contract_id = $contract_id AND k.res_id = $res_id
     ORDER BY k.ts
) TO 'exports/NSE_NIFTY25MAR23000CE_1min.csv'
  (FORMAT CSV, HEADER, DELIMITER ',', DATEFORMAT '%Y-%m-%d');
```

Emitting both a human readable IST string and the raw UTC epoch removes all ambiguity for
downstream consumers, at negligible cost.

### 10.3 Export mechanics in the service

Exports are reads, so they go through the reader path, not the writer queue. But they are long
reads: a full database Parquet export of 600 million rows will take tens of seconds and will use
`threads` worth of cores. Run them as a tracked background job with progress, write to a temp
name and rename on success, and cap concurrent exports at one.

Never stream a `COPY TO` result through the FastAPI response directly. Write the file, then serve
it, so a client disconnect does not abort a half written export.

## 11. Query patterns the rest of the system will use

Chart data for one contract, which is the hot path for openalgo-charts:

```sql
SELECT epoch(ts) - 19800 AS time,     -- lightweight-charts style UTCTimestamp
       open, high, low, close, volume, oi
  FROM candles
 WHERE contract_id = ? AND res_id = ?
   AND ts >= ? AND ts < ?
 ORDER BY ts;
```

Leading key predicate, zone map pruned, measured at 0.1 ms class on 5,000,000 rows.

Whole chain for one expiry at one timestamp, the phase 2 backtester's inner loop:

```sql
SELECT c.strike, c.option_type, k.close, k.oi, k.volume
  FROM candles k
  JOIN contracts c USING (contract_id)
 WHERE k.contract_id BETWEEN ? AND ?   -- contiguous ids for this expiry
   AND k.res_id = ?
   AND k.ts = ?
 ORDER BY c.strike, c.option_type;
```

Coverage for the UI, so the user can see what they already have before downloading:

```sql
SELECT c.fyers_symbol, c.strike, c.option_type,
       min(cov.range_from) AS have_from,
       max(cov.range_to)   AS have_to,
       sum(cov.row_count)  AS rows
  FROM candle_coverage cov
  JOIN contracts c USING (contract_id)
 WHERE c.underlying_id = ? AND c.expiry_date = ? AND cov.res_id = ?
 GROUP BY 1, 2, 3
 ORDER BY c.strike, c.option_type;
```

This reads only the small bookkeeping table, so it is instant regardless of how large `candles`
grows. That is the reason `candle_coverage` exists rather than deriving coverage with
`min(ts), max(ts)` over `candles`.

For Phase 2, expose results as Arrow rather than pandas where the consumer can take it:
`cur.execute(sql).arrow()` and `.fetch_record_batch(n)` avoid a pandas materialisation and let a
vectorised backtester stream row groups. `.pl()` returns Polars directly. All three were verified
working, including `DECIMAL(9,2)` round tripping as `decimal128(9,2)` in Arrow, `Decimal` in
Polars, and `float64` in pandas.

## 12. Pitfalls checklist

Things that will bite, each one verified in this session unless noted.

1. `executemany` for candle inserts. 1200x slower. Ban it in review.
2. A second process (a separate scheduler, a second uvicorn worker, a `duckdb` CLI session left
   open, a DBeaver connection) opening the file. Fails with `IOException: Could not set lock`.
   Even read-only fails while a writer holds it.
3. `duckdb.connect(path, read_only=True)` in the same process as the writer. Fails with
   `ConnectionException: ... different configuration`.
4. `ATTACH '<the live file>' AS x (READ_ONLY)` from a second in memory instance in the same
   process. Fails with `BinderException: Unique file handle conflict`.
5. `con.register(name, obj)` on the parent connection then executing on a `cursor()`. The cursor
   cannot see the registered view. Register on the cursor, or use a Python local variable and let
   the replacement scan find it.
6. `con.append()` with a `pa.Table`. Only pandas DataFrames are accepted.
7. Adding `PRIMARY KEY` or `UNIQUE` to `candles` "for safety". 4.5x file size, 4.5x load time.
8. `FLOAT` for prices. Larger than `DOUBLE` because ALP degrades to ALPRD.
9. Assuming `VACUUM` shrinks the file. It does not. Only a full rewrite does.
10. Backing up `expiry_manager.duckdb` without the `.wal`, or without checkpointing first.
11. Relying on the session `TimeZone`. It defaults to the host OS zone and silently changes the
    meaning of `to_timestamp` and every `TIMESTAMPTZ` literal.
12. Off by one at the seam between two 100 day download chunks. Use half open IST day windows and
    make the delete predicate exactly match the request window.
13. Calling DuckDB from an `async def` handler without `run_in_threadpool`. Blocks the event loop.
14. Leaving `temp_directory` at the default `.tmp`, which is relative to the process working
    directory.
15. Leaving `memory_limit` at the default 80 percent of RAM in a desktop app that also runs a
    Vite dev server and a browser.
16. `SET threads=10` on a 10 core box, then wondering why one export makes the UI unresponsive.
17. Using `DECIMAL(9,2)` for a currency derivative underlying without a guard. It truncates the
    4 decimal quotes silently. Allowlist segments at ingest.
18. Reusing a single cursor across concurrent tasks. The second `execute` discards the first
    result set.

## Appendix: benchmark method

All benchmarks ran on this machine, Apple Silicon, macOS 25.2.0, DuckDB 1.5.5, Python 3.14.3,
in an isolated `uv` virtual environment. Scripts lived under the session scratchpad.

- **Insert methods.** Nine column in memory table, best of five runs per method, table truncated
  between runs. Batches of 3,000 and 40,000 rows built once outside the timing loop, so the
  numbers measure the insert path only, not Arrow construction.
- **Storage size.** 500 contracts x 10,000 bars = 5,000,000 rows, generated in SQL with
  `hash()` to make prices multiples of 0.05 in the 0 to 1000 range, which matches the NSE and
  BSE tick. `CHECKPOINT` before measuring `os.path.getsize`. Note that a synthetic generator
  produces more regular data than a real market, so absolute compression ratios are optimistic;
  the *relative* ordering of the type choices is the reliable output.
- **Query timing.** Best of seven, `read_only=True` connection, `PRAGMA threads=4`, full
  `fetchall()` so lazy evaluation is not being measured.
- **Concurrency.** `subprocess` for cross process lock tests, `threading.Thread` with
  `con.cursor()` for in process tests, a busy writer thread running delete plus insert in a loop
  for the reader-under-write test.
- **Sustained ingest.** 300 sequential Arrow batches of 3,000 rows into an on disk file, one
  implicit transaction per `execute`, WAL and file size sampled before and after `CHECKPOINT`.
- **Compaction.** `ATTACH` a second file, `CREATE TABLE AS SELECT ... ORDER BY`, `CHECKPOINT`
  the new database, compare `os.path.getsize`.

Raw figures are reproduced inline in each section above rather than dumped here, so that each
number sits next to the decision it supports.
