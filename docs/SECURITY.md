# ExpiryManager Security Implementation

The product decision is zero configuration: there is no .env file anywhere in this repository and
no environment variable is read at runtime. The Fyers app id, app secret and redirect URI are
entered through the UI, and the app secret is stored encrypted in SQLite. That raises the bar rather
than lowering it, because the key has to live somewhere the app can reach unattended after a
reboot, and that question is answered explicitly below rather than avoided.

Pinned versions, verified on PyPI 2026-09-09: `cryptography` 50.0.1, `argon2-cffi` 25.1.0,
`limits` 5.8.0, `secure` 2.0.1, `keyring` 25.7.0 (optional provider only), `httpx` 0.28.1,
`fastapi` 0.141.1, `starlette` 1.6.0, `uvicorn` 0.52.4.

---

## 1. Threat model, stated plainly

| Id | Threat | Covered |
|---|---|---|
| T1 | Someone reads `config.sqlite3` alone (a backup, a copied file, a synced folder) | Yes. Every secret field is AEAD encrypted and the key is not in the database. |
| T2 | Someone copies the whole `~/.expirymanager` directory | Partially. They get the key file too. Mitigated only by filesystem permissions and full disk encryption. |
| T3 | A process running as the same user | No. This is stated in the UI rather than implied away. Nothing in-process defends against code running as you. |
| T4 | A malicious web page in the user's browser reaching the loopback API | Yes. TrustedHost against DNS rebinding, `Sec-Fetch-Site`, Origin allowlist, CSRF synchronizer token, session cookie. |
| T5 | An attacker with write access to the database relocating a ciphertext | Yes. AAD binds every ciphertext to its table, column, row id and key version. |
| T6 | Secrets leaking through logs, error responses, browser history or the access log | Yes. Redaction filter, correlation-id-only 500s, 303 off the OAuth callback URL, an explicit never-log list. |
| T7 | Brute force against the local login | Yes. Argon2id, per-(ip, username) rate limit, account lockout, identical failure body and timing. |
| T8 | The app running on a shared machine or a VPS | No. Out of scope for 1.0. The app binds loopback only. |
| T9 | A passive observer on the local machine reading loopback traffic | Yes, in the weak sense that transport is TLS. The certificate is self-signed and the trust decision is the user's, so this is not a strong control; it exists because the registered Fyers redirect URI requires https. |

---

## 2. Credential encryption

### 2.1 What is encrypted

Only true secrets. Everything else stays plaintext so it remains queryable and greppable.

| Table.column | Encrypted | Why |
|---|---|---|
| `broker_credential.app_secret_enc` | yes | It is the secret half of the app identity. |
| `broker_token.access_token_enc` | yes | A live bearer credential. |
| `broker_token.refresh_token_enc` | yes | A live bearer credential returned by the login response. |
| `broker_credential.app_id` | no | Not secret. It appears in the authorize URL. |
| `broker_credential.redirect_uri` | no | Not secret. |
| `broker_token.token_fingerprint` | no | sha256 of the access token. Safe to log, safe to store on coverage rows for provenance. |

### 2.2 Algorithm and envelope

AES-256-GCM through `cryptography.hazmat.primitives.ciphers.aead.AESGCM`. 32 byte key, 12 byte
nonce from `os.urandom`, 16 byte tag.

Stored as a SQLite `BLOB`:

```
magic     3 bytes    b"EM1"
key_ver   1 byte     uint8, which DEK version encrypted this
nonce    12 bytes
ct+tag    n bytes    AESGCM.encrypt() output
```

Minimum length 32. Anything shorter, or with the wrong magic, is rejected before any crypto runs.
`key_ver` is what makes online DEK rotation possible: during a rotation both DEKs are loaded,
decrypt dispatches on `key_ver`, and encrypt always uses the newest.

Fernet is explicitly rejected: it has no AAD parameter, it is AES-128-CBC plus HMAC rather than an
AEAD, and it embeds a timestamp. Without AAD it cannot defend T5.

### 2.3 AAD

