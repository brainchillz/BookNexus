#!/usr/bin/env bash
# Nightly database backup. Dumps the Docker MySQL (and the bare-metal MySQL
# too when BACKUP_BAREMETAL=true in .env) into ./backups/, gzipped and
# timestamped, keeping 14 days.
#
# Install via /etc/cron.d/booklibrary-backup, e.g.:
#   17 3 * * * root /home/<user>/BookDatabaseApp/backup_db.sh >> /var/log/booklibrary-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
ts=$(date +%Y%m%d-%H%M%S)

get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed "s/^'//; s/'\$//"; }

# Docker instance (skipped when the compose stack isn't running)
if docker compose ps -q db 2>/dev/null | grep -q .; then
    docker compose exec -T db sh -c \
        'exec mysqldump -u books_user -p"$MYSQL_PASSWORD" --single-transaction --no-tablespaces books' \
        | gzip > "backups/books-docker-${ts}.sql.gz"
    echo "$(date -Is) dumped docker db -> backups/books-docker-${ts}.sql.gz"
fi

# Bare-metal MySQL running alongside Docker on the same host
if [ "$(get_env BACKUP_BAREMETAL)" = "true" ]; then
    mysqldump -u books_user -p"$(get_env DB_PASSWORD)" --single-transaction --no-tablespaces books \
        | gzip > "backups/books-baremetal-${ts}.sql.gz"
    echo "$(date -Is) dumped bare-metal db -> backups/books-baremetal-${ts}.sql.gz"
fi

find backups -name 'books-*.sql.gz' -mtime +14 -delete

# Off-host copy. Set in .env:
#   BACKUP_REMOTE=user@host:path/   (rsync-over-ssh destination, key auth)
#   BACKUP_REMOTE_KEY=/path/to/ssh/key   (optional; default ssh key otherwise)
# The remote is additive-only: local pruning is NOT mirrored, so the
# destination accumulates full history (dumps are ~50 KB, so that's fine).
remote=$(get_env BACKUP_REMOTE)
if [ -n "$remote" ]; then
    key=$(get_env BACKUP_REMOTE_KEY)
    rsync -az -e "ssh ${key:+-i $key} -o StrictHostKeyChecking=accept-new" \
        backups/ "$remote"
    echo "$(date -Is) synced backups/ -> $remote"
fi

# Publish the dataset to the public GitHub data repo (production only).
# Set in .env:
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
    docker compose exec -T web python export_data.py --csv > "$clone/books.csv"
    docker compose exec -T web python export_data.py --sql > "$clone/books.sql"
    git -C "$clone" add -A
    if [ -n "$(git -C "$clone" status --porcelain)" ]; then
        git -C "$clone" -c user.name="booklibrary-backup" -c user.email="backup@localhost" \
            commit -q -m "Data export $(date +%F)"
        git -C "$clone" branch -M main
        git -C "$clone" push -q -u origin main
        echo "$(date -Is) pushed dataset to $data_repo"
    else
        echo "$(date -Is) dataset unchanged; nothing to push"
    fi
fi
