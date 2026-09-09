#!/usr/bin/env bash
# Start the Vite dev server for ExpiryManager.
#
# The dev origin is https://127.0.0.1:5173 and it proxies /api to https://127.0.0.1:8000.
# Both are 127.0.0.1 by design: cookies ignore the port but not the host, so the session
# cookie the Fyers OAuth callback sets on the backend is visible to this origin.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here/../frontend"

tls_dir="$HOME/.expirymanager/tls"
if [ ! -f "$tls_dir/server.crt" ]; then
  echo "Note: no certificate at $tls_dir."
  echo "The dev server will fall back to http, which breaks the shared session cookie."
  echo "Run the backend once (scripts/dev-backend.sh) to generate the pair, then restart."
fi

if [ ! -d node_modules ]; then
  npm install
fi

exec npm run dev -- "$@"