```python
def aad(table: str, column: str, row_id: str, key_ver: int) -> bytes:
    # Bound to location so an attacker with database write access cannot move a ciphertext
    # from one row or column to another and have it still decrypt.
    return f"expirymanager|v1|{table}|{column}|{row_id}|{key_ver}".encode("utf-8")
```

**Consequence for the schema.** The AAD needs `row_id` before the INSERT, so no table holding an
encrypted column may use `INTEGER PRIMARY KEY AUTOINCREMENT`. `broker_credential` and
`broker_token` use TEXT primary keys holding a Python-generated `uuid4`, so the id exists before
encryption and there is never an insert-then-update window.

### 2.4 Key hierarchy

```
KEK (32 bytes)   from a pluggable provider, never stored in SQLite
   wraps
DEK (32 bytes)   random, generated once at first run
   encrypts
field ciphertexts in SQLite
```

The wrapped DEK lives in `crypto_key.wrapped_dek`, itself an `EM1` envelope with the constant AAD
`b"expirymanager|kek-wrap|v1"`.

Why two levels rather than one: switching the KEK provider rewraps one 32 byte DEK and never
re-encrypts a single field. That turns "start on the easy provider and upgrade later" into a real,
reversible product feature rather than a migration project, and it means the hard question (where
does the master key live) is a swappable component rather than a schema decision.

Rotating the KEK is also a rewrap. Rotating the DEK is the expensive path (decrypt and re-encrypt
every secret field) and is only needed after a suspected compromise; it stays possible because
`key_ver` is in the envelope.

### 2.5 Where the KEK lives

The hard constraint that decides this: **the scheduler must run unattended after a reboot.**

**Primary provider: a generated key file at `~/.expirymanager/master.key`.**

```python
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, os.urandom(32))
os.fsync(fd)
os.close(fd)
dir_fd = os.open(str(path.parent), os.O_RDONLY)
os.fsync(dir_fd)          # the directory entry must be durable too
os.close(dir_fd)
```

Startup refuses to run if `st_mode & 0o077` is non zero, the way sshd refuses a loose private key.
The parent directory is 0700.

It is chosen because it is the only candidate that satisfies both zero configuration and
unattended-after-reboot:

- The macOS Data Protection Keychain is only reachable inside a user session. Apple states that
  processes outside a user context must use the file-based keychain, and a LaunchAgent only starts
  after the user logs in, so a 03:00 reboot to the login window would run no scheduler.
- On macOS the keyring's advantage over a 0600 file is also smaller than it looks: the Keychain
  item's ACL is granted to the Python interpreter, so once stored, any Python script the user runs
  from any virtualenv reads it back with no prompt. Keyring mode must therefore never be described
  as protecting against local processes.
- A passphrase provider kills unattended restart by construction.

**Opt-in provider: OS keyring** (`keyring` 25.7.0). Offered in Settings with its threat model
stated in the UI text.

**Opt-in provider: passphrase.** Uses `cryptography.hazmat.primitives.kdf.argon2.Argon2id` with
`iterations=3, lanes=4, memory_cost=262144` (256 MiB), `length=32` and a 16 byte stored salt. Note
the parameter names differ from argon2-cffi (`iterations`/`lanes` rather than
`time_cost`/`parallelism`). The UI states plainly that this mode means the scheduler does not run
after a reboot until the passphrase is entered. If a "remember passphrase" option is ever offered
it must say that it reduces the mode to exactly the key file mode with extra steps.

**SQLCipher is rejected.** The secret fields are already AEAD encrypted with a key outside the
database; SQLCipher still needs a key at open time so it relocates rather than solves the key
storage problem; `sqlcipher3-binary` needs a custom SQLite build that threatens the zero-config
install on Python 3.14; and it does nothing against T3. FileVault covers the powered-off laptop.

**DuckDB encryption at rest is rejected.** DuckDB 1.4.0 added AES-256-GCM via
`ATTACH ... (ENCRYPTION_KEY '...')`, but the OHLCV store holds public market data and encrypting
the hot path buys nothing.

