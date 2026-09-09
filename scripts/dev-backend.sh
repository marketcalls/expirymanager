#!/usr/bin/env bash
# Start the backend in development: https://127.0.0.1:8000 with reload.
#
# The host, port and https scheme are fixed by the registered Fyers redirect URI
# (https://127.0.0.1:8000/fyers/callback), which Fyers matches exactly. Do not parameterise them.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root/backend"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. See https://docs.astral.sh/uv/ for installation." >&2
    exit 1
fi

uv sync
exec uv run expirymanager --reload "$@"
