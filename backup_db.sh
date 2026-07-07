#!/usr/bin/env bash
# Nightly database backup. Uses SQLite's online-backup API (safe while the
# app is running) to snapshot ./data/books.db into ./backups/, gzipped and
# timestamped, keeping 14 days.
#
# Install via /etc/cron.d/booknexus-backup, e.g.:
#   17 3 * * * root /path/to/BookNexus/backup_db.sh >> /var/log/booknexus-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
ts=$(date +%Y%m%d-%H%M%S)

get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed "s/^'//; s/'\$//"; }

DB_PATH=$(get_env DB_PATH)
DB_PATH=${DB_PATH:-data/books.db}

python3 - "$DB_PATH" "backups/books-${ts}.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
EOF
gzip "backups/books-${ts}.db"
echo "$(date -Is) backed up ${DB_PATH} -> backups/books-${ts}.db.gz"

find backups -name 'books-*.db.gz' -mtime +14 -delete

# Off-host copy. Set in .env:
#   BACKUP_REMOTE=user@host:path/   (rsync-over-ssh destination, key auth)
#   BACKUP_REMOTE_KEY=/path/to/ssh/key   (optional; default ssh key otherwise)
# The remote is additive-only: local pruning is NOT mirrored, so the
# destination accumulates full history.
remote=$(get_env BACKUP_REMOTE)
if [ -n "$remote" ]; then
    key=$(get_env BACKUP_REMOTE_KEY)
    rsync -az -e "ssh ${key:+-i $key} -o StrictHostKeyChecking=accept-new" \
        backups/ "$remote"
    echo "$(date -Is) synced backups/ -> $remote"
fi

# Publish the dataset to a public GitHub data repo. Set in .env:
#   DATA_REPO_SSH=git@github.com:<user>/<data-repo>.git
#   DATA_REPO_KEY=/path/to/deploy/key   (write-access deploy key)
# Commits/pushes only when the data actually changed. The synopsis column
# is excluded by export_data.py (public repo; re-fetched lazily anyway).
data_repo=$(get_env DATA_REPO_SSH)
if [ -n "$data_repo" ]; then
    data_key=$(get_env DATA_REPO_KEY)
    export GIT_SSH_COMMAND="ssh ${data_key:+-i $data_key} -o StrictHostKeyChecking=accept-new"
    clone=data-repo
    if [ ! -d "$clone/.git" ]; then
        git clone -q "$data_repo" "$clone"
    fi
    git -C "$clone" pull -q --ff-only 2>/dev/null || true   # fails on empty repo; fine
    DB_PATH="$DB_PATH" python3 export_data.py --csv > "$clone/books.csv"
    DB_PATH="$DB_PATH" python3 export_data.py --sql > "$clone/books.sql"
    git -C "$clone" add -A
    if [ -n "$(git -C "$clone" status --porcelain)" ]; then
        git -C "$clone" -c user.name="booknexus-backup" -c user.email="backup@localhost" \
            commit -q -m "Data export $(date +%F)"
        git -C "$clone" branch -M main
        git -C "$clone" push -q -u origin main
        echo "$(date -Is) pushed dataset to $data_repo"
    else
        echo "$(date -Is) dataset unchanged; nothing to push"
    fi
fi