---

## 3. Filesystem posture

`os.umask(0o077)` is the **first statement** in `expirymanager/__main__.py`, before anything opens
a file. This is not cosmetic: SQLite creates the `-wal` and `-shm` sidecars itself and they
inherit from the umask, and a later `chmod` on the main `.db` does not touch them.

```
~/.expirymanager/          0700
  master.key               0600, refused if group or other bits are set
  config.sqlite3           0600 (plus -wal and -shm, same)
  market.duckdb            0600 (plus .wal)
  exports/                 0700
  raw/YYYY/MM/DD/*.json.gz 0600
  logs/                    0700
  tmp/                     0700
  expirymanager.lock       0600, advisory single-instance lock
```

`paths.py` refuses to start if the data directory resolves under a known cloud sync root (iCloud
Drive, Dropbox, OneDrive, Google Drive). WAL plus a sync client is a database corruption generator
as well as a key leak path.

---

## 3a. Transport: self-signed TLS on 127.0.0.1:8000

The Fyers app is registered with the redirect URI `http://127.0.0.1:8000/fyers/callback` and
Fyers matches it exactly, so the server must speak https on that host, that port and that path.
Because the project is zero configuration, it generates its own certificate rather than asking
anyone to produce one.

`security/tls.py`, called from `paths.ensure()` before uvicorn binds:

- If `~/.expirymanager/tls/server.crt` is missing or its `not_valid_after` has passed, generate a
  new RSA 2048 or EC P-256 key and a self-signed certificate with `cryptography`:
  `CN=127.0.0.1`, `subjectAltName = IP:127.0.0.1, DNS:localhost`, `basicConstraints CA:FALSE`,
  `keyUsage digitalSignature, keyEncipherment`, `extendedKeyUsage serverAuth`, validity about one
  year.
- Write both files with `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)` and fsync the file and the
  parent directory, exactly as for `master.key`.
- Print one plain-text line at startup saying the certificate is self-signed and that the browser
  will warn on first visit, so the warning is not mistaken for a fault.
- `uvicorn.run(..., ssl_keyfile=..., ssl_certfile=..., host="127.0.0.1", port=8000)`.

What this does and does not buy:

- It satisfies the broker's redirect requirement, which is the whole reason it exists.
- It encrypts loopback traffic, which is weak protection because the trust decision is the user's
  own click-through on an untrusted certificate.
- It makes `Secure` cookies work identically in development and production.
- It does **not** justify HSTS. HSTS on 127.0.0.1 poisons that origin in the browser for every
  other local development server the user runs, and a self-signed certificate makes recovery
  worse. HSTS is never sent.

The private key at `tls/server.key` is a secret for logging purposes: it is on the never-log list
in section 11 alongside the KEK.

---

## 4. Local login

`argon2-cffi` 25.1.0, `argon2.PasswordHasher()` with the library defaults: Argon2id,
`time_cost=3`, `memory_cost=65536` KiB (64 MiB), `parallelism=4`, `hash_len=32`, `salt_len=16`.
These exceed the OWASP Argon2id baseline of m=19456, t=2, p=1. The full PHC string goes in one
TEXT column; `check_needs_rehash()` runs after every successful verify and silently upgrades.

Verification takes 50 to 100 ms and is offloaded through `run_in_threadpool`. Calling it
synchronously in an async route blocks the event loop and makes the scheduler stutter on every
login attempt.

`passlib` must not be used: last released 2020-10-08, and its bcrypt backend breaks against modern
bcrypt. `bcrypt` 5.0.0 additionally now raises `ValueError` on passwords longer than 72 bytes
instead of silently truncating.

Lockout: 10 consecutive failures sets `locked_until = now + 15 minutes`. The response body and
timing for an unknown username and a wrong password are identical.

---

## 5. Sessions

Opaque server-side sessions in SQLite. Not JWT: a JWT cannot be revoked without a server-side
denylist, at which point there is server-side state anyway and nothing has been gained but a
signing key to protect and a family of algorithm confusion bugs.

