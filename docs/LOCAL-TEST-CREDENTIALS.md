# Local test credentials and the constraints they impose

The developer has a working Fyers app registered. Its credentials live OUTSIDE this
repository at:

    /Users/openalgo/AIBootcamp2026/Day26/.cred      (mode 0600, never commit, never copy in)

Format, three lines of KEY=VALUE:

    FYERS_API_KEY=<app id, shape XXXXXXXXX-100>
    FYERS_API_SECRET=<app secret>
    REDIRECTION_URL=https://127.0.0.1:8000/fyers/callback

These are REAL credentials for a live broker account. Rules for every agent and every
test:

- Never write the key or the secret into any file in this repository, any test fixture,
  any log line, any commit, any error message, or any API response.
- Never echo the secret to stdout. Load it from the file at test time only.
- The repository .gitignore must exclude .cred, *.cred, data/, *.duckdb, *.sqlite3 and
  the generated TLS material before the first commit.
- These values are for seeding the running app through its own credential API or UI, the
  same path a real user takes. They are not application configuration and must not be
  read by application code at runtime.

## The registered redirect URI drives three hard requirements

The redirect URI is registered on the Fyers dashboard as
`https://127.0.0.1:8000/fyers/callback`. Fyers matches it exactly, so the application has
no freedom here.

1. The FastAPI backend must listen on 127.0.0.1 port 8000.
2. It must serve HTTPS, not HTTP. Note the scheme. A plain HTTP server on 8000 will fail
   the OAuth callback.
3. The callback route must be exactly `/fyers/callback`, not `/api/auth/callback` or any
   other path. Mount it at the root, outside any `/api` prefix.

Because the project is zero config, the app must generate its own TLS material on first
run rather than asking the developer to produce a certificate:

- On startup, if no certificate exists in the application data directory, generate a
  self-signed certificate and key for CN=127.0.0.1 with subjectAltName IP:127.0.0.1 and
  DNS:localhost, valid for about one year, written mode 0600.
- Start uvicorn with that key and certificate.
- Print the browser trust warning once at startup in plain text, since the developer will
  see a certificate warning on first visit and needs to know it is expected.
- Regenerate automatically when the certificate is missing or expired.

The Vite dev server proxy must therefore target an HTTPS origin whose certificate is
self-signed. Configure the proxy with secure disabled for the local target only, and keep
cookies working across the two dev origins.

## What can and cannot be validated without the developer

The OAuth flow needs an interactive Fyers login in a browser, using the account password
and TOTP. An agent cannot and must not attempt that.

Automatically testable:
- appIdHash computation, SHA-256 over "api_key:api_secret", against a known vector.
- Credential round trip: store through the API, encrypt at rest, read back, confirm the
  ciphertext in SQLite is not the plaintext and that no endpoint ever returns the secret.
- The authorisation URL the app builds, including the state parameter.
- Certificate generation and that the server answers HTTPS on 127.0.0.1:8000.
- Every pipeline component against recorded or synthetic responses.

Needs the developer, once, interactively:
- Clicking Login, completing the Fyers login, and letting the callback land. After that
  the stored access token makes the live download path testable end to end.

Design the app so that first interactive login is the only manual step, and so that the
refresh token flow keeps the scheduler running unattended afterwards.
