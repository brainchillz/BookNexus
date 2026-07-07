#!/usr/bin/env bash
set -euo pipefail

# Prefer docker compose v2 (plugin), fall back to docker-compose v1
if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "Error: neither 'docker compose' nor 'docker-compose' found." >&2
    exit 1
fi

# Create .env from example if it doesn't exist yet
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "  .env created from .env.example."
    echo "  Edit it and set SECRET_KEY, then re-run this script."
    echo ""
    exit 1
fi

if ! grep -qE "^SECRET_KEY=.+" .env || grep -qE "^SECRET_KEY=change-this" .env; then
    echo "Error: set SECRET_KEY in .env first. Generate one with:" >&2
    echo '  python3 -c "import secrets; print(secrets.token_hex(32))"' >&2
    exit 1
fi

# The SQLite database lives in ./data on the host, written by the container
# user (uid 10001). Create it with the right ownership up front.
mkdir -p data backups
if [ "$(id -u)" -eq 0 ]; then
    chown 10001:10001 data
elif [ ! -w data ] || ! chown 10001:10001 data 2>/dev/null; then
    echo "Note: could not chown ./data to uid 10001 — if the app can't write"
    echo "the database, run: sudo chown 10001:10001 data"
fi

echo "Building and starting BookNexus..."
$DC up -d --build

echo ""
PORT=$(grep -E '^APP_PORT=' .env | cut -d= -f2 || true)
PORT=${PORT:-8000}
echo "  App is running at https://localhost:${PORT}"
    echo "  (HTTPS is on by default with a self-signed certificate — your"
    echo "   browser will warn once; manage certificates in Settings)"
echo "  First run? It will walk you through creating the admin account."
echo ""
echo "Useful commands:"
echo "  $DC logs -f          # stream logs"
echo "  $DC ps               # container status"
echo "  $DC down             # stop (your library stays in ./data)"