```python
raw = secrets.token_urlsafe(32)                    # 256 bits
id_hash = hashlib.sha256(raw.encode()).digest()    # only this is stored
```

| Cookie | HttpOnly | SameSite | Secure | Path | Purpose |
|---|---|---|---|---|---|
| `em_session` | true | Lax | **true** | `/` | the opaque session id |
| `em_csrf` | **false** | Lax | **true** | `/` | the CSRF token, JavaScript must read it |

`Secure` is unconditionally true, in development as well as in production, because the server
speaks https on 127.0.0.1:8000 in both. That is not a preference: the registered Fyers redirect
URI is `http://127.0.0.1:8000/fyers/callback` and Fyers matches it exactly, so there is no http
mode to support.

No `Domain=` attribute, so the cookies are host-only, which is what a loopback app wants. In
production over HTTPS the `__Host-` prefix is used, which the browser enforces as Secure, Path=/
and no Domain.

**`SameSite=Lax`, never `Strict`, and the reason is specific.** The Fyers OAuth redirect back to
the callback is a cross-site top-level GET navigation. `Strict` withholds cookies on exactly that
navigation, so the callback would arrive with no session and could not verify the state-to-session
binding. Changing this later silently breaks OAuth, so the constraint is written next to the
constant in code.

Lifetimes: 8 hour idle timeout that slides (`last_seen_at` written at most once a minute to avoid
a write per request against WAL), 7 day absolute timeout that never extends. The session id is
rotated on successful login and on password change. Logout deletes the row; a password change
deletes every row for that user. An hourly scheduled job prunes expired rows.

---

## 6. The one-origin decision, and CSRF

### 6.1 Solve the origin problem first

```ts
// frontend/vite.config.ts
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const tls = path.join(os.homedir(), '.expirymanager', 'tls');

server: {
  host: '127.0.0.1',          // not 'localhost': cookies ignore the port but not the host,
                              // so same-host different-port is what makes the callback cookie
                              // visible to the dev origin.
  port: 5173,
  https: {
    key: fs.readFileSync(path.join(tls, 'server.key')),
    cert: fs.readFileSync(path.join(tls, 'server.crt')),
  },
  proxy: {
    '/api': {
      target: 'http://127.0.0.1:8000',
      // Keep the browser Origin header intact so the backend Origin check stays meaningful.
      changeOrigin: false,
      // The backend certificate is self-signed and is the one this dev server also presents.
      secure: false,
    },
  },
},
```

In production FastAPI mounts `frontend/dist` and serves `/api` itself, so it is genuinely one
origin. What this buys:

- Cookies are first-party in development and in production. `SameSite=Lax` works. No
  `SameSite=None`, therefore no forced `Secure`, therefore no development TLS certificates.
- **`CORSMiddleware` is never added to this codebase at all.** The most common way a FastAPI app
  ends up insecure is `allow_origins=["*"]` with `allow_credentials=True`, which the browser
  rejects, which people then "fix" with `allow_origin_regex=".*"`, which works and is a hole. Not
  needing CORS removes the temptation entirely.
- Development and production behave the same, so a cookie or CSRF bug shows up in development.

### 6.2 CSRF, three layers

A synchronizer token, stored on the server-side session row, delivered as the readable `em_csrf`
cookie and echoed in `X-CSRF-Token`.

```python
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

async def csrf_middleware(request, call_next):
    if request.method not in SAFE_METHODS and not _is_exempt(request.url.path):
        # Layer 1: Fetch Metadata. Free, and blocks most cross-origin attempts before any lookup.
        site = request.headers.get("sec-fetch-site")
        if site in {"cross-site", "same-site"}:
            return _reject("cross_origin_rejected")
        # Layer 2: Origin allowlist. Some browsers omit Origin on a same-origin POST,
        # so this cannot be the only check.
        origin = request.headers.get("origin")
        if origin is not None and origin not in settings.allowed_origins:
            return _reject("bad_origin")
        # Layer 3: the token, compared in constant time.
        sess = request.state.session
        if sess is None or not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), sess.csrf_token):
            return _reject("csrf_invalid")
    return await call_next(request)
```

