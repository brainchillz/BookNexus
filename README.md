# BookNexus

BookNexus began because my wife has a large library and is constantly adding
to it. Standing in a thrift store holding a paperback, she had no way to tell
whether it was one she already owned — so she'd either gamble on a duplicate
or pass on a book she actually needed. Now the whole collection is searchable
from her phone in a couple of seconds — or she just points the camera at the
barcode and the app answers "already in the library" or "not owned — add it?"

It's a personal book library web application built with Python/Flask and
**SQLite** — no database server to install, configure, or babysit. Your
entire library is one file on disk. Anyone can browse and search the catalog
without logging in; an admin account enables adding, editing, and deleting
entries. On first run a setup wizard asks you to create the admin account and
name your library — no default passwords, no data included, your collection
starts empty and grows from there.

> BookNexus is the SQLite successor to
> [CindysBookCollection](https://github.com/brainchillz/CindysBookCollection)
> (MySQL). Same features, half the moving parts. Moving over? Export your
> library as CSV there, import it here — done.

---

## Features

- Browse all books in a sortable, searchable, paginated table
- Browse books grouped by author or by series
- **Add by ISBN** — type an ISBN or scan the barcode with your phone's camera;
  the book's title, author, and cover are fetched automatically, and the app
  warns you if it's already in your library
- Book detail popup with cover image and synopsis (via Open Library)
- Light/dark mode toggle (remembers your choice; follows OS preference by default)
- First-run setup wizard — create the admin account and name your library; no default credentials
- Series bulk edit — every book in a series as one grid of edit boxes with a single Apply
- CSV export/import of the whole library, with validation, a confirmation step, and an automatic rollback table
- Autocomplete on author and series fields when editing
- Built-in HTTPS (`TLS_CERT`/`TLS_KEY`) or reverse-proxy mode
- One-file database; backup script with optional off-host rsync and dataset publish
- Responsive layout (Bootstrap 5) — works on mobile and desktop

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask 3 |
| Database | SQLite (Python standard library — zero dependencies) |
| Frontend | Bootstrap 5, jQuery, DataTables, ZXing (barcode scanning) |
| Production server | Gunicorn |

---

## Quick Start (Docker)

```bash
git clone https://github.com/brainchillz/BookNexus.git
cd BookNexus
cp .env.example .env
# set SECRET_KEY in .env — generate one with:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
./deploy.sh
```

Open `http://localhost:8000` — the setup wizard walks you through creating
the admin account and naming your library. That's the whole install.

Your library lives in `./data/books.db` on the host. Stopping or rebuilding
the container never touches it.

> **Note:** camera barcode scanning requires HTTPS (browsers only allow
> camera access on secure origins). See the HTTPS section below.

---

## Running without Docker

```bash
git clone https://github.com/brainchillz/BookNexus.git
cd BookNexus
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # set SECRET_KEY
gunicorn -c gunicorn.conf.py app:app
```

No database setup at all — the SQLite file and schema are created
automatically on first run.

---

## Project Structure

```
BookNexus/
├── app.py               # Flask application — all routes and database logic
├── gunicorn.conf.py     # Gunicorn config; enables TLS when configured
├── export_data.py       # Exports the dataset (CSV + seed SQL) for a data repo
├── backup_db.sh         # Nightly snapshot + optional off-host sync (cron-able)
├── deploy.sh            # One-command Docker deployment helper
├── requirements.txt     # Three dependencies: Flask, python-dotenv, gunicorn
├── .env.example         # Environment variable template (SECRET_KEY is the only required one)
├── Dockerfile           # python:3.12-slim, non-root user
├── docker-compose.yml   # Single service; ./data holds the database
├── data/                # Your library (gitignored) — data/books.db
├── certs/               # TLS cert + key (optional, gitignored)
├── static/              # Custom styles (theme-aware)
└── templates/           # All pages (Jinja2)
```

---

## Database

A single SQLite file (`data/books.db` by default; override with `DB_PATH`).
The schema is created automatically:

```sql
CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT,        -- stored as "LastName, FirstName"
    title TEXT,
    series TEXT,
    series_num TEXT,
    isbn TEXT,
    cover_id INTEGER,   -- Open Library cover image id
    ol_key TEXT,        -- Open Library work key
    synopsis TEXT       -- cached lazily on first view
);
```

A small `settings` table stores the admin credential hash and the library
title from the setup wizard. WAL mode is enabled, so public reads never
block admin writes.

**Scale**: SQLite is comfortable far beyond any personal library — tens of
thousands of books perform identically to a hundred.

---

## Usage

### First-run setup

On a brand-new install, every page redirects to `/setup` until you complete
it: pick an admin username, a password (at least 8 characters), and the title
your library displays. The page locks itself permanently once setup is done.
Complete it right after deploying.

### Public access

| URL | Description |
|---|---|
| `/` | Landing page (authors) |
| `/books` | All books — searchable, sortable, paginated |
| `/authors`, `/series` | Browse views with counts |

### Admin

Log in via **Admin** in the navbar, then:

- **📷 ISBN** — the fast lane: type or scan an ISBN, review the fetched
  details, add. Warns when the book is already in your library.
- **+ Add Book** — the manual form (autocomplete on author/series).
- **Edit / Del** on any row; **Edit all** on a series page for bulk edits.
- **Export CSV / Import** — full-library download, or replace the library
  from a previous export (validated, confirmed, previous data kept as an
  automatic rollback table).
- **Settings** — change the library title or admin password.

---

## HTTPS

**On by default.** BookNexus generates a self-signed certificate on first
boot and serves HTTPS immediately — because the camera barcode scanner
requires a secure origin, and a library app you can't scan into is half an
app. Your browser will warn once per device about the self-signed
certificate; accept it and everything works, fully encrypted.

Everything is managed from **Settings → HTTPS certificate** in the app:

- **Install certificate** — upload a real cert (PEM full chain) + private
  key to make the browser warning go away. Validated before it goes live;
  the server hot-reloads in seconds.
- **Regenerate self-signed** — new 10-year self-signed pair, with the host
  you're currently using in its subject-alternative names.
- **Disable HTTPS** — for setups behind a TLS-terminating reverse proxy,
  prefer `HTTPS=true` + `PROXY_FIX=true` in `.env` instead.

Advanced: set `TLS_CERT`/`TLS_KEY` in `.env` to manage cert files yourself
(the Settings page then shows them as environment-managed).

---

## Backups

Your library is one file, so backups are simple. `backup_db.sh` takes a
consistent snapshot (SQLite online-backup API — safe while the app runs),
gzips it into `./backups/`, and keeps 14 days. Add it to cron:

```
17 3 * * * root /path/to/BookNexus/backup_db.sh >> /var/log/booknexus-backup.log 2>&1
```

Optional, via `.env`: rsync each run's output to another machine
(`BACKUP_REMOTE`), and/or publish a synopsis-free CSV+SQL export of your
dataset to a GitHub repo (`DATA_REPO_SSH`) — handy as versioned, diffable,
offsite history.

Restore = stop the app, `gunzip` a snapshot over `data/books.db`, start the
app. The CSV export/import in the admin UI is a second, database-agnostic
path — it's also how you migrate from the MySQL version.

---

## License

Personal project, shared as-is. Use it, fork it, enjoy it.
