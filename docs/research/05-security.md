# ExpiryManager Security Model

Research note 05. Scope: the security design for a local-first, zero-config FastAPI application
that stores third-party broker (Fyers) API credentials in SQLite, with an unattended scheduler.

House rules for this document and all code it describes: no emoji, no icons, no em dashes or en
dashes, plain text labels only. Comments explain why, not what.

Verified package versions are as of 2026-09-09 from the PyPI JSON API.

---

## 0. Executive summary (the decisions)

| Concern | Decision |
|---|---|
| Field encryption | AES-256-GCM via `cryptography` 50.0.1, random 96-bit nonce, versioned envelope, AAD binds table, column, row id and key version |
| Key hierarchy | Random 32-byte DEK encrypts fields. DEK is wrapped by a KEK. KEK provider is pluggable |
| KEK primary | Key file, `~/.expirymanager/master.key`, 32 raw bytes, mode 0600 in a 0700 directory |
| KEK fallback / upgrade | OS keyring (`keyring` 25.7.0) as an opt-in provider; passphrase + Argon2id as an opt-in paranoid provider that disables unattended restart |
| Password hashing | `argon2-cffi` 25.1.0, Argon2id, m=65536 KiB, t=3, p=4, hash_len=32, salt_len=16 (RFC 9106 low-memory, the library default) |
| Session | Opaque 256-bit random id, SHA-256 stored server side in SQLite, idle 8h, absolute 7d, rotate on login |
| CSRF | Synchronizer token delivered as a JS-readable cookie, echoed in `X-CSRF-Token`, plus an `Origin` / `Sec-Fetch-Site` check |
| Dev origin problem | Solved by the Vite dev proxy so there is exactly one browser origin in dev and in prod. CORS is then not needed at all |
| Rate limiting (inbound) | `limits` 5.8.0 with `MemoryStorage` + `MovingWindowRateLimiter` behind a FastAPI dependency, per-route budgets |
| Rate limiting (outbound) | Separate concern. A global async token bucket plus a SQLite-persisted daily counter, targeted below the published Fyers limits |
| Security headers | `secure` 2.0.1 or a 30-line middleware. Strict CSP on the production static mount only. No HSTS on http://localhost |
| OAuth callback | Server-side single-use `state` row bound to the session, 10 minute TTL, `hmac.compare_digest`, 303 redirect to a clean URL |
| SQLite | WAL, `secure_delete=ON`, `trusted_schema=OFF`, `foreign_keys=ON` per connection, `busy_timeout`, 0600 on db and on the `-wal` / `-shm` sidecars |
| SQLCipher | Not worth it here. Reasoning in section 10 |

---

## 1. Threat model

State this up front because every control below is only meaningful against a named threat.
The application is local-first: a single user, a single machine, uvicorn bound to loopback.

| Id | Threat | Defended by | Honest verdict |
|---|---|---|---|
| T1 | The SQLite file alone leaves the machine (a backup, a Dropbox or iCloud sync folder, a `git add .`, a support bundle the user emails, a screen share of a DB browser) | Field-level AEAD with the key stored outside the DB | Fully defended. This is the single most likely real-world leak and the main reason encryption at rest earns its place |
| T2 | The whole home directory leaves the machine (a Time Machine volume, a full `rsync` of `~`, a stolen unlocked backup drive) | Key file: not defended, the key travels with the data. Keyring: partially, the Keychain is a separate file with its own protection. Passphrase: fully defended | This is the only threat where the KEK provider choice actually changes the outcome |
| T3 | Malware or any other program running as the same user | Nothing in this document | Not defended, and not defendable. Any process running as the user can read the key file, can prompt the Keychain in the same way our process does, and can read the plaintext out of our process memory. Do not claim otherwise in the UI |
| T4 | Stolen laptop, powered off | FileVault (on by default on modern macOS) | Defended by the OS, not by us. Document that FileVault must be on |
| T5 | A second local user account on the same machine | 0700 data directory, 0600 files, umask 0o077 | Defended |
| T6 | A malicious or compromised web page in the user's browser reaching `http://127.0.0.1:8000` while the user is logged in | CSRF token, `Origin` and `Sec-Fetch-Site` checks, no wildcard CORS, `TrustedHostMiddleware` (DNS rebinding), session cookie `SameSite=Lax` | Defended. This is the second most likely real attack against a local server and is routinely underestimated |
| T7 | Secrets leaking into logs, tracebacks, HTTP access logs, browser history, error toasts | Redaction filter, response-model separation, query-string scrubbing, `Referrer-Policy: no-referrer` | Defended by discipline. Needs tests |
| T8 | Shoulder surfing, screenshots, screen recordings during a demo | Secrets are write-only in the UI. After save the field renders as "configured", never as the value | Defended |
| T9 | Someone with write access to the DB file swapping ciphertext between rows or columns | AAD binding table, column, row id and key version | Defended. This is why we use AEAD with AAD and not Fernet |

Non-goals: multi-tenant isolation, protection against a root-level or kernel-level adversary,
protection against the Fyers backend itself, hardware-backed attestation.

---

## 2. Encryption at rest

### 2.1 What gets encrypted

Encrypt (each in its own column, each its own AEAD envelope):

- `broker_credential.app_secret` (Fyers app secret)
- `broker_token.access_token`
- `broker_token.refresh_token`
- `broker_credential.pin` if the user opts into the refresh-token flow, which requires the PIN
- any future webhook signing secret from postbacks (doc 22)

Do not encrypt: `app_id` (the client id, semi-public and needed for the auth URL), `redirect_uri`,
timestamps, catalog metadata, job state, or anything in DuckDB. Encrypting those buys nothing and
makes queries and debugging worse.

Fyers token lifetimes (from `06-authentication-login-flow-user-apps.md`): the access token is a
daily token, the refresh token has a 15 day validity, and the docs carry a note that the refresh
token "will be discontinued from 1st April" alongside the SEBI retail algo trading changes taking
effect 2026-04-01. Design implication: the unattended scheduler cannot assume it can always mint a
fresh token without a human. See section 8.4.

### 2.2 Algorithm

Use AES-256-GCM from `cryptography` 50.0.1 (released 2026-08-25, requires Python >= 3.9, so fine on
3.14.6).

```python
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
```

- Key length: 32 bytes (AES-256).
- Nonce: 12 bytes from `os.urandom(12)`, fresh per encryption, never reused, never derived from a
  counter kept in the DB.
- Tag: 16 bytes, appended to the ciphertext by the library.
- AAD: mandatory, see 2.4.

Why not Fernet: Fernet is AES-128-CBC plus HMAC-SHA256, it has no AAD parameter at all (so T9 is
undefendable), it embeds a plaintext timestamp, and it forces base64. It is a fine general default
but it is the wrong tool the moment you want context binding.