Allowed origins: `http://127.0.0.1:5173` in development, `http://127.0.0.1:8000` in production.
Because the Vite proxy carries `/api` on the dev origin itself, every API call is `same-origin`
and never `same-site`, so the `Sec-Fetch-Site` layer is a real filter rather than a formality.

Plain double-submit is rejected: it trusts that no attacker can write a cookie on the target
origin, and a sibling origin or a MITM on a sibling http origin can. Holding the token server side
means the attacker cannot forge a matching pair.

The only exempt path is `GET /fyers/callback`, which is a GET and is protected by the single-use
`state` value.

### 6.3 DNS rebinding

`TrustedHostMiddleware` with `allowed_hosts = ["127.0.0.1", "127.0.0.1:8000", "127.0.0.1:5173"]`.
`localhost` is deliberately not in the list: the app has exactly one host name and narrowing the
allowlist costs nothing. Without Host validation, an attacker's domain can resolve to 127.0.0.1 after
page load, which makes the requests same-origin from the browser's point of view, so CORS provides
no protection at all. Host header validation is the only defence.

---

## 7. The Fyers OAuth flow

```
1  POST /api/v1/broker/fyers/connect
     state_raw  = secrets.token_urlsafe(32)
     INSERT INTO oauth_state (state_hash = sha256(state_raw),
                              session_id_hash, credential_id,
                              created_at, expires_at = now + 10 min)
     return https://api-t1.fyers.in/api/v3/generate-authcode
              ?client_id=<app_id>&redirect_uri=<redirect_uri>
              &response_type=code&state=<state_raw>

2  The user authenticates at Fyers and is redirected to
     http://127.0.0.1:8000/fyers/callback?s=ok&code=200&auth_code=<...>&state=<state_raw>
     Note the scheme, the port and the ROOT path. All three are fixed by the registration.

3  GET /fyers/callback          (mounted at the root, outside the /api prefix)
     row = SELECT ... WHERE state_hash = sha256(state)
     reject unless row exists, is unused, is unexpired, and
       hmac.compare_digest(row.session_id_hash, current_session_id_hash)
     mark used_at in the SAME transaction that read it (single use)

     appIdHash = hashlib.sha256(f"{app_id}:{app_secret}".encode()).hexdigest()   # lowercase hex

     POST https://api-t1.fyers.in/api/v3/validate-authcode
       {"grant_type": "authorization_code", "appIdHash": appIdHash, "code": auth_code}

     encrypt access_token and refresh_token, decode the JWT exp claim locally,
     generation += 1, auth_gate.set(), blocked_auth jobs -> queued

4  303 See Other -> /settings?broker=connected
```

Every state failure (missing, expired, already used, bound to a different session) returns the
same generic reason. Distinguishing them is an oracle.

`appIdHash` is the lowercase hex SHA-256 of the literal string `f"{client_id}:{app_secret}"`.
The doc prose says "SHA-256 of api_id + app_secret" while its own worked example says
"SHA-256 of app_id:app_secret", and the two example digests printed in the docs are inconsistent
with each other (one is 64 hex characters, the other 63). Neither is a usable test vector. The
colon-joined pre-image description is what is implemented, and `client_id` includes the `-100`
suffix.

**The callback URL contains `auth_code` in its query string**, so it lands in browser history, the
Referer header and the uvicorn access log. Three mitigations are mandatory and all three ship:
the 303 to a clean URL, `Referrer-Policy: no-referrer`, and a uvicorn access log filter that
strips the query string for exactly that path.

The redirect URI is `http://127.0.0.1:8000/fyers/callback`. Fyers does accept a loopback redirect
URI, confirmed by the developer's existing registration, but it must be https and it is matched
exactly, so the app has no freedom over the scheme, the host, the port or the path. The setup
wizard shows that exact string with a copy button so the value registered on the Fyers dashboard
and the value the app serves cannot drift.

