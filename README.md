# Cindy's Book Collection

I built this because my wife has a large library and is constantly adding to
it. Standing in a thrift store holding a paperback, she had no way to tell
whether it was one she already owned — so she'd either gamble on a duplicate
or pass on a book she actually needed. Now the whole collection is searchable
from her phone in a couple of seconds: type part of a title or author, and
know before you reach the register.

It's a personal book library web application built with Python/Flask and
MySQL. Anyone can browse and search the catalog without logging in; an admin
account enables adding, editing, and deleting entries. On first run a setup
wizard asks you to create the admin account and name your library — no
default passwords, no data included, your collection starts empty and grows
from there.

---

## Features

- Browse all books in a sortable, searchable, paginated table
- Browse books grouped by author or by series
- Series pages order books numerically by series number
- Full-text search across title, author, and series
- Book detail popup with cover image and synopsis (via Open Library ISBN lookup)
- Light/dark mode toggle (remembers your choice; follows OS preference by default)
- First-run setup wizard — create the admin account and name your library; no default credentials
- Admin login to add, edit, and delete books
- Series bulk edit — every book in a series as one grid of edit boxes with a single Apply
- CSV export/import of the whole library, with validation, a confirmation step, and an automatic rollback table
- Autocomplete on author and series fields when editing
- Built-in HTTPS (`TLS_CERT`/`TLS_KEY`) or reverse-proxy mode
- Nightly backup script with optional off-host rsync and dataset publish (see `BACKUP.md`)
- Responsive layout (Bootstrap 5) — works on mobile and desktop

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask 3 |
| Database | MySQL 8+ |
| DB driver | PyMySQL + cryptography |
| Frontend | Bootstrap 5, jQuery, DataTables |
| Production server | Gunicorn |

---

## Project Structure

```
BookDatabaseApp/
├── app.py               # Flask application — all routes and database logic
├── gunicorn.conf.py     # Gunicorn config (shared by Docker + systemd); enables TLS when configured
├── export_data.py       # Exports the dataset (CSV + seed SQL) for the public data repo
├── backup_db.sh         # Nightly dump + off-host sync + dataset publish (see BACKUP.md)
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
├── books.sql            # Database schema, imported on first run (empty library;
│                        #   set SEED_DATA_URL in .env to seed from a dataset)
├── Dockerfile           # Container image definition
├── docker-compose.yml   # Multi-container stack (app + MySQL)
├── .dockerignore        # Files excluded from the Docker image
├── deploy.sh            # One-command Docker deployment helper (fetches seed data first)
├── certs/               # TLS cert + key (gitignored; mounted read-only into the container)
├── BACKUP.md            # The full backup/restore story — read this
├── static/
│   └── style.css        # Custom styles (theme-aware)
└── templates/
    ├── base.html         # Shared layout, navbar, theme toggle, book detail modal
    ├── index.html        # Main book table (DataTables, server-side AJAX)
    ├── authors.html      # Browse all authors
    ├── author_books.html # Books by a single author
    ├── series.html       # Browse all series
    ├── series_books.html # Books in a single series
    ├── series_edit.html  # Bulk edit every book in a series (admin)
    ├── import.html       # CSV import upload + confirmation (admin)
    ├── login.html        # Admin login form
    └── book_form.html    # Add / edit book form
```

---

## Database Schema

The application uses a `books` table in a database also named `books`:

```sql
CREATE TABLE `books` (
  `id`         int          NOT NULL AUTO_INCREMENT,
  `author`     varchar(500) DEFAULT NULL,
  `title`      varchar(500) DEFAULT NULL,
  `series`     varchar(500) DEFAULT NULL,
  `series_num` varchar(100) DEFAULT NULL,
  `isbn`       varchar(20)  DEFAULT NULL,
  `cover_id`   int          DEFAULT NULL,   -- Open Library cover image id
  `ol_key`     varchar(40)  DEFAULT NULL,   -- Open Library work key
  `synopsis`   text,                        -- cached lazily on first view
  PRIMARY KEY (`id`)
);
```

A small `settings` table (created automatically) stores the admin credential
hash and the library title set in the setup wizard.

Authors are stored in **"LastName, FirstName"** format (e.g. `Adams, Douglas`).

---

## Installation

### Prerequisites

- Ubuntu 22.04 / 24.04 (or any Linux distro with `apt`)
- Python 3.10+
- MySQL 8.0+

### 1. Install system packages

```bash
sudo apt-get update
sudo apt-get install -y mysql-server python3-venv python3-pip
sudo systemctl enable --now mysql
```

### 2. Set up the database

Log into MySQL as root (on Ubuntu 24.04, use `sudo mysql`):

```bash
sudo mysql
```

Then run:

