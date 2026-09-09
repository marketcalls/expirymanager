# First login

The shortest correct path from a cold checkout to a completed Fyers OAuth login.

Read this once end to end before you start. Step 2 is the one that fails silently if you get it
wrong, and it fails much later with an error that reads like a different problem.

---

## 0. What you need

* `uv` on the path, and Python 3.14 available to it.
* `npm`, only for the one frontend build in step 3.
* A Fyers account with API access, and an app created on the Fyers API dashboard.
* Port 8000 on 127.0.0.1 free. The port is not configurable. See "Port 8000 is already in use"
  under Troubleshooting.

Nothing else. There is no `.env`, no config file to edit and no environment variable to set. The
application keeps everything under `~/.expirymanager`, which it creates itself with mode 0700.

---

## 1. Register the redirect URL on the Fyers dashboard

Open the Fyers API dashboard, open your app, and set the redirect URL to exactly this:

```
https://127.0.0.1:8000/fyers/callback
```

Copy it. Do not retype it. Fyers compares this string character for character against the value
the application sends, and there is no way for the application to warn you about a mismatch: a
wrong redirect URL is rejected by Fyers during the login with a message that reads like a wrong
app id.

Three things people get wrong here:

* `localhost` instead of `127.0.0.1`. They are different strings to Fyers even though they resolve
  to the same machine.
* `http` instead of `https`. The application serves HTTPS only.
* A trailing slash. `https://127.0.0.1:8000/fyers/callback/` is a different string.

Keep the app id and the app secret from that page to hand. You will paste both in step 5.

---

## 2. Build the frontend

From the repository root:

```
cd frontend
npm install
npm run build
```

This writes `frontend/dist`. The backend serves it automatically when it is present, which is what
lets the whole application run from one origin on port 8000 with no second process. You do not
need the Vite dev server for a first login.

---

## 3. Start the application

```
cd backend
uv run expirymanager
```

The first run prints something like this, and creates `~/.expirymanager` along with a self signed
TLS certificate:

```
ExpiryManager 1.0.0
Data directory: /Users/you/.expirymanager
TLS: this server uses a self-signed certificate it generated for 127.0.0.1.
     Your browser will warn on the first visit. That warning is expected and is not a
     fault. Choose to proceed, once, for https://127.0.0.1:8000.
Listening on https://127.0.0.1:8000
```

Leave it running. Stop it later with Ctrl+C.

If you want to check the data directory, the logging and the certificate without starting the
server, run `uv run expirymanager --check` instead. It prepares everything, reports it and exits.

---

## 4. Open the browser and accept the certificate warning once

Go to:

```
https://127.0.0.1:8000
```

Your browser will refuse the page on the first visit. This is expected and it is not a fault.

The certificate is generated on this machine, for this machine, and it is signed by itself. No
public authority has vouched for it, because no public authority can vouch for 127.0.0.1. What you
see is the browser reporting exactly that.

* Chrome and Edge: "Your connection is not private", error code
  `NET::ERR_CERT_AUTHORITY_INVALID`. Click "Advanced", then "Proceed to 127.0.0.1 (unsafe)".
* Firefox: "Warning: Potential Security Risk Ahead". Click "Advanced", then "Accept the Risk and
  Continue".
* Safari: "This Connection Is Not Private". Click "Show Details", then "visit this website", and
  confirm.

Accept it now, before you start the login. This matters more than it looks. When Fyers sends your
browser back to `https://127.0.0.1:8000/fyers/callback`, that return is a plain navigation. If the
browser has not yet been told to trust this certificate, it blocks the return instead of
completing your login, and the tab sits on a warning page holding an auth code that expires. Step
7 tells you how to recover from that, but accepting the warning here avoids it entirely.

The certificate is valid for about one year, covers `IP:127.0.0.1` and `DNS:localhost`, and lives
in `~/.expirymanager/tls` with mode 0600. It is regenerated automatically when it nears expiry.

---

## 5. Create your local passcode, then store the Fyers app registration

The application opens on the setup screen because there is no account yet.

1. Choose a username and a passcode. The passcode must be at least 12 characters. This is the lock
   on this application on this machine. It is not your Fyers password and it is never sent to
   Fyers.
2. You are signed in immediately after setup. Go to Settings.
3. In the Fyers panel, fill in:
   * "Fyers app id", for example `ABCD1234-100`. Paste only the app id. If you paste
     `appid:secret` joined by a colon, the form tells you so.
   * "Fyers app secret".
   * "Fyers plan". Leave it on Standard unless you pay for Prime. This sets the outbound request
     governor to 8 per second and 170 per minute against the Standard limits of 10 and 200.
   * The redirect URL is shown with a copy button. It is already the registered value. Use the
     copy button to fill the Fyers dashboard in step 1 if you have not done that yet.
4. Save.

The app secret is encrypted with the local key before it is written, and no screen and no API
response in this application ever reads it back. Settings shows a boolean, not a mask.

---

## 6. Connect

Still in Settings, press "Connect to Fyers".

What happens, in order:

1. The application mints a single use random value called the state, stores only its SHA-256, and
   binds it to your current browser session. It expires in ten minutes.
2. Your browser opens the Fyers authorize page:
   `https://api-t1.fyers.in/api/v3/generate-authcode` carrying `client_id`, `redirect_uri`,
   `response_type=code` and `state`.
3. You log in to Fyers and approve the app.
4. Fyers redirects your browser to `https://127.0.0.1:8000/fyers/callback` with an auth code and
   the same state.
5. The application checks the state, exchanges the auth code for an access token, encrypts the
   token and stores it, then redirects you to `/settings?broker=connected`.

You end up back on the Settings page with the connection showing as connected. That is the login.

