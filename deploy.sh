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
    echo "  Edit it and fill in all required values, then re-run this script."
    echo ""
    exit 1
fi

# Warn about any unset required variables
required=(MYSQL_ROOT_PASSWORD DB_PASSWORD SECRET_KEY)
missing=()
for var in "${required[@]}"; do
    if ! grep -qE "^${var}=.+" .env; then
        missing+=("$var")
    fi
done
# Admin credential is optional: without one, the app shows a first-run
# setup wizard at /setup — complete it immediately after deploying.
if [ ${#missing[@]} -gt 0 ]; then
    echo "Error: the following required variables are not set in .env:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    exit 1
fi

# Optionally refresh the seed data before starting. When SEED_DATA_URL is
# set in .env, the latest books.sql is fetched from it (e.g. a nightly
# data-repo export); otherwise the books.sql shipped in this directory is
# used as-is. Either way it is only imported when the MySQL volume is
# brand new.
SEED_URL=$(grep -E '^SEED_DATA_URL=' .env | cut -d= -f2- || true)
if [ -n "${SEED_URL:-}" ]; then
    if curl -fsSL --connect-timeout 10 "$SEED_URL" -o books.sql.new && [ -s books.sql.new ]; then
        mv books.sql.new books.sql
        echo "Seed data refreshed from $SEED_URL"
    else
        rm -f books.sql.new
        echo "Warning: could not fetch $SEED_URL — using existing local books.sql"
    fi
fi
if [ ! -f books.sql ]; then
    touch books.sql
    echo "Note: no books.sql present — a fresh database will start empty"
fi

echo "Building and starting Book Library..."
$DC up -d --build

echo ""
PORT=$(grep -E '^APP_PORT=' .env | cut -d= -f2)
PORT=${PORT:-8000}
echo "  App is running at http://localhost:${PORT}"
echo ""
echo "Useful commands:"
echo "  $DC logs -f          # stream logs"
echo "  $DC ps               # container status"
echo "  $DC down             # stop and remove containers"
echo "  $DC down -v          # stop and remove containers + wipe database"