Why random nonces are safe here: the birthday bound for random 96-bit nonces under a single key is
about 2^32 messages for a 2^-32 collision probability (NIST SP 800-38D). This application writes on
the order of one to a few hundred secret records per year. Still, encode the rule: the DEK carries a
`use_count` and the app rotates the DEK at 2^32 encryptions. That counter will never fire, and
writing it down is how you prove the reasoning was done.

If nonce-misuse resistance is wanted as belt and braces, `cryptography` also ships
`AESGCMSIV` (`cryptography.hazmat.primitives.ciphers.aead.AESGCMSIV`, available since 42.0). It is a
drop-in with the same interface and a small performance cost. Recommendation: plain `AESGCM` is
correct; note `AESGCMSIV` in the code comment as the escape hatch if the nonce source is ever
changed to something deterministic.

### 2.3 Envelope format

Store as a SQLite `BLOB`, not TEXT. Versioned so the scheme can change without a migration guess:

```
magic      3 bytes   b"EM1"
key_ver    1 byte    uint8, which DEK version encrypted this
nonce     12 bytes
ct+tag    n bytes    AESGCM.encrypt() output
```

Minimum size 32 bytes. Reject anything shorter or with the wrong magic before touching crypto.
`key_ver` is what makes online DEK rotation possible: during rotation, old and new DEKs are both
loaded, decrypt dispatches on `key_ver`, encrypt always uses the newest.

### 2.4 AAD (additional authenticated data)

AAD is not encrypted but is authenticated. Bind the ciphertext to exactly where it lives so it
cannot be relocated:

```python
def aad(table: str, column: str, row_id: str, key_ver: int) -> bytes:
    # Bound to location so an attacker with DB write access cannot move a ciphertext
    # from one row or column to another and have it still decrypt.
    return f"expirymanager|v1|{table}|{column}|{row_id}|{key_ver}".encode("utf-8")
```

Gotcha, and it is a real one: the AAD needs `row_id` before the INSERT. Do not use SQLite
`INTEGER PRIMARY KEY AUTOINCREMENT` for tables holding encrypted columns, because you would have to
insert then update, which leaves a window and complicates rollback. Use a `TEXT` primary key holding
a UUID generated in Python (`uuid.uuid4()`, or a UUIDv7 if you want time-ordered ids) so the id is
known before encryption.

### 2.5 Key hierarchy

Two levels. This is the piece that makes the "where does the master key live" question answerable
without repainting the whole database every time the answer changes.

```
KEK (32 bytes)  supplied by a pluggable provider, never stored in SQLite
  wraps
DEK (32 bytes)  random, generated once at first run
  encrypts
field ciphertexts in SQLite
```

The wrapped DEK lives in SQLite:

```sql
CREATE TABLE crypto_key (
  version        INTEGER PRIMARY KEY,      -- matches key_ver in the envelope
  wrapped_dek    BLOB NOT NULL,            -- EM1 envelope, AAD = b"expirymanager|kek-wrap|v1"
  kek_provider   TEXT NOT NULL,            -- 'keyfile' | 'keyring' | 'passphrase'
  kdf_params     TEXT,                     -- JSON, only for the passphrase provider
  state          TEXT NOT NULL,            -- 'active' | 'retiring' | 'retired'
  created_at     TEXT NOT NULL,
  use_count      INTEGER NOT NULL DEFAULT 0
);
```

Consequences, and they are the whole point:

- Switching KEK provider (key file to passphrase, or the reverse) rewraps one 32-byte DEK. It never
  re-encrypts a single field. That makes "start on the easy provider, upgrade later" a real,
  reversible product feature rather than a migration project.
- Rotating the KEK is also just a rewrap.
- Rotating the DEK is the expensive path (decrypt and re-encrypt every secret field), only needed
  after a suspected compromise. It stays possible because `key_ver` is in the envelope.

Wrap the DEK with AES-256-GCM under the KEK using a constant AAD `b"expirymanager|kek-wrap|v1"`.
`cryptography` also offers RFC 3394 `aes_key_wrap`, which is a legitimate alternative, but using
AESGCM for both levels means one primitive, one envelope parser, one set of tests.

### 2.6 Where the KEK lives: the three candidates, judged

The hard constraint that decides this: the scheduler must run unattended after a reboot.

#### Candidate A: OS keyring, `keyring` 25.7.0 (released 2025-11-16, Python >= 3.9)

Backend on macOS is the Keychain; on Windows the Credential Manager; on Linux SecretService or
KWallet.

```python
import base64, keyring
keyring.set_password("ExpiryManager", "kek", base64.b64encode(kek).decode())
```

Defends: T2 partially (the Keychain is a separate encrypted store with its own ACLs, so a naive
`rsync ~/Library/Application Support` does not necessarily carry a usable key), T1 (the DB alone is
useless).

Does not defend: T3 at all, and on macOS it is materially weaker for a Python app than it looks.
The Keychain ACL is granted to the executable that created the item, and for us that executable is
the Python interpreter. jaraco/keyring issue 457 documents exactly this: once the secret is stored,
any Python script the user runs, even from a different virtualenv, reads it back with no prompt.
So on macOS, for a Python app, the keyring's advantage over a 0600 file against a local attacker is
close to zero. It is still better against T2.

The reboot problem, and this is disqualifying as a default: the macOS Data Protection Keychain is
only reachable from a process running inside a user session. Apple's own guidance is that programs
running outside a user context, such as a `launchd` daemon, must target the file-based keychain.
Concretely:

- As a `LaunchAgent` in the user's session: works, because the login keychain unlocks at login. But
  it only starts once the user logs in. A Mac that reboots at 03:00 and sits at the login window
  runs no agent and downloads no data.
- As a `LaunchDaemon` (system context, runs before and without any login): cannot reach the user
  Keychain at all. The scheduler dies at startup.

Verdict: excellent as an opt-in upgrade for users who run the app interactively. Wrong as the
zero-config default given the unattended requirement.

#### Candidate B: generated key file, 0600, in the app data directory (RECOMMENDED PRIMARY)

`~/.expirymanager/master.key`, 32 raw bytes from `os.urandom(32)`, directory 0700, file 0600.

```python
import os, pathlib

def load_or_create_keyfile(path: pathlib.Path) -> bytes:
    if path.exists():
        st = path.stat()
        # Refuse to run on a world- or group-readable key, the way sshd refuses a loose private key.
        if st.st_mode & 0o077:
            raise SystemExit(
                f"Refusing to start: {path} is group or world accessible. "
                f"Run: chmod 600 {path}"
            )
        return path.read_bytes()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    kek = os.urandom(32)
    # O_EXCL so a concurrent first-run cannot clobber a key that another process just created.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(kek)
        fh.flush()
        os.fsync(fh.fileno())
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)   # durability of the directory entry, not just the file contents
    finally:
        os.close(dir_fd)
    return kek
```