The auth code never appears on a page you can read, because the callback answers with a redirect
rather than a body. That keeps the code out of your browser history and out of the referrer of
anything the page would have loaded.

---

## 7. If the redirect does not come back

The usual cause is the certificate warning in the returning tab: you declined it, or you never
accepted it in step 4. Fyers did its part; your browser stopped at the door.

You do not have to start over, as long as you are inside the ten minute window.

1. Go to the tab Fyers sent you to, the one showing the warning page.
2. Copy the entire address from the address bar. It starts with
   `https://127.0.0.1:8000/fyers/callback` and carries a long `auth_code` and a `state`.
3. Back in Settings, under "The return did not land", press "Paste the URL", paste the whole
   address, and submit.

That runs the identical verification the automatic return runs: the same state row, the same
single use, the same session binding. It is a different way in, not a weaker one.

Other causes, and what each looks like:

* **You accepted the warning but landed on a page saying the connection failed.** Read the reason
  on the Settings page. The five reasons the callback can send are `state_invalid`,
  `login_failed`, `exchange_failed`, `no_credentials` and `unexpected`, and Settings explains each
  one in words.
* **The reason is `state_invalid`.** The login took longer than ten minutes, or the tab you
  finished in is not the browser session that started it, or that state was already used. Press
  Connect again and finish it in the same browser without waiting.
* **The reason is `exchange_failed`.** Fyers returned an auth code but refused to exchange it. In
  practice this is a wrong app secret or a redirect URL that does not match the registration
  character for character. Recheck step 1 and step 5.
* **You never reached the Fyers login page at all.** The app id is wrong or the app is not
  approved on the Fyers dashboard.

Starting over is always safe. Press Connect again. The old state is dead either way: states are
single use and are pruned every time a new login starts.

---

## 8. Confirm the login worked

Three checks, in increasing order of how much they prove.

**The screen.** The connection badge in Settings reads Connected, and the top bar shows the token
chip with an expiry time.

**The bootstrap endpoint.** From a terminal:

```
curl -k https://127.0.0.1:8000/api/v1/bootstrap
```

You want `"has_credentials": true`, `"broker_connected": true`, `"token_state": "valid"` and
`"needs_reauth": false`. The `-k` is there because the certificate is self signed; it is the
terminal equivalent of the warning you accepted in step 4.

**A real request.** In Settings, press the button that tests the connection. It spends exactly one
governed request against the Fyers expiry-dates endpoint and reports the latency and how much of
today's budget of 100000 requests you have used. This is the only check that proves the stored
token is actually accepted by Fyers.

---

## 9. What happens to the token afterwards

The access token is destroyed on a schedule at 03:00 IST every day. This is deliberate and it is
not a bug or an expiry you can extend. SEBI discontinued the refresh token flow from 1 April 2026,
so there is no way to renew a token without you logging in again, and a planned nightly logout is
safer than a token that dies in the middle of a download.

Work in flight is not lost. Running jobs checkpoint, park in the awaiting authentication state,
and resume from the exact task they stopped at after your next login. Log in again the next
morning from Settings, the same Connect button, and parked work restarts on its own.

---

## Troubleshooting

**Port 8000 is already in use.**

```
ExpiryManager 1.0.0
...
ERROR uvicorn.error: OSError(48, "error while attempting to bind on address
      ('127.0.0.1', 8000): address already in use")
```

The port is fixed by the registered Fyers redirect URI and cannot be changed, so the other process
has to move. Find it:

```
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

Stop whatever that is, then start ExpiryManager again. Note that the application itself starts up
fully before uvicorn tries to bind, so you will see "Application startup complete" in the log just
above this error. That line is not a sign that the server is serving.

**Another ExpiryManager process holds the lock.**

```
ExpiryManager cannot start.
Another ExpiryManager process holds /Users/you/.expirymanager/expirymanager.lock. Likely causes:
the app is already running in another terminal, a previous run is still shutting down, or a duckdb
CLI or database browser is open against market.duckdb. Only one process may hold the data
directory.
```

Exactly one process may hold the data directory, because DuckDB allows one read-write process and
a second scheduler would run every scheduled job twice. Close the other terminal, close any DuckDB
CLI or database browser pointed at `~/.expirymanager/market.duckdb`, and try again. A previous run
that was stopped a second ago may still be exiting; the lock is released when its process ends,
with no file to delete by hand.

**The server starts on plain HTTP instead of HTTPS.** The banner says so:

```
TLS: no certificate was found and none could be generated, so the server is starting on
     plain HTTP. Fyers OAuth will not complete against an http redirect URI. Restart once
     the certificate can be created.
```

The login cannot work in this state, because the registered redirect URI is https. Check that
`~/.expirymanager/tls` is writable and look in `~/.expirymanager/logs` for the reason. Deleting
`~/.expirymanager/tls/server.key` and `server.crt` and restarting regenerates both.

**The application refuses to start because the data directory is inside a cloud sync folder.** A
sync client copying a write ahead log out from under an open database corrupts it, and it would
upload the key file as well. Set `EXPIRYMANAGER_HOME` to a directory outside any synced folder and
start again.

**You are using the Vite dev server on port 5173.** That works: run `npm run dev` in `frontend`
and browse `https://127.0.0.1:5173`. The dev server proxies `/api` to the backend and the login
still completes, because the callback lands on port 8000 and cookies are scoped to the host rather
than the port. Accept the certificate warning on both origins, 5173 and 8000, before you start the
login.

---

## What never leaves this machine

Your app secret and your access token are encrypted with a key in `~/.expirymanager/master.key`,
mode 0600. Neither is logged, neither is returned by any endpoint, and neither is written anywhere
in the repository. `.gitignore` excludes the data directory, both databases, the key file and the
TLS material, so a stray copy inside the checkout is still not committable. If you keep your live
credentials in a file, keep it outside the repository.
