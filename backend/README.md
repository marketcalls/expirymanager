# ExpiryManager backend

FastAPI, SQLAlchemy over SQLite, DuckDB over Arrow. One OS process, one uvicorn worker, loopback
only.

## Running

```
uv sync
uv run expirymanager                 # https://127.0.0.1:8000
uv run expirymanager --reload        # development
uv run expirymanager --check         # prepare the data directory and exit
uv run pytest
```

`../scripts/dev-backend.sh` does the sync and the reload run in one step.

The host, the port and the https scheme are fixed. The Fyers app is registered with the redirect
URI `https://127.0.0.1:8000/fyers/callback` and Fyers matches it exactly, so none of the three is
configurable. The server generates its own self-signed certificate on first run and prints a
plain-text notice that the browser will warn once.

## Configuration

There is no `.env` and there are no environment variables for application settings. Every tunable
is a row in the SQLite `settings` table with a code default in `settings_store.py`, readable and
writable through `GET` and `PATCH /api/v1/system/settings`.

The one environment variable the process reads is `EXPIRYMANAGER_HOME`, which relocates the data
directory itself. It exists because the location of the settings store cannot be read out of the
settings store, and because the cloud sync refusal below otherwise has no remedy.

## The data directory

```
~/.expirymanager/          0700
  config.sqlite3           0600  (plus -wal and -shm)
  market.duckdb            0600  (plus .wal)
  master.key               0600, refused if any group or other bit is set
  expirymanager.lock       0600, advisory single-instance lock
  tls/server.key           0600
  tls/server.crt           0600
  exports/  raw/  logs/  tmp/  backups/    all 0700
```

`os.umask(0o077)` is the first executable statement of `__main__.py`. This is load bearing:
SQLite creates the `-wal` and `-shm` sidecars itself and DuckDB creates its `.wal`, so a `chmod`
applied after the database is opened leaves those files world readable and never fixes them.

Startup refuses to run if the data directory resolves under a known cloud sync root (iCloud,
Dropbox, OneDrive, Google Drive, and the macOS `Library/CloudStorage` provider layout). A sync
client copying a write-ahead log out from under two open databases corrupts both, and it uploads
the key file while it is doing it.

Startup also takes an advisory `flock` on `expirymanager.lock`. A second instance is refused with
a message naming the likely causes, because DuckDB reports the same situation as an `IOException`
that reads like corruption.

## Module layout

`expirymanager/` mirrors ARCHITECTURE.md section 7. The scaffold owns:

| File | Purpose |
|---|---|
| `version.py` | The single version string, written into DuckDB `meta` and sent as User-Agent. |
| `paths.py` | Data directory resolution, 0700 creation, cloud sync refusal, advisory lock, TLS seam. |
| `settings_store.py` | Typed read-through accessors over the `settings` table. The replacement for `.env`. |
| `logging_setup.py` | JSON logging plus the redaction filters and the OAuth callback scrubber. |
| `__main__.py` | umask, data directory, logging, TLS, lock, then `uvicorn.run`. |

## Interfaces other work items code against

`paths.ensure(root=None, ensure_tls=True) -> Paths`, and `Paths` exposes `root`, `sqlite_db`,
`duckdb_file`, `master_key`, `lock_file`, `tls_dir`, `tls_key`, `tls_cert`, `exports_dir`,
`raw_dir`, `raw_day_dir(y, m, d)`, `logs_dir`, `log_file`, `tmp_dir` and `backups_dir`.

`paths.ensure_tls_material(paths) -> tuple[Path, Path] | None` is the seam onto W04's
`security/tls.py`. It calls `tls.ensure_tls_material(paths)` when that function exists, falls
back to `tls.ensure_certificate(key_path, cert_path)`, and returns `None` when neither the module
nor the files are present, so the process starts on plain HTTP rather than failing.

`SettingsStore(engine)` gives `get`, `get_int`, `get_float`, `get_bool`, `get_str`, `get_list`,
`all`, `describe`, `set`, `set_many`, `reset` and `invalidate`. It speaks plain SQL, so it does
not depend on the declarative models and can be used during bootstrap. An engine of `None` makes
it answer entirely from code defaults. Unknown keys raise `UnknownSettingError` (API code
`unknown_setting`); a failed validator raises `SettingOutOfRangeError` (`setting_out_of_range`).

`configure_logging(log_file=..., level=..., json_console=...)` installs the handlers and attaches
the redaction filters to the handlers rather than to the loggers, which is the only placement
that also covers records propagated from `uvicorn` and `httpx`.

## Secrets

No credential, token, key or passcode is ever written to a file in this repository, to a fixture,
to a log line or to an error message. Tests use synthetic values only.