Defends: T1 fully (the DB alone is inert), T5 (0600). Works identically on macOS, Linux and Windows.
Works in a `LaunchDaemon`, in a `systemd` unit, in a Docker container, at 03:00 with nobody logged
in. Zero prompts, zero configuration, which is exactly the product requirement.

Does not defend: T2 (a full home-directory copy carries both halves) or T3.

Mitigations that make T2 much less likely in practice, and all of them should be implemented:

- Ship a `.gitignore` in the data directory covering `*.key`, `*.db`, `*.db-wal`, `*.db-shm`.
- On startup, resolve the data directory and warn loudly if it sits under a known sync root
  (`~/Library/Mobile Documents`, `~/Dropbox`, `~/Google Drive`, `~/OneDrive`). This also prevents
  SQLite WAL corruption, so it pays for itself twice.
- Add `~/.expirymanager` to the Time Machine exclusion list on first run (`tmutil addexclusion`),
  offered as a checkbox rather than done silently.
- The "export diagnostics" feature must never include the data directory.

Verdict: this is the primary. It is the only one of the three that satisfies both "zero config" and
"unattended after reboot", and on macOS it gives up less to the Keychain than it first appears.

#### Candidate C: user passphrase with Argon2id derivation

KEK = Argon2id(passphrase, salt), salt is 16 random bytes stored in `crypto_key.kdf_params`.

```python
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id  # added in cryptography 44.0

kek = Argon2id(
    salt=salt,
    length=32,
    iterations=3,
    lanes=4,
    memory_cost=262144,   # 256 MiB, this is a KEK not a login hash, spend more
).derive(passphrase.encode("utf-8"))
```

Note the parameter names differ from argon2-cffi: `iterations` / `lanes` / `memory_cost` here
versus `time_cost` / `parallelism` / `memory_cost` there. Easy to get wrong.

Defends: T1, T2 and T5. It is the only option that survives a full home-directory copy, because the
key exists only in the user's head and in process memory.

Does not defend: T3.

Kills: unattended restart. After every reboot, crash, or `uvicorn --reload`, the scheduler is dead
until a human types the passphrase. There is no honest way around this. Caching the derived KEK on
disk so the scheduler survives a reboot reduces the scheme exactly to Candidate B with extra steps,
and should be labelled as such in the UI rather than sold as passphrase protection.

Verdict: offer it as an explicit opt-in mode named something like "Require passphrase on start".
When enabled, the UI must say plainly: "Scheduled downloads will pause after a restart until you
unlock the app." Do not enable it by default and do not offer a "remember passphrase" checkbox.

#### The recommendation

Primary: **key file (Candidate B)**, created automatically on first run. Zero config, no prompts,
survives reboot, works under `launchd`, `systemd` and Docker.

Documented alternatives, both selectable in Settings and both implemented as KEK providers behind
one interface so switching is a DEK rewrap:

1. **OS keyring**, for a user who runs ExpiryManager interactively as a `LaunchAgent` and wants the
   KEK out of the file tree. Startup must detect "keyring provider configured but unreachable"
   (headless context, locked keychain) and fail with a clear message plus a documented recovery
   path, not a stack trace.
2. **Passphrase + Argon2id**, for a user who accepts manual unlock after every restart.

Provider interface, deliberately tiny:

```python
class KekProvider(Protocol):
    name: str
    def get(self) -> bytes: ...
    def put(self, kek: bytes) -> None: ...
    def available(self) -> bool: ...
```

And a hard rule to write into the code: the KEK and DEK are `bytes` held in one module-level
`CryptoBox` instance, never placed on a Pydantic model, never on an ORM row, never in a
`FastAPI` dependency return value, never in `app.state` as a plain attribute that a debug endpoint
might dump. Python cannot reliably zero them, so do not pretend to by writing `del`; just limit the
blast radius.

---

## 3. Password hashing for the local login

The app should have a local login. Even on a single-user machine it is what makes T6 (a hostile web
page hitting loopback) meaningfully harder and it gives the session and CSRF machinery something to
hang off.

### Recommendation

`argon2-cffi` 25.1.0 (released 2025-06-03, Python >= 3.8). It is the reference CFFI binding to the
Argon2 reference implementation and is actively maintained.

```python
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

# Library defaults are the RFC 9106 "SECOND RECOMMENDED" (low memory) profile:
# time_cost=3, memory_cost=65536 KiB (64 MiB), parallelism=4, hash_len=32, salt_len=16.
ph = PasswordHasher()

def hash_password(pw: str) -> str:
    return ph.hash(pw)                      # PHC string, salt is embedded, store as TEXT

def verify_password(stored: str, pw: str) -> tuple[bool, str | None]:
    try:
        ph.verify(stored, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, None
    # Only here do we hold the cleartext, so this is the only place a param upgrade can happen.
    return True, (ph.hash(pw) if ph.check_needs_rehash(stored) else None)
```

Parameters: **use the library defaults** (m=65536 KiB, t=3, p=4). They are RFC 9106's second
recommended option and they exceed the OWASP Password Storage Cheat Sheet baseline for Argon2id
(m=19456 KiB, t=2, p=1). This is a desktop-class machine serving one login, not a server doing
thousands per second, so there is no reason to drop to the OWASP floor. Do not go to
`RFC_9106_HIGH_MEMORY` (2 GiB), which would make login feel broken and could be OOM-killed.

Store the full PHC string (`$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`) in a single TEXT column.
Do not store salt separately, do not store the parameters separately, they are in the string.

Operational notes:

- Argon2 with p=4 and 64 MiB is roughly 50 to 100 ms. Login must be `async def` calling the hash in
  a thread (`anyio.to_thread.run_sync` or `starlette.concurrency.run_in_threadpool`), otherwise it
  blocks the event loop and the scheduler stutters on every login attempt.
- On a failed login for an unknown user, still run a dummy verify against a fixed hash so the
  response time does not distinguish "no such user" from "wrong password".
- `check_needs_rehash` is what lets you raise parameters later without forcing a reset.

### If bcrypt instead

`bcrypt` 5.0.0 (2025-09-25) is the only defensible alternative, and only for one reason: it has no
memory-hard cost, so it stays predictable on a memory-constrained box. Use cost 12 or higher.

Two gotchas if you go this way:

- bcrypt silently truncated passwords over 72 bytes for its whole history. As of 5.0.0, `hashpw`
  raises `ValueError` on a password longer than 72 bytes. Pre-hash with SHA-256 and base64 if you
  want to accept long passphrases, and be consistent about it forever.
- bcrypt 5.0.0 dropped Python 3.8.

### Do not use passlib