The manual paste-the-redirected-URL fallback (`POST /api/v1/broker/fyers/callback/manual`) still
ships and runs the identical verification path, because the first visit shows a certificate
warning and a user who declines it will not complete the automatic redirect.

---

## 8. Inbound rate limiting

`limits` 5.8.0 with `MemoryStorage` and `MovingWindowRateLimiter`, wired as FastAPI dependencies.
In-memory is correct here because there is exactly one process by design.

| Scope | Limit |
|---|---|
| `POST /auth/login` | 5 per 15 minutes per (ip, username), plus lockout after 10 failures |
| `POST /auth/setup`, `POST /auth/password` | 5 per hour per ip |
| `GET /broker/fyers/callback`, `POST /broker/fyers/connect` | 20 per minute per ip |
| `POST /downloads`, `POST /jobs/*`, `POST /schedules*` | 30 per minute per session |
| `POST /exports`, `POST /system/checkpoint` | 10 per minute per session |
| `POST /system/optimise`, `POST /system/backup` | 2 per hour per session |
| `GET /bars*`, `GET /contracts/*/bounds`, `GET /system/budget` | 240 per minute per session |
| Other GETs | 120 per minute per session |
| Global fallback | 300 per minute per session |
| `GET /events/stream` | 10 concurrent streams per session |

Every limited response carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`;
a 429 also carries `Retry-After`.

`slowapi` 0.1.10 is deliberately not used: it declares `requires_python <4,>=3.7` but its
classifiers stop at Python 3.13 while this project targets 3.14.6. `limits` is its underlying
dependency anyway and declares `>=3.10`.

**This is entirely separate from the outbound Fyers throttle.** Inbound limiting protects this
process from its own browser. The outbound governor in `brokers/fyers/throttle.py` is a scheduling
constraint protecting the user's broker account, with its own durable counters. They share no code
and must never be conflated.

---

## 9. Security headers

Emitted by `security/headers.py` on every response, using `secure` 2.0.1 where convenient.

```
Content-Security-Policy: default-src 'self';
  script-src 'self';
  style-src 'self' 'unsafe-inline';
  img-src 'self' data:;
  font-src 'self';
  connect-src 'self';
  frame-ancestors 'none';
  base-uri 'none';
  form-action 'self';
  object-src 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
Permissions-Policy: geolocation=(), camera=(), microphone=(), payment=(), usb=()
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Cache-Control: no-store            (on every /api response)
```

**`style-src 'unsafe-inline'` is required and the reason is verified, not assumed.**
`openalgo-charts/src/widget/styles.ts` line 304 does
`doc.createElement('style')` and sets `style.textContent`, which CSP governs under `style-src`,
and `src/widget/tokens.ts` writes every `--oac-*` chrome token with `el.style.setProperty`. A
strict `style-src 'self'` silently renders the chart unstyled. There is no `eval` and no
`new Function` anywhere in the library, which was checked, so `script-src` stays strict at
`'self'`. Inline script is the XSS vector that matters here and it remains blocked.

`Strict-Transport-Security` is emitted **only** when the environment is production **and** the
request scheme is https. Setting HSTS on an `http://localhost` response poisons the localhost
origin in that browser for a year, for every other local development server the user runs.

---

## 10. What must never be logged

`security/redaction.py` provides a `logging.Filter` installed on the root logger, on the uvicorn
loggers and on the httpx logger.

Two independent mechanisms, because either alone is insufficient:

1. A named-key scrubber over structured log fields and over any dict rendered into a message:
   `app_secret`, `appIdHash`, `access_token`, `refresh_token`, `auth_code`, `code`, `pin`,
   `password`, `csrf_token`, `state`, `Authorization`, `authorization`, `em_session`, `em_csrf`,
   `wrapped_dek`, `master_key`, `ssl_keyfile`, `private_key`.
2. A shape regex, because tokens turn up in unexpected fields and under unexpected names. The
   Fyers app secret and both tokens are JWTs:
   `eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.` is replaced with `[redacted-jwt]`.