```sql
CREATE DATABASE books CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'books_user'@'localhost' IDENTIFIED BY 'your_db_password';
GRANT ALL PRIVILEGES ON books.* TO 'books_user'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

Create the schema (the table starts empty — you'll add books through the app):

```bash
mysql -u books_user -p books < books.sql
```

> Have an existing dataset (e.g. a `books.sql` exported by this app's backup
> tooling)? Import that file instead and your library starts populated.

### 3. Copy the application files

Copy the project directory to the server, for example to `/home/youruser/BookDatabaseApp`.

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in all values:

```ini
DB_HOST=localhost
DB_USER=books_user
DB_PASSWORD=your_db_password
DB_NAME=books

# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=paste_a_long_random_string_here

# Admin credentials are OPTIONAL — leave them unset and the app will show
# a first-run setup wizard at /setup where you create the admin account
# and name your library. See .env.example for the env-based alternative.
```

### 5. Create the Python virtual environment

```bash
cd BookDatabaseApp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 6. Test locally

```bash
source venv/bin/activate
python app.py
```

Visit `http://localhost:8000` — on a brand-new install you'll be redirected
to the setup wizard to create the admin account and name your library.

---

## Deployment — Docker (Recommended)

Docker bundles the app and MySQL together, handles the database import automatically on first run, and needs no manual Python or MySQL setup on the host.

### Prerequisites

- Docker 20.10+ and the Compose plugin (`docker compose version` should work)
- The project files cloned or copied to the server

Install Docker on Ubuntu if needed:

```bash
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sudo sh /tmp/get-docker.sh
sudo usermod -aG docker $USER   # log out and back in after this
```

### 1. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and set the values (the deploy script checks the required ones):

```ini
# MySQL root password (used only inside the Docker network)
MYSQL_ROOT_PASSWORD=choose-a-root-password

# Port the app will be exposed on the host
APP_PORT=8000

# Database credentials (used by both the db and web containers)
DB_PASSWORD=choose-a-db-password

# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=paste-a-long-random-string-here

# Admin credentials are OPTIONAL — leave unset and the app shows a
# first-run setup wizard at /setup (see .env.example for the alternative)
```

> `DB_HOST`, `DB_USER`, and `DB_NAME` are set automatically by `docker-compose.yml`
> and do not need to appear in `.env` for the Docker deployment.

### 2. Deploy

```bash
./deploy.sh
```

The script will:
1. Verify all required variables are set in `.env`
2. Build the app image
3. Pull the MySQL 8.0 image
4. Start both containers (`-d` detached)
5. Print the URL and a short command reference

On **first run** MySQL imports `books.sql` automatically. On subsequent starts the data is already in the named volume and the import is skipped.

### 3. Allow the port through the firewall

```bash
sudo ufw allow 8000/tcp   # use whatever port you set for APP_PORT
```

### Useful commands

```bash
# Stream logs from both containers
docker compose logs -f

# Stream logs from the app only
docker compose logs -f web

# Check container status
docker compose ps

# Stop the stack (data is preserved in the volume)
docker compose down

# Stop and delete all data (full reset)
docker compose down -v

# Rebuild and restart after a code change
docker compose up -d --build
```

### How it works

| Detail | Behaviour |
|---|---|
| **Database persistence** | MySQL data lives in a named Docker volume (`db_data`). It survives `docker compose down` and server reboots. Only `docker compose down -v` removes it. |
| **First-run import** | `books.sql` is mounted into `/docker-entrypoint-initdb.d/` in the MySQL container. MySQL runs it once on initialisation, then never again. |
| **Startup order** | The `web` container waits for the `db` healthcheck to pass before starting, preventing connection errors on boot. |
| **Auto-restart** | Both containers use `restart: unless-stopped` — they come back up automatically after a reboot or crash as long as the Docker daemon is running. |
| **Non-root process** | The Flask app runs as an unprivileged `appuser` inside the container. |

### Updating after a code change

```bash
git pull
docker compose up -d --build
```

The `db` container is untouched; only the `web` image is rebuilt.

---

## Deployment — Manual (systemd + Gunicorn)

Create a systemd service so the app starts on boot and restarts automatically if it crashes.

### Create the service file

```bash
sudo nano /etc/systemd/system/booklibrary.service
```

Paste the following, replacing `youruser` with the Linux user that owns the app files:

```ini
[Unit]
Description=Book Library Flask App
After=network.target mysql.service

[Service]
User=youruser
WorkingDirectory=/home/youruser/BookDatabaseApp
EnvironmentFile=/home/youruser/BookDatabaseApp/.env
ExecStart=/home/youruser/BookDatabaseApp/venv/bin/gunicorn -c /home/youruser/BookDatabaseApp/gunicorn.conf.py app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now booklibrary
sudo systemctl status booklibrary
```

The app is now listening on port **8000**.

---

## Firewall

If `ufw` is active, allow port 8000:

```bash
sudo ufw allow 8000/tcp
```

---

## HTTPS (Recommended for Public Internet)

### Option A — built-in TLS (no proxy needed)

If you already have a certificate, the app can serve HTTPS directly. Set in `.env`:

```ini
TLS_CERT=certs/fullchain.pem
TLS_KEY=certs/privkey.pem
```

Put the files in `./certs/` — relative paths resolve both on bare metal
(cwd = project dir) and in Docker (`./certs` is mounted read-only at
`/app/certs`). Make the key readable by the container user:
`sudo chown 10001:10001 certs/privkey.pem && sudo chmod 400 certs/privkey.pem`
(use group `10001` with mode `640` if a bare-metal gunicorn on the same host
must read it too).

When both variables are set, Gunicorn serves HTTPS on the same port, the session
cookie gets the `Secure` flag, and an HSTS header is sent. Plain HTTP connections
to the port are refused.

### Option B — reverse proxy

Alternatively, run nginx in front of Gunicorn and terminate TLS there. Set
`HTTPS=true` and `PROXY_FIX=true` in `.env` so the app marks cookies secure and
sees real client IPs. Install nginx and Certbot:

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

Create `/etc/nginx/sites-available/booklibrary`:

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Enable the site and get a certificate:

```bash
sudo ln -s /etc/nginx/sites-available/booklibrary /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d yourdomain.com
```

Certbot will automatically configure HTTPS and schedule certificate renewal.

---

## Usage

### Public access

| URL | Description |
|---|---|
| `/` | All books — searchable, sortable, paginated |
| `/authors` | All authors with book counts |
| `/authors/<name>` | All books by a specific author |
| `/series` | All series with book counts |
| `/series/<name>` | All books in a series, ordered by series number |

Use the search box on the main page to filter across title, author, and series simultaneously. Click any column header to sort. Click an author name or series name in any table to jump to that browse view.

### First-run setup

On a brand-new install, every page redirects to `/setup` until you complete
it: pick an admin username, a password (at least 8 characters), and the title
your library displays. The page locks itself permanently once setup is done.
Complete it right after deploying — until then, whoever visits first gets to
claim the admin account.

### Admin access

Navigate to `/login` (or click **Admin** in the navbar) and use the account
created in the setup wizard (or the `.env` credentials if you configured
those instead).

Once logged in:

- **Add a book** — click **+ Add Book** in the navbar. The author and series fields have autocomplete from existing entries. Author names should be entered in **Last, First** format.
- **Edit a book** — click the **Edit** button on any row.
- **Bulk edit a series** — open a series page and click **Edit all**: every book becomes a row of edit boxes with one Apply button. Changed fields highlight; nothing saves until Apply, and it's all-or-nothing.
- **Delete a book** — click **Del** on any row (with confirmation), or use the **Delete Book** button at the bottom of the edit form.
- **Export / Import** — download the whole library as CSV, or re-upload one to replace the library (validated, with a confirmation step, and the previous data is kept as a rollback table).
- **Settings** — change the library title or the admin password anytime.

---

## Updating the Application

After editing files on your local machine, push changes to the server:

```bash
# Copy changed files
scp app.py youruser@yourserver:~/BookDatabaseApp/

# Restart the service
ssh youruser@yourserver 'sudo systemctl restart booklibrary'
```

To update Python dependencies after changing `requirements.txt`:

```bash
ssh youruser@yourserver
cd ~/BookDatabaseApp
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart booklibrary
```

---

## Troubleshooting

**Docker: app starts but shows AJAX error / can't reach the database**
```bash
docker compose logs db      # check MySQL started cleanly
docker compose logs web     # check for Python tracebacks
docker compose ps           # confirm db shows "(healthy)"
```
If `db` is not healthy, wait 30 seconds and check again — MySQL takes a moment on first boot while it imports `books.sql`.

**Docker: books.sql was not imported**
The init scripts only run when the volume is first created. If you started with an empty or partial `.env` and MySQL initialised without importing the data:
```bash
docker compose down -v      # wipe the volume
docker compose up -d        # fresh start — import runs again
```

**Docker: port already in use**
Change `APP_PORT` in `.env` to a free port (e.g. `8080`) and run `docker compose up -d` again.

**Docker: permission denied running docker commands**
```bash
sudo usermod -aG docker $USER   # add your user to the docker group
# then log out and back in
```

---

**DataTables AJAX error on page load**
Check the service logs:
```bash
sudo journalctl -u booklibrary -n 50
```
Common causes:
- `.env` file missing or has wrong database credentials
- MySQL service not running (`sudo systemctl status mysql`)
- Missing `cryptography` package — run `venv/bin/pip install cryptography`

**Cannot connect to MySQL**
```bash
mysql -u books_user -p books -e "SELECT COUNT(*) FROM books;"
```
If this fails, verify the user and password in MySQL:
```bash
sudo mysql -e "SELECT user, host FROM mysql.user;"
```

**Service fails to start**
```bash
sudo systemctl status booklibrary
sudo journalctl -u booklibrary -b
```

**Port 8000 not reachable**
```bash
sudo ufw status
sudo ufw allow 8000/tcp
```