`passlib` 1.7.4 was last released 2020-10-08. It is effectively unmaintained and its bcrypt backend
famously breaks against modern `bcrypt` releases. If an abstraction over multiple hash schemes is
genuinely wanted (for future migration), use `pwdlib` 0.3.1 (2026-08-12, Python >= 3.10), which is
the modern maintained replacement and wraps `argon2-cffi` and `bcrypt`. For one scheme, calling
`argon2-cffi` directly is fewer moving parts.

---

## 4. Sessions

### Design

Opaque server-side sessions in SQLite. Not JWT.

Why not JWT here: a JWT cannot be revoked without a server-side denylist, at which point you have
server-side state anyway and have gained nothing but a signing key to protect and a family of
algorithm-confusion bugs. `PyJWT` 2.13.0 is fine software; it is the wrong shape for this app.

```sql
CREATE TABLE session (
  id_hash            BLOB PRIMARY KEY,   -- sha256(raw session id). Raw id is never stored.
  user_id            TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  csrf_token         TEXT NOT NULL,      -- 32 random bytes, base64url
  created_at         TEXT NOT NULL,
  last_seen_at       TEXT NOT NULL,
  idle_expires_at    TEXT NOT NULL,      -- last_seen_at + 8h, slides
  absolute_expires_at TEXT NOT NULL,     -- created_at + 7d, never slides
  user_agent         TEXT,
  client_ip          TEXT
);
```

Storing only `sha256(id)` means that reading the DB (T1) does not hand over live sessions. The
lookup is a single indexed equality on the hash, so there is no cost.

```python
import hashlib, secrets

raw = secrets.token_urlsafe(32)            # 256 bits of entropy, 43 chars
id_hash = hashlib.sha256(raw.encode()).digest()
```

### Cookies

| Cookie | HttpOnly | SameSite | Secure | Path | Purpose |
|---|---|---|---|---|---|
| `em_session` | true | Lax | prod: true, dev: false | `/` | the opaque session id |
| `em_csrf` | **false** | Lax | prod: true, dev: false | `/` | the CSRF token, JS must read it |

`SameSite=Lax`, not `Strict`, and the reason is specific rather than habitual: the Fyers OAuth
redirect back to our callback is a cross-site top-level GET navigation. `Strict` withholds cookies
on exactly that navigation, so the callback would arrive with no session and could not bind the
`state` to a session. `Lax` sends cookies on top-level GET navigations, which is what we need and
nothing more. `SameSite=None` is never needed because we never make a genuine cross-site request
(see section 5).

`Secure` in dev: Chrome and Firefox do permit `Secure` cookies over `http://localhost`, but Safari
historically did not. Drive it from config (`SETTINGS.cookie_secure`), default false in dev and true
in prod, rather than relying on browser-specific leniency.

Do not use `Domain=`. An omitted Domain gives a host-only cookie, which is what a loopback app
wants. In production over HTTPS, use the `__Host-` prefix (`__Host-em_session`), which the browser
enforces as Secure, Path=/, and no Domain.

### Lifetimes

- Idle timeout: 8 hours, slides on each authenticated request (write `last_seen_at` at most once a
  minute to avoid a write per request against WAL).
- Absolute timeout: 7 days, never extends. Forces a re-login weekly.
- Rotate the session id on successful login (session fixation) and on password change.
- On logout, delete the row. On password change, delete every row for that user.
- A scheduled job prunes expired rows hourly. It runs in the same scheduler as the download jobs.

### Alternative considered and rejected

Starlette `SessionMiddleware` (itsdangerous 2.2.0 signed cookie). Rejected: it puts state in the
browser, it cannot be revoked, and a signed cookie holding session data is a larger footgun than a
32-byte opaque id in a table we already have.

---

## 5. CSRF, CORS, and the two-origin dev problem

### 5.1 Solve the origin problem first, then CSRF is easy

The single highest-value decision in this section: **use the Vite dev server proxy so the browser
only ever talks to one origin, in dev and in prod.**

```ts
// vite.config.ts
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        // Keep the browser's Origin header intact so the backend's Origin check stays meaningful.
        changeOrigin: false,
      },
    },
  },
});
```

In production, FastAPI mounts the built `dist/` as static files and serves `/api` itself, so it is
genuinely one origin.

What this buys, and it is a lot:

- Cookies are first-party in dev and in prod. `SameSite=Lax` works. No `SameSite=None`, which means
  no forced `Secure`, which means no dev TLS certificates.
- `CORSMiddleware` is not needed at all. The most common way FastAPI apps end up insecure is
  `allow_origins=["*"]` with `allow_credentials=True`, and the way to never write that line is to
  not need CORS.
- Dev and prod behave the same, so a CSRF or cookie bug shows up in dev instead of in production.

### 5.2 CSRF pattern

**Synchronizer token, transported as a JS-readable cookie and echoed in a header.** This is the
strongest variant available to us and it costs nothing, because we already have server-side session
state to hang the token off.

- On session creation, generate `csrf = secrets.token_urlsafe(32)` and store it in the `session` row.
- Set it as the non-HttpOnly `em_csrf` cookie so the SPA can read it with `document.cookie`.
- The SPA sends it back as the `X-CSRF-Token` request header on every state-changing request.
- The server compares the header value against `session.csrf_token` using
  `hmac.compare_digest`.

Why this and not the plain double-submit cookie: plain double-submit trusts that no attacker can
write a cookie on the target origin. A subdomain or a MITM on a sibling http origin can, and the
attack is well documented. Because we hold the token server side, the attacker cannot forge a pair.
The stateless alternative, a signed double-submit token bound to the session id via HMAC, is also
sound and is what `fastapi-csrf-protect` 1.0.7 (2025-09-16) implements if you would rather take a
dependency than write 60 lines. Either is acceptable; the synchronizer token is simpler to reason
about here.

Enforcement, as a single middleware:

```python
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}  # prod: the app origin only

async def csrf_middleware(request, call_next):
    if request.method not in SAFE_METHODS:
        # Layer 1: Fetch Metadata. Costs nothing and blocks most of T6 before the token is read.
        site = request.headers.get("sec-fetch-site")
        if site in {"cross-site", "same-site"}:
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        # Layer 2: Origin allowlist. Absent Origin on a same-origin POST is allowed by some
        # browsers, so this cannot be the only check.
        origin = request.headers.get("origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "bad origin"}, status_code=403)
        # Layer 3: the token itself.
        sent = request.headers.get("x-csrf-token", "")
        sess = request.state.session
        if sess is None or not hmac.compare_digest(sent, sess.csrf_token):
            return JSONResponse({"detail": "csrf token invalid"}, status_code=403)
    return await call_next(request)
```

Exemptions, and there are only two:

- `GET /api/broker/fyers/callback`. It is a GET, so it is already exempt by method, and it has its
  own single-use `state` defense (section 7).