Plus a uvicorn access log filter that replaces the query string with `?[redacted]` for the path
`/fyers/callback`.

**Never logged, at any level, in any environment:**

- The app secret, the PIN, the local passcode, any Argon2 input.
- Any access token, refresh token or auth code, in whole or in part.
- The KEK, the DEK, `wrapped_dek`, or any raw envelope BLOB.
- The TLS private key at `~/.expirymanager/tls/server.key`.
- The raw session id or the raw CSRF token (`sha256` prefixes are acceptable).
- The raw OAuth `state` value.
- A full Fyers response body on an auth error path.
- The full OAuth callback URL.

**What is logged instead:** `token_fingerprint` (sha256 of the access token, first 8 hex
characters), the credential id, the endpoint path, the request parameters with the above keys
scrubbed, the HTTP status, the Fyers envelope code, latency and byte size. That is enough to
diagnose every failure the pipeline can produce.

---

## 11. What must never be returned

- **No secret and no mask.** `GET /api/v1/broker/fyers` returns `app_secret_configured: true`, not
  `sk_****abcd`. A mask is an oracle and it tempts the frontend into round-tripping it back on
  save, which is how a secret gets overwritten with asterisks.
- **No raw file paths the browser could ask for.** `task.raw_body_path` is projected as a boolean
  `has_raw_body`. Export files are served by `export_id`, and the path is re-derived server side
  and asserted to resolve inside `~/.expirymanager/exports` after `Path.resolve()`.
- **No upstream error body.** `raise HTTPException(500, str(exc))` is exactly how a Fyers error
  body containing a token reaches the browser console. Production 500 handlers return a
  correlation id and a generic message; the detail goes to the log, redacted.
- **No distinguishing auth failure.** Unknown username and wrong password return an identical body
  with identical timing. Every OAuth state failure returns one generic reason.
- **No stack traces in production.** `debug` is never enabled outside a developer's own machine.

---

## 12. SQLite hardening

Applied on **every** connection through a SQLAlchemy `connect` event listener, not once at startup:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;      -- per connection, defaults to OFF, must be re-applied every time
PRAGMA busy_timeout = 5000;
PRAGMA secure_delete = ON;     -- so a rotated token does not linger in a free page
PRAGMA trusted_schema = OFF;
PRAGMA cell_size_check = ON;
```

`secure_delete` is the one that matters for this application: without it, a revoked access token
ciphertext stays readable in a freed page until that page is reused, which defeats the point of
revoking it.

---

## 13. Security tests that must pass

`tests/test_crypto.py`
- Envelope round trip for every secret column.
- A ciphertext moved from `broker_credential.app_secret_enc` to `broker_token.access_token_enc`
  fails to decrypt (AAD relocation).
- A ciphertext moved to a different row id fails to decrypt.
- Switching KEK provider rewraps the DEK and leaves every field ciphertext byte identical.
- A truncated envelope and a wrong magic are rejected before any crypto call.

`tests/test_security_http.py`
- POST with no `X-CSRF-Token` is 403.
- POST with a token from a different session is 403.
- POST with `Sec-Fetch-Site: cross-site` is 403 before the token is even read.
- POST with a foreign `Origin` is 403.
- A request with a foreign `Host` is 400 from TrustedHost.
- The full header set is present on an API response and on the SPA index.
- HSTS is absent on an http response and present on a simulated https production response.
- The redaction filter removes a JWT-shaped value from a message, from a dict field, and from a
  nested exception argument.
- The access log filter strips the callback query string for `/fyers/callback`.
- `security/tls.py` generates a certificate whose SAN contains IP 127.0.0.1 and DNS localhost, at
  mode 0600, and regenerates an expired one.
- The server answers https on 127.0.0.1:8000 and `/fyers/callback` is routed at the root.
- `GET /broker/fyers` never contains the string of the stored secret.

`tests/test_api_smoke.py`
- Login rate limit returns 429 with `Retry-After` after the sixth attempt.
- The OAuth state is single use: replaying the same callback URL fails identically the second
  time.