- `POST /api/auth/login`. There is no session yet, so there is no synchronizer token. Protect it
  with a pre-session CSRF cookie issued by `GET /api/auth/csrf`, or accept the `Origin` plus
  `Sec-Fetch-Site` check alone for this one route. Login CSRF is a real but low-value attack here
  (the attacker would be logging the victim into the attacker's own local account, which does not
  exist on this machine), so the Origin check is sufficient. Document the reasoning rather than
  silently exempting.

Never exempt an endpoint "temporarily for testing". Add a test that asserts every non-safe route is
covered.

### 5.3 CORS

Production: same origin. Do not add `CORSMiddleware` at all. An absent CORS layer cannot be
misconfigured.

Dev with the Vite proxy: also same origin. Do not add it.

Dev without the proxy (only if the user insists on hitting `:8000` directly from `:5173`):

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # explicit list, never "*"
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
    expose_headers=["RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "Retry-After"],
    max_age=600,
)
```

and gate the whole block behind `if SETTINGS.env == "development"` so it cannot ship. Note the trap
this mode drags in: with a genuinely cross-origin browser context, `SameSite=Lax` cookies are not
sent, so you would have to move to `SameSite=None; Secure`, which requires HTTPS on both ports,
which requires dev certificates. That cascade is the argument for the proxy, stated concretely.

Never use `allow_origins=["*"]` together with `allow_credentials=True`. Starlette will emit `*` and
the browser will refuse the credentialed request anyway, so it does not even work; people then
"fix" it with `allow_origin_regex=".*"`, which does work and is a hole.

Bind uvicorn to `127.0.0.1` by default, never `0.0.0.0`. Make binding to a routable address an
explicit flag that prints a warning.

Add `TrustedHostMiddleware(allowed_hosts=["localhost", "127.0.0.1", "[::1]"])` as the outermost
middleware. This is the DNS rebinding defense: without it, an attacker's domain can be made to
resolve to 127.0.0.1 after the page loads, at which point the page's requests are same-origin from
the browser's point of view and CORS does not help. Host header validation is what stops it.

---

## 6. Rate limiting

Two completely separate systems. Do not let them share code or vocabulary, because conflating them
is how people end up with a scheduler that drops downloads because a browser refresh loop consumed
the budget.

### 6.1 Inbound: protecting the FastAPI API

Purpose: brute force on the local login, runaway frontend loops, and T6 abuse from a hostile page.
There is one user and one process, so no Redis, no distributed state.

Recommendation: `limits` 5.8.0 (2026-02-05, Python >= 3.10) used directly, with `MemoryStorage` and
`MovingWindowRateLimiter`, behind a small FastAPI dependency.

```python
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

storage = MemoryStorage()
limiter = MovingWindowRateLimiter(storage)

def rate_limit(spec: str, scope: str):
    item = parse(spec)                       # e.g. "5/15minute"
    async def dep(request: Request):
        # Key on the session when we have one, on the peer address when we do not.
        ident = request.state.session_id or (request.client.host if request.client else "unknown")
        if not limiter.hit(item, scope, ident):
            window = limiter.get_window_stats(item, scope, ident)
            retry = max(1, int(window.reset_time - time.time()))
            raise HTTPException(429, "rate limit exceeded",
                                headers={"Retry-After": str(retry)})
    return dep
```

Per-route budgets:

| Route | Limit | Key | Reason |
|---|---|---|---|
| `POST /api/auth/login` | 5 per 15 min | client ip + submitted username | brute force. Plus a separate account lockout: after 10 consecutive failures, lock for 15 min with exponential growth, recorded on the user row |
| `POST /api/auth/password` | 5 per hour | session | |
| `POST /api/broker/credentials` | 10 per min | session | credential stuffing against our own store is pointless, but this bounds accidental loops |
| `GET /api/broker/oauth/start` | 10 per min | session | |
| `GET /api/broker/fyers/callback` | 20 per min | client ip | bound the cost of state-guessing attempts |
| `POST /api/jobs/*`, `POST /api/downloads/*` | 30 per min | session | these enqueue outbound Fyers work, so they are the expensive ones |
| `DELETE /api/**` | 30 per min | session | |
| all `GET /api/**` | 120 per min | session | |
| global fallback, every request | 300 per min | client ip | |

Responses: `429` with `Retry-After`, plus the IETF draft headers `RateLimit-Limit`,
`RateLimit-Remaining`, `RateLimit-Reset` on every response so the SPA can back off gracefully.

Alternative: `slowapi` 0.1.10 (2026-06-13) is a thin, maintained wrapper over `limits` with
decorator ergonomics. It is a fine choice. One caveat to verify before adopting: its published
classifiers currently top out at Python 3.13 while this project targets 3.14.6. It is pure Python so
it will almost certainly work, but confirm rather than assume. `limits` itself already declares
Python >= 3.10 and is the dependency `slowapi` wraps, so using it directly removes the question.

Do not use `asgi-ratelimit` (0.10.0, last released 2022-12-07, unmaintained).

### 6.2 Outbound: the Fyers rate limiter, a different problem entirely

This is not defensive, it is a scheduling constraint. It must never drop a request; it must delay it.

Published limits (`04-request-response-structure.md`):

| Timeframe | Standard | Prime |
|---|---|---|
| Per second | 10 | 10 |
| Per minute | 200 | 600 |
| Per day | 100,000 | 200,000 |

And the rule that changes the design: "The user will be blocked for the rest of the day if the per
minute rate limit is exceeded more than 3 times in the day." A per-minute overshoot is not a
retryable error, it is three strikes from losing the whole trading day. Therefore:

- Target below the ceiling, not at it. Use 8 per second and 170 per minute as configured defaults,
  with the true limits stored as hard caps that the app refuses to exceed even if a user raises the
  configured value.
- Implement as a single process-wide async token bucket (per-second) plus a sliding window
  (per-minute), guarded by an `asyncio.Lock`, with an `asyncio.Semaphore` (4 to 8) capping in-flight
  requests. Ten permits per second issued to fully concurrent requests still produces a burst; the
  semaphore is what smooths it.
- The daily counter must be **persisted in SQLite**, keyed by the IST calendar date, and reloaded on
  startup. An in-memory daily counter resets on every restart, and a scheduler that restarts twice a
  day would happily blow through 100,000.
- On HTTP 429 or Fyers code `-429`, stop the whole bucket, back off exponentially with full jitter,
  and raise a UI-visible alert. Do not retry per-task, because N tasks each backing off
  independently is how you collect the second and third strike.
- On `-8` or `-17` (token expired or invalid), do not retry. Mark the broker connection as needing
  re-auth and pause the affected jobs.
- Also bound by the endpoint's own limits, which are a data-pipeline concern rather than a security
  one: 366 days per expiry-dates call, 100 days per minute-resolution history call, last 30 trading
  days for second resolutions.

Keep this module in `app/brokers/fyers/throttle.py`, keep the inbound limiter in
`app/security/ratelimit.py`, and never import one from the other.

---

## 7. The OAuth redirect URI and the `state` parameter

The Fyers docs (section 6) list as a best practice: "You should send a random value in the state
parameter and verify whether the same value has been returned to you", and "Provide a redirect_uri
which is in your control rather than a public endpoint". Both are load-bearing.

### 7.1 Redirect URI

Preferred: a loopback callback we own.

```
http://127.0.0.1:8000/api/broker/fyers/callback
```

It must match the value registered on the Fyers app byte for byte, including scheme, host spelling
(`127.0.0.1` and `localhost` are different strings to an exact-match validator), port, path, and
trailing slash. Store the registered value in the DB, use that exact stored string when building the
auth URL, and validate on save that it parses and that its host is a loopback address when the app
is in local mode.

Gotcha to design around: Fyers app registration may not accept a plain `http://127.0.0.1:PORT` URL,
and the sample throughout the docs is the Fyers-hosted page
`https://trade.fyers.in/api-login/redirect-uri/index.html`. If the user's app is registered against
a hosted redirect, the `auth_code` lands in their browser at a page we do not control and never
reaches our server. So the UI must support both:

1. Loopback callback (automatic, preferred).
2. Manual paste fallback: the user copies the full redirected URL from the address bar into a field,
   and the backend parses `auth_code` and `state` out of it and runs the identical validation path.
   Same `state` check, same single-use rule. The paste field must be `type="password"`-like in the
   sense that it is never logged and is cleared on submit.

### 7.2 `state`

Server side, single use, session bound, short lived. Do not put the only copy in a cookie.

```sql
CREATE TABLE oauth_state (
  state_hash    BLOB PRIMARY KEY,   -- sha256(raw state)
  session_id_hash BLOB NOT NULL,    -- binds the callback to the session that started the flow
  credential_id TEXT NOT NULL,      -- which broker credential this flow is for
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,      -- created_at + 10 minutes
  used_at       TEXT                -- NULL until consumed, then never reusable
);
```

Start endpoint (`GET /api/broker/oauth/start`, session required, CSRF not applicable to a GET but
the `Origin` / `Sec-Fetch-Site` check still applies):

1. `raw_state = secrets.token_urlsafe(32)`.
2. Insert the row with `sha256(raw_state)`, the current session hash, and a 10 minute expiry.
3. Build the auth URL with `client_id`, the exact stored `redirect_uri`, `response_type=code`, and
   `state=raw_state`.
4. Return the URL as JSON for the SPA to navigate to. Do not 302 from an XHR.

Callback endpoint (`GET /api/broker/fyers/callback`):

1. If `s` is not `ok` or `auth_code` is absent, render a generic failure page. Log the Fyers `code`
   and `message`, never the query string verbatim.
2. Look up `sha256(state)`. Reject, with the same generic message and the same timing, if it is
   missing, expired, already `used_at`, or bound to a different session hash. Use
   `hmac.compare_digest` on any direct comparison.
3. Mark `used_at` in the same transaction that reads it, so a double submit cannot race.
4. Exchange: POST `auth_code` plus `appIdHash = sha256(f"{app_id}:{app_secret}")` to
   `https://api-t1.fyers.in/api/v3/validate-authcode`. The app secret is decrypted in memory for the
   duration of this call only.
5. Encrypt and store `access_token` and `refresh_token`.
6. **303 redirect to `/settings/broker?connected=1`.** This is not cosmetic: it removes the
   `auth_code` from the address bar, from browser history, and from anything that later reads
   `document.referrer`.

Additional hardening:

- `Referrer-Policy: no-referrer` on the callback response so the `auth_code`-bearing URL is never
  sent onward.
- `Cache-Control: no-store` on the callback response.
- Scrub the query string for this path in the uvicorn access log (a logging filter that rewrites the
  path to `/api/broker/fyers/callback?<redacted>`), otherwise the `auth_code` and `state` sit in
  plaintext in the log file.
- The session cookie must be `SameSite=Lax` for this callback to carry a session at all. Restated
  here because changing it to `Strict` later silently breaks step 2.
- PKCE: the Fyers v3 documentation does not describe a `code_challenge` / `code_verifier` flow, so
  PKCE is not available. Do not invent parameters the server will ignore. The single-use
  session-bound `state` is the defense we have.

---

## 8. What must never be logged or returned

### 8.1 The never list

`app_secret`, `access_token`, `refresh_token`, `auth_code`, `appIdHash`, the Fyers PIN, the KEK, the
DEK, raw session ids, CSRF tokens, the local login password, the Argon2 hash string, and the
`Authorization` header value.

### 8.2 API surface

Separate Pydantic models, never one model reused in both directions:

```python
class BrokerCredentialIn(BaseModel):
    app_id: str
    app_secret: SecretStr        # SecretStr so an accidental repr or log line prints '**********'
    redirect_uri: AnyHttpUrl

class BrokerCredentialOut(BaseModel):
    id: str
    app_id: str                  # the client id is semi-public and is needed to build the auth URL
    redirect_uri: AnyHttpUrl
    secret_configured: bool      # a boolean, not a masked value, not a last-4
    connected: bool
    token_expires_at: datetime | None
    updated_at: datetime
```

Never return a masked secret (`sk_live_****abcd`). A mask is an oracle and it tempts the frontend
into round-tripping it back on save. Optional and acceptable if the user asks for a "did I paste the
right one" check: a `secret_fingerprint` of the first 8 hex characters of `sha256(secret)`. The
Fyers secret is high entropy so this is not brute forceable, but it is still information disclosure,
so it should be off by default.

The Fyers app secret and tokens must never appear in any response body, any header, any WebSocket
frame, or any file the "export diagnostics" feature produces. Add a test that walks the full OpenAPI
schema and fails if any response model contains a field in the never list.

### 8.3 Logging

- Install a `logging.Filter` on the root logger that regex-redacts, on both `record.msg` and
  `record.args`: `access_token`, `refresh_token`, `auth_code`, `app_secret`, `appIdHash`,
  `authorization`, and the JWT shape `eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.`. Fyers tokens are
  JWTs, so that last pattern catches them even when they appear in an unexpected field.
- Configure the `httpx` client with event hooks that log method, host, path and status only. Never
  log headers, never log request or response bodies at INFO. If body logging is needed for
  debugging, it goes behind a `FYERS_DEBUG_BODIES` flag that is off by default and that prints a
  warning when enabled.
- Exception handlers must not return `str(exc)` to the client in production. Generate a correlation
  id, log the full traceback server side (after redaction), return `{"detail": "internal error",
  "correlation_id": "..."}`. The Fyers error body can and does contain tokens; a naive
  `raise HTTPException(500, str(exc))` is how it reaches the browser console.
- Set `uvicorn` access log format to exclude the query string, or filter it for the callback path.
- Never write secrets to DuckDB, to a Parquet or CSV export, or to a chart payload. DuckDB holds
  market data only.

### 8.4 Token lifetime handling

The Fyers access token is a daily token, and the refresh token has a 15 day validity with a
documented note that it "will be discontinued from 1st April". The scheduler must therefore:

- Store `token_obtained_at` and a computed `token_expires_at`, and treat a token as stale before
  Fyers does.
- Attempt refresh (while the flow exists) ahead of expiry, not on the first `-8` error.
- When refresh is unavailable or fails, transition the broker connection to `needs_reauth`, pause
  dependent jobs rather than failing them, and surface a UI banner plus a desktop notification. A
  scheduler that silently stops downloading for a week is worse than one that shouts.
- Never persist the PIN unless the user explicitly enables refresh, and encrypt it exactly like the
  app secret when they do.

---

## 9. Security headers

Use `secure` 2.0.1 (2026-04-22, Python >= 3.10) or a 30-line middleware. Either is fine; the header
set matters more than the library.

| Header | Value | Note |
|---|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'` | Production static mount only. See below |
| `X-Content-Type-Options` | `nosniff` | |
| `Referrer-Policy` | `no-referrer` | Keeps the OAuth `auth_code` URL out of any Referer header |
| `X-Frame-Options` | `DENY` | Belt and braces with `frame-ancestors 'none'` |
| `Permissions-Policy` | `geolocation=(), camera=(), microphone=(), payment=(), usb=()` | |
| `Cross-Origin-Opener-Policy` | `same-origin` | |
| `Cross-Origin-Resource-Policy` | `same-origin` | |
| `Cross-Origin-Embedder-Policy` | `require-corp` | Only if nothing third party is embedded. Verify against openalgo-charts before enabling |
| `Cache-Control` | `no-store` | On every `/api/**` response |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | **Only when actually serving HTTPS.** Never on `http://localhost` |

The HSTS point is not pedantry. Setting HSTS on a `localhost` response poisons the `localhost` origin
in that browser for a year for every other local development server the user runs, and the failure
mode is confusing and hard to trace back. Gate it on `SETTINGS.env == "production" and request.url.scheme == "https"`.

CSP in dev: the Vite dev server needs `'unsafe-inline'` and `'unsafe-eval'` for HMR, and a
`connect-src` including `ws://localhost:5173`. Do not weaken the production policy to accommodate
dev. Apply the strict CSP only on the production static-file mount and skip the header entirely in
dev. If openalgo-charts turns out to inject inline styles, prefer a nonce or hash over
`style-src 'unsafe-inline'`; check the local checkout at
`/Users/openalgo/AIBootcamp2026/Day26/openalgo-charts` before finalising, and add `'unsafe-inline'`
to `style-src` only if a nonce genuinely cannot be threaded through.

Tailwind 4 via the Vite plugin emits a real CSS file, so `style-src 'self'` should hold.

---

## 10. SQLite hardening

### 10.1 Pragmas

Apply on every connection, because several of these are per-connection and not persistent:

```python
def configure(conn):
    conn.execute("PRAGMA journal_mode = WAL")        # persistent, set once, but harmless to repeat
    conn.execute("PRAGMA synchronous = NORMAL")      # safe with WAL for this workload
    conn.execute("PRAGMA foreign_keys = ON")         # PER CONNECTION, off by default, easy to miss
    conn.execute("PRAGMA busy_timeout = 5000")       # scheduler and API contend for the same file
    conn.execute("PRAGMA secure_delete = ON")        # overwrite freed pages so old ciphertext and
                                                     # old plaintext metadata do not linger
    conn.execute("PRAGMA trusted_schema = OFF")      # do not run schema-embedded functions
    conn.execute("PRAGMA cell_size_check = ON")      # detect a malformed or tampered db file early
    conn.execute("PRAGMA foreign_keys = ON")
```

`journal_mode = WAL` is the right call for this app because the APScheduler jobs write while the API
reads. It brings two files along: `expirymanager.db-wal` and `expirymanager.db-shm`.

`secure_delete = ON` is genuinely relevant rather than decorative: without it, when a credential row
is deleted or a token rotated, the old ciphertext stays in a free page and appears in a raw file
dump. It costs write throughput on a table we write rarely.

### 10.2 File permissions

- Call `os.umask(0o077)` as the first statement in the entry point, before anything opens a file.
  SQLite creates the `-wal` and `-shm` sidecars itself, and they inherit from the umask. Setting
  0600 on the main DB after the fact does not fix the sidecars.
- Data directory: `~/.expirymanager` (or `~/Library/Application Support/ExpiryManager` on macOS),
  created with mode 0700.
- `expirymanager.db`, `expirymanager.db-wal`, `expirymanager.db-shm`, `master.key`: all 0600.
- At startup, stat every one of them and hard-fail with a remediation message if `st_mode & 0o077`.
  Failing closed on a loose key file is what sshd does and users understand the pattern.
- Refuse to run if the data directory resolves under a known cloud sync root. WAL plus a file syncer
  is a corruption generator quite apart from the key leak.

### 10.3 Is SQLCipher worth it?

**No, not as the primary control.** The reasoning, so the decision can be revisited on evidence:

1. The fields that matter are already AEAD-encrypted under a key that does not live in the database.
   SQLCipher's marginal contribution is encrypting the non-secret catalog metadata (symbol lists,
   expiry dates, job history), which is not sensitive.
2. It does not answer the hard question. SQLCipher needs a passphrase or key at `open` time, so it
   lands back in exactly the "where does the key live" problem from section 2.6, with the same three
   candidates and the same reboot constraint. It moves the problem, it does not solve it.
3. Packaging cost. The Python route is `sqlcipher3-binary` or `pysqlcipher3`, which need a custom
   SQLite build and are less consistently maintained than `cryptography`. On Python 3.14.6 with a
   zero-config install promise, adding a compiled non-standard SQLite is a real risk to the "it just
   works" requirement, and it also complicates using the stdlib `sqlite3` driver and SQLAlchemy's
   default dialect.
4. It does nothing against T3, which is the dominant threat on a local machine.
5. FileVault, on by default on modern macOS, already covers T4 for the entire file including the
   metadata SQLCipher would protect.

Revisit if any of these become true: the DB will live on a shared or removable volume, the metadata
itself becomes sensitive (for example if it starts carrying positions or P&L), or a compliance
requirement demands full-database encryption as a checkbox.

### 10.4 DuckDB

DuckDB 1.5.5 is current. DuckDB gained data-at-rest encryption in 1.4.0 (AES-256-GCM, block level,
covering the database file, the WAL and temporary files) via
`ATTACH 'file.duckdb' AS db (ENCRYPTION_KEY '...')`, with much better throughput when the `httpfs`
extension's OpenSSL backend is loaded.

Recommendation: **do not encrypt the DuckDB store.** It holds public market data (OHLCV, open
interest, greeks) with no confidentiality requirement, it is the large-volume hot path where the
encryption overhead actually matters, and encrypting it would create a second key-management problem
for no security gain. Note the capability in the design so it can be switched on if the store ever
starts holding user positions or strategy output.

Do apply the same file permission discipline: the DuckDB file lives in the 0700 data directory.

---

## 11. Dependency list (verified 2026-09-09)

Security-relevant packages, versions confirmed from the PyPI JSON API.

| Package | Version | Released | Python | Role |
|---|---|---|---|---|
| `cryptography` | 50.0.1 | 2026-08-25 | >=3.9 | AES-256-GCM field encryption, DEK wrapping, Argon2id KDF for the passphrase provider |
| `argon2-cffi` | 25.1.0 | 2025-06-03 | >=3.8 | local login password hashing |
| `keyring` | 25.7.0 | 2025-11-16 | >=3.9 | optional KEK provider |
| `limits` | 5.8.0 | 2026-02-05 | >=3.10 | inbound rate limiting |
| `secure` | 2.0.1 | 2026-04-22 | >=3.10 | security headers |
| `fastapi` | 0.141.1 | 2026-07-29 | >=3.10 | |
| `starlette` | 1.6.0 | 2026-08-08 | >=3.10 | pulled by FastAPI (`starlette>=0.46.0`), provides `TrustedHostMiddleware`, `CORSMiddleware` |
| `uvicorn` | 0.52.4 | 2026-08-19 | >=3.10 | bind to 127.0.0.1 |
| `pydantic` | 2.13.5 | 2026-08-28 | >=3.9 | `SecretStr`, request/response model separation |
| `sqlalchemy` | 2.0.52 | 2026-08-11 | | |
| `apscheduler` | 3.11.3 | 2026-06-28 | >=3.8 | the unattended scheduler that constrains the KEK choice |
| `httpx` | 0.28.1 | 2024-12-06 | >=3.8 | outbound Fyers client |
| `duckdb` | 1.5.5 | 2026-07-22 | >=3.10 | market data store |

Optional, only if chosen over the in-house equivalent:

| Package | Version | Released | Note |
|---|---|---|---|
| `slowapi` | 0.1.10 | 2026-06-13 | wrapper over `limits`. Verify on Python 3.14, classifiers stop at 3.13 |
| `fastapi-csrf-protect` | 1.0.7 | 2025-09-16 | signed double-submit CSRF, if you prefer not to hand-roll |
| `pwdlib` | 0.3.1 | 2026-08-12 | maintained passlib replacement, only if multi-scheme migration is wanted |
| `bcrypt` | 5.0.0 | 2025-09-25 | only if Argon2 is ruled out |

Do not use:

| Package | Version | Why not |
|---|---|---|
| `passlib` | 1.7.4 (2020-10-08) | unmaintained for six years, breaks against modern `bcrypt` |
| `asgi-ratelimit` | 0.10.0 (2022-12-07) | unmaintained |
| `python-jose` | 3.5.0 | not needed, we are not issuing JWTs |
| `PyJWT` | 2.13.0 | good library, wrong shape for opaque local sessions |

---

## 12. Implementation checklist

Ordered so that each item is testable when it lands.

1. `os.umask(0o077)` as the first line of the entry point.
2. Data directory bootstrap: 0700 dir, sync-root warning, permission audit that fails closed.
3. `app/security/crypto.py`: `KekProvider` protocol, `KeyfileKekProvider`, `CryptoBox` with
   `encrypt(table, column, row_id, plaintext)` and `decrypt(table, column, row_id, blob)`.
   Round-trip tests, AAD-mismatch tests, tampered-tag tests, wrong-magic tests.
4. `crypto_key` table plus DEK generation on first run. Rewrap test: switch provider, assert no
   field ciphertext changed.
5. SQLite pragma configuration on the SQLAlchemy `connect` event, with a test asserting
   `foreign_keys` is on for a fresh connection.
6. `app_user` table plus Argon2id hashing, threadpool-offloaded verify, timing-equalised failure,
   `check_needs_rehash` on success.
7. `session` table, opaque id, SHA-256 storage, cookie flags from config, rotation on login.
8. CSRF middleware with `Sec-Fetch-Site`, `Origin`, and synchronizer-token layers. Test that every
   non-safe route is covered.
9. `TrustedHostMiddleware` outermost. Test that a request with `Host: evil.com` is rejected.
10. Security headers middleware. Test that HSTS is absent in dev and present in prod-over-https.
11. Inbound rate limiter dependency plus the per-route table in section 6.1.
12. Redaction logging filter plus an httpx event hook that logs no headers or bodies. Test with a
    fake JWT in a log call.
13. `oauth_state` table, start endpoint, callback endpoint, 303 to a clean URL, access-log scrub.
14. Manual-paste OAuth fallback sharing the same validation path.
15. Outbound Fyers throttle in `app/brokers/fyers/throttle.py` with the SQLite-persisted daily
    counter, entirely separate from item 11.
16. OpenAPI schema test: fail the build if any response model exposes a never-list field.
17. Provider-switch UI in Settings with the honest wording for passphrase mode.

---

## 13. Open questions for the design phase

1. Does the Fyers app registration accept `http://127.0.0.1:8000/...` as a redirect URI, or must it
   be a public HTTPS URL? This decides whether the loopback callback is the default path or whether
   the manual-paste fallback is the primary flow. It needs an account to verify and cannot be
   settled from the docs alone.
2. What actually happens to the refresh token flow after 2026-04-01 under the SEBI retail algo
   trading changes? The docs carry the note "Refresh token will be discontinued from 1st April"
   without saying what replaces it. If a daily interactive login (TOTP or similar) becomes mandatory,
   the "unattended after reboot" requirement is bounded by the broker regardless of how we store the
   key, and the scheduler needs a first-class `needs_reauth` state with notifications rather than a
   retry loop.
3. Should the app support more than one local user? Everything above assumes one. Multi-user would
   need per-user DEKs and per-user credential rows, which is a schema decision better made now than
   later.
4. Is the app packaged as a `LaunchAgent` (user session, keyring viable) or a `LaunchDaemon` (system
   context, key file mandatory)? Section 2.6 assumes the daemon case is possible, which is what makes
   the key file primary. If the product is definitively an interactive app the user launches, the
   keyring becomes a reasonable default instead.
5. Does openalgo-charts inject inline styles or use `eval`-based code paths? This determines whether
   `style-src 'self'` and `script-src 'self'` hold, or whether a nonce needs threading through the
   template.
6. Is there any requirement to run ExpiryManager on a machine other than the user's own (a VPS, a
   shared research box)? If yes, TLS, HSTS, a non-loopback bind, and the SQLCipher question all
   reopen.
