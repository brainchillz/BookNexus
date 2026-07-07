import csv
import io
import os
import re
import json
import sqlite3
import time
import hmac
import secrets
import threading
import urllib.request
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, session, flash, abort, Response)
from functools import wraps
from urllib.parse import quote, urlparse, urljoin
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    raise RuntimeError('SECRET_KEY environment variable must be set')

app = Flask(__name__)
app.secret_key = _secret_key
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # cap uploads (CSV import)
# TLS_CERT/TLS_KEY make gunicorn serve HTTPS directly (see gunicorn.conf.py);
# HTTPS=true covers the behind-a-TLS-terminating-proxy case.
TLS_ENABLED = bool(os.environ.get('TLS_CERT') and os.environ.get('TLS_KEY'))
app.config['SESSION_COOKIE_SECURE'] = (
    TLS_ENABLED or os.environ.get('HTTPS', '').lower() == 'true')

# Set PROXY_FIX=true when a reverse proxy sets X-Forwarded-For/-Proto, so
# request.remote_addr is the real client IP (the login rate limiter needs it)
if os.environ.get('PROXY_FIX', '').lower() == 'true':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# SQLite database file. The schema is created automatically on first run —
# no server, no seed file: a brand-new install starts with an empty library.
DB_PATH = os.environ.get('DB_PATH', 'data/books.db')

_BOOKS_COLUMNS_DDL = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT,
    title TEXT,
    series TEXT,
    series_num TEXT,
    isbn TEXT,
    cover_id INTEGER,
    ol_key TEXT,
    synopsis TEXT
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS books ({_BOOKS_COLUMNS_DDL});
CREATE TABLE IF NOT EXISTS settings (
    name TEXT NOT NULL PRIMARY KEY,
    value TEXT
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    conn = get_db()
    try:
        # WAL: readers and the (single) writer never block each other
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


_init_db()

# Admin credentials live in the DB settings table (written by the first-run
# setup wizard or the admin settings page) and take precedence. The .env
# variables remain as a fallback so pre-wizard installs keep working. When
# neither exists, every request redirects to /setup.
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH', '')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
DEFAULT_SITE_TITLE = os.environ.get('SITE_NAME', 'BookNexus')

# Settings cache: one tiny query per worker per TTL; forced refresh on writes.
_SETTINGS_TTL = 30
_settings_cache: dict = {'data': None, 'ts': 0.0}
_settings_db_lock = threading.Lock()


def _load_settings(force: bool = False) -> dict:
    now = time.time()
    with _settings_db_lock:
        cached = _settings_cache['data']
        if not force and cached is not None and now - _settings_cache['ts'] < _SETTINGS_TTL:
            return cached
    try:
        conn = get_db()
        try:
            with closing(conn.cursor()) as cur:
                cur.execute("SELECT name, value FROM settings")
                data = {r['name']: r['value'] for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception:
        # DB briefly unavailable: serve stale cache rather than 500 every page
        with _settings_db_lock:
            return _settings_cache['data'] or {}
    with _settings_db_lock:
        _settings_cache['data'] = data
        _settings_cache['ts'] = now
    return data


def _save_settings(**values) -> None:
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            for name, value in values.items():
                cur.execute(
                    "INSERT INTO settings (name, value) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                    (name, value))
        conn.commit()
    finally:
        conn.close()
    _load_settings(force=True)


def _is_configured() -> bool:
    """True once an admin credential exists (DB settings or .env fallback)."""
    if ADMIN_PASSWORD_HASH or ADMIN_PASSWORD:
        return True
    return bool(_load_settings().get('admin_password_hash'))


def _get_admin_username() -> str:
    s = _load_settings()
    if s.get('admin_password_hash'):
        return s.get('admin_username') or 'admin'
    return ADMIN_USERNAME


def _check_admin_password(candidate: str) -> bool:
    s = _load_settings()
    if s.get('admin_password_hash'):
        return check_password_hash(s['admin_password_hash'], candidate)
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, candidate)
    if ADMIN_PASSWORD:
        return hmac.compare_digest(candidate, ADMIN_PASSWORD)
    return False


def _site_title() -> str:
    return _load_settings().get('site_title') or DEFAULT_SITE_TITLE


@app.context_processor
def _inject_site_title():
    return {'site_title': _site_title()}


@app.before_request
def _require_setup():
    if request.endpoint in ('setup', 'static'):
        return None
    if not _is_configured():
        # Cache may be stale right after another worker completed setup —
        # re-check the DB before actually redirecting
        if not (_load_settings(force=True).get('admin_password_hash')
                or ADMIN_PASSWORD_HASH or ADMIN_PASSWORD):
            return redirect(url_for('setup'))
    return None

# Rate limiter: max 5 login attempts per IP per 5 minutes
_login_attempts: dict = defaultdict(list)
_login_lock = threading.Lock()
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW = 300


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        recent = [t for t in _login_attempts[ip] if now - t < _RATE_LIMIT_WINDOW]
        _login_attempts[ip] = recent
        if len(recent) >= _RATE_LIMIT_MAX:
            return True
        _login_attempts[ip].append(now)
        return False


def _safe_redirect_url(target: str) -> str:
    """Return target if it's same-origin, otherwise the index URL."""
    if target:
        ref = urlparse(request.host_url)
        test = urlparse(urljoin(request.host_url, target))
        if test.scheme in ('http', 'https') and ref.netloc == test.netloc:
            return target
    return url_for('index')


def _get_csrf_token() -> str:
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

app.jinja_env.globals['csrf_token'] = _get_csrf_token


def _check_csrf() -> None:
    token = session.get('csrf_token')
    if not token or not hmac.compare_digest(token, request.form.get('csrf_token', '')):
        abort(403)


@app.after_request
def _security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if app.config['SESSION_COOKIE_SECURE']:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.template_filter('urlencode')
def urlencode_filter(s):
    return quote(str(s), safe='')


_OL_HEADERS = {'User-Agent': 'BookDatabaseApp/1.0 (personal library)'}

# Failed synopsis fetches aren't cached in the DB, so without a cooldown every
# public detail request retries the 5s outbound fetch — an easy way to tie up
# all gunicorn workers. Per-worker, in-memory, like the login rate limiter.
_synopsis_failures: dict = {}
_synopsis_lock = threading.Lock()
_SYNOPSIS_RETRY_COOLDOWN = 600


def _synopsis_fetch_allowed(book_id: int) -> bool:
    with _synopsis_lock:
        return time.time() - _synopsis_failures.get(book_id, 0) > _SYNOPSIS_RETRY_COOLDOWN


def _record_synopsis_result(book_id: int, failed: bool) -> None:
    with _synopsis_lock:
        if failed:
            _synopsis_failures[book_id] = time.time()
        else:
            _synopsis_failures.pop(book_id, None)


def _clean_isbn(raw):
    isbn = re.sub(r'[\s-]', '', raw or '')
    return isbn if re.fullmatch(r'\d{9,12}[\dXx]', isbn) else None


def _fetch_ol_description(ol_key):
    """Fetch a work's description from Open Library.

    Returns '' when the work has no description (cache that), or None on
    transient failure (so a later request retries).
    """
    if not re.fullmatch(r'/works/OL\d+W', ol_key or ''):
        return ''
    try:
        req = urllib.request.Request(
            f'https://openlibrary.org{ol_key}.json', headers=_OL_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
    except Exception:
        return None
    desc = data.get('description')
    if isinstance(desc, dict):
        desc = desc.get('value')
    if not isinstance(desc, str):
        return ''
    return desc.strip()[:5000]


def _lookup_isbn(isbn):
    """Best-effort (cover_id, ol_key) lookup for an ISBN via Open Library."""
    try:
        req = urllib.request.Request(
            f'https://openlibrary.org/isbn/{isbn}.json', headers=_OL_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
        cover_id = (data.get('covers') or [None])[0]
        works = data.get('works') or []
        ol_key = works[0].get('key') if works else None
        return cover_id, ol_key
    except Exception:
        return None, None


def _isbn_variants(isbn):
    """The ISBN plus its ISBN-10<->13 twin, so owned-checks match either form."""
    variants = {isbn}
    if len(isbn) == 13 and isbn.startswith('978') and isbn.isdigit():
        body = isbn[3:12]
        check = sum((i + 1) * int(d) for i, d in enumerate(body)) % 11
        variants.add(body + ('X' if check == 10 else str(check)))
    elif len(isbn) == 10:
        body = '978' + isbn[:9]
        if body.isdigit():
            check = (10 - sum((1 if i % 2 == 0 else 3) * int(d)
                              for i, d in enumerate(body)) % 10) % 10
            variants.add(body + str(check))
    return variants


def _flip_author(name):
    """'Douglas Adams' -> 'Adams, Douglas' (the format this library uses)."""
    parts = (name or '').strip().split()
    if len(parts) >= 2 and ',' not in name:
        return f"{parts[-1]}, {' '.join(parts[:-1])}"
    return name or ''


def _fetch_ol_json(path):
    req = urllib.request.Request(
        f'https://openlibrary.org{path}', headers=_OL_HEADERS)
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.load(resp)


def api_isbn_lookup(isbn):
    """Metadata + already-owned check for the Add-by-ISBN page."""
    clean = _clean_isbn(isbn)
    if not clean:
        return jsonify({'error': 'Not a valid ISBN'}), 400

    # Owned check by ISBN (either 10/13 form) — works even if Open Library is down
    variants = list(_isbn_variants(clean))
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            placeholders = ', '.join(['?'] * len(variants))
            cur.execute(
                f"SELECT id, author, title FROM books WHERE isbn IN ({placeholders})",
                variants)
            owned = cur.fetchall()

        meta = None
        try:
            data = _fetch_ol_json(f'/isbn/{clean}.json')
            author_name = ''
            authors = data.get('authors') or []
            if authors and authors[0].get('key'):
                try:
                    author_name = _fetch_ol_json(
                        f"{authors[0]['key']}.json").get('name', '')
                except Exception:
                    pass
            cover_id = (data.get('covers') or [None])[0]
            meta = {
                'title': data.get('title', ''),
                'author': _flip_author(author_name),
                'cover_url': (f'https://covers.openlibrary.org/b/id/{cover_id}-M.jpg'
                              if cover_id else None),
            }
        except Exception:
            pass

        # Second owned-check net: same title+author already entered without an ISBN
        if meta and meta['title'] and not owned:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    "SELECT id, author, title FROM books "
                    "WHERE LOWER(title) = LOWER(?) AND author LIKE ? LIMIT 5",
                    (meta['title'],
                     f"%{meta['author'].split(',')[0]}%" if meta['author'] else '%'))
                owned = cur.fetchall()
    finally:
        conn.close()

    return jsonify({
        'isbn': clean,
        'found': meta is not None,
        'meta': meta,
        'owned': [{'id': r['id'], 'author': r['author'] or '',
                   'title': r['title'] or ''} for r in owned],
    })


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated


@app.route('/')
def index():
    return redirect(url_for('authors'))


@app.route('/books')
def all_books():
    return render_template('index.html')


@app.route('/api/books')
def api_books():
    draw = request.args.get('draw', 1, type=int)
    start = request.args.get('start', 0, type=int)
    length = min(request.args.get('length', 25, type=int), 200)
    search = request.args.get('search[value]', '').strip()

    order_col_idx = request.args.get('order[0][column]', 0, type=int)
    order_dir = request.args.get('order[0][dir]', 'asc')

    col_names = ['author', 'title', 'series', 'series_num']
    order_col = col_names[order_col_idx] if 0 <= order_col_idx < len(col_names) else 'author'
    if order_dir not in ('asc', 'desc'):
        order_dir = 'asc'

    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM books")
            total = cur.fetchone()['cnt']

            if search:
                like = f'%{search}%'
                where = "WHERE author LIKE ? OR title LIKE ? OR series LIKE ?"
                params = [like, like, like]
                cur.execute(f"SELECT COUNT(*) as cnt FROM books {where}", params)
                filtered = cur.fetchone()['cnt']
                cur.execute(
                    f"SELECT id, author, title, series, series_num FROM books {where} "
                    f"ORDER BY {order_col} {order_dir} LIMIT ? OFFSET ?",
                    params + [length, start]
                )
            else:
                filtered = total
                cur.execute(
                    f"SELECT id, author, title, series, series_num FROM books "
                    f"ORDER BY {order_col} {order_dir} LIMIT ? OFFSET ?",
                    [length, start]
                )
            rows = cur.fetchall()
    finally:
        conn.close()

    data = [
        {
            'id': r['id'],
            'author': r['author'] or '',
            'title': r['title'] or '',
            'series': r['series'] or '',
            'series_num': r['series_num'] or '',
        }
        for r in rows
    ]

    return jsonify({
        'draw': draw,
        'recordsTotal': total,
        'recordsFiltered': filtered,
        'data': data,
    })


@app.route('/api/books/<int:book_id>')
def api_book_detail(book_id):
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM books WHERE id = ?", (book_id,))
            book = cur.fetchone()
        book = dict(book) if book else None
        if not book:
            abort(404)
        # Lazily fetch and cache the synopsis on first view
        if (book.get('synopsis') is None and book.get('ol_key')
                and _synopsis_fetch_allowed(book_id)):
            synopsis = _fetch_ol_description(book['ol_key'])
            _record_synopsis_result(book_id, failed=synopsis is None)
            if synopsis is not None:
                with closing(conn.cursor()) as cur:
                    cur.execute("UPDATE books SET synopsis=? WHERE id=?",
                                (synopsis, book_id))
                conn.commit()
                book['synopsis'] = synopsis
    finally:
        conn.close()
    cover_url = (f"https://covers.openlibrary.org/b/id/{book['cover_id']}-L.jpg"
                 if book.get('cover_id') else None)
    return jsonify({
        'id': book['id'],
        'author': book['author'] or '',
        'title': book['title'] or '',
        'series': book['series'] or '',
        'series_num': book['series_num'] or '',
        'isbn': book.get('isbn') or '',
        'cover_url': cover_url,
        'synopsis': book.get('synopsis') or '',
    })


@app.route('/api/autocomplete/authors')
def autocomplete_authors():
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT DISTINCT author FROM books WHERE author IS NOT NULL ORDER BY author"
            )
            authors = [r['author'] for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify(authors)


@app.route('/api/autocomplete/series')
def autocomplete_series():
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT DISTINCT series FROM books "
                "WHERE series IS NOT NULL AND series != '' ORDER BY series"
            )
            series = [r['series'] for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify(series)


@app.route('/authors')
def authors():
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT author, COUNT(*) as book_count FROM books "
                "WHERE author IS NOT NULL GROUP BY author ORDER BY author"
            )
            authors_list = cur.fetchall()
    finally:
        conn.close()
    return render_template('authors.html', authors=authors_list)


@app.route('/authors/<path:author>')
def author_books(author):
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT id, title, series, series_num FROM books "
                "WHERE author = ? ORDER BY series, series_num, title",
                (author,)
            )
            books = cur.fetchall()
    finally:
        conn.close()
    return render_template('author_books.html', author=author, books=books)


@app.route('/series')
def series_list():
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT series, COUNT(*) as book_count FROM books "
                "WHERE series IS NOT NULL AND series != '' "
                "GROUP BY series ORDER BY series"
            )
            series = cur.fetchall()
    finally:
        conn.close()
    return render_template('series.html', series_list=series)


@app.route('/series/<path:series_name>')
def series_books(series_name):
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT id, author, title, series_num FROM books "
                "WHERE series = ? "
                "ORDER BY "
                "  CASE WHEN series_num IS NULL OR series_num = '' THEN 1 ELSE 0 END, "
                "  CAST(series_num AS REAL) ASC, "
                "  series_num ASC, title ASC",
                (series_name,)
            )
            books = cur.fetchall()
    finally:
        conn.close()
    return render_template('series_books.html', series=series_name, books=books)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        _check_csrf()
        if _is_rate_limited(request.remote_addr):
            flash('Too many login attempts — please wait a few minutes.', 'danger')
            return render_template('login.html', next=request.form.get('next', ''))
        # Credentials must never be checked against a stale cache: a password
        # changed in another worker must take effect immediately. Login is
        # rate-limited, so the forced DB read costs nothing.
        _load_settings(force=True)
        username_ok = hmac.compare_digest(
            request.form.get('username', ''), _get_admin_username())
        password_ok = _check_admin_password(request.form.get('password', ''))
        if username_ok and password_ok:
            next_url = request.form.get('next', '')
            session.clear()
            session['logged_in'] = True
            return redirect(_safe_redirect_url(next_url))
        flash('Invalid credentials', 'danger')
    return render_template('login.html', next=request.args.get('next', ''))


@app.route('/logout', methods=['POST'])
def logout():
    _check_csrf()
    session.clear()
    return redirect(url_for('index'))


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    # Only reachable while no admin credential exists anywhere; afterwards
    # it permanently redirects (fresh DB check, never trust the cache here)
    if (_load_settings(force=True).get('admin_password_hash')
            or ADMIN_PASSWORD_HASH or ADMIN_PASSWORD):
        return redirect(url_for('login'))
    if request.method == 'POST':
        _check_csrf()
        username = request.form.get('username', '').strip() or 'admin'
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        title = request.form.get('site_title', '').strip()
        if len(password) < 8:
            flash('Password must be at least 8 characters', 'warning')
        elif password != confirm:
            flash('Passwords do not match', 'warning')
        else:
            _save_settings(
                admin_username=username,
                admin_password_hash=generate_password_hash(password),
                site_title=title or DEFAULT_SITE_TITLE)
            flash('Setup complete — log in with your new credentials', 'success')
            return redirect(url_for('login'))
    return render_template('setup.html',
                           default_title=DEFAULT_SITE_TITLE,
                           form=request.form)


@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if request.method == 'POST':
        _check_csrf()
        title = request.form.get('site_title', '').strip()
        current = request.form.get('current_password', '')
        new = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if title and title != _site_title():
            _save_settings(site_title=title)
            flash('Site title updated', 'success')
        if current or new or confirm:
            if not _check_admin_password(current):
                flash('Current password is incorrect', 'danger')
            elif len(new) < 8:
                flash('New password must be at least 8 characters', 'warning')
            elif new != confirm:
                flash('New passwords do not match', 'warning')
            else:
                # Written to the DB; takes precedence over any .env credential
                _save_settings(
                    admin_username=_get_admin_username(),
                    admin_password_hash=generate_password_hash(new))
                flash('Password changed', 'success')
        return redirect(url_for('admin_settings'))
    return render_template('settings.html')


@app.route('/series/<path:series_name>/edit', methods=['GET', 'POST'])
@login_required
def series_edit(series_name):
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT id, author, title, series, series_num, isbn FROM books "
                "WHERE series = ? "
                "ORDER BY "
                "  CASE WHEN series_num IS NULL OR series_num = '' THEN 1 ELSE 0 END, "
                "  CAST(series_num AS REAL) ASC, "
                "  series_num ASC, title ASC",
                (series_name,)
            )
            books = cur.fetchall()
        if not books:
            flash('No books found in that series', 'warning')
            return redirect(url_for('series_list'))

        if request.method == 'GET':
            return render_template('series_edit.html', series=series_name,
                                   books=books, formdata={})

        _check_csrf()
        by_id = {str(b['id']): b for b in books}
        fields = ('author', 'title', 'series', 'series_num', 'isbn')
        formdata, errors, updates = {}, [], []
        for bid in request.form.getlist('book_id'):
            book = by_id.get(bid)
            if not book:
                continue  # not part of this series; ignore
            f = {c: request.form.get(f'{c}-{bid}', '').strip() for c in fields}
            formdata[bid] = f
            if not f['author'] or not f['title']:
                errors.append(f'"{book["title"] or "book " + bid}": '
                              'author and title are required')
                continue
            isbn = _clean_isbn(f['isbn'])
            new = (f['author'], f['title'], f['series'] or None,
                   f['series_num'] or None, isbn)
            old = (book['author'], book['title'], book['series'],
                   book['series_num'], book['isbn'])
            if new != old:
                updates.append((int(bid), new, isbn != book['isbn']))

        if errors:
            return render_template('series_edit.html', series=series_name,
                                   books=books, formdata=formdata, errors=errors)
        if not updates:
            flash('No changes to apply', 'info')
            return redirect(url_for('series_books', series_name=series_name))

        # All updates in one transaction: Apply commits everything or nothing
        with closing(conn.cursor()) as cur:
            for bid, (author, title, series, series_num, isbn), isbn_changed in updates:
                if isbn_changed:
                    # Same behavior as the single-book edit form:
                    # refresh cover/work identity, clear cached synopsis
                    cover_id, ol_key = _lookup_isbn(isbn) if isbn else (None, None)
                    cur.execute(
                        "UPDATE books SET author=?, title=?, series=?, series_num=?, "
                        "isbn=?, cover_id=?, ol_key=?, synopsis=NULL WHERE id=?",
                        (author, title, series, series_num, isbn, cover_id, ol_key, bid))
                else:
                    cur.execute(
                        "UPDATE books SET author=?, title=?, series=?, series_num=? "
                        "WHERE id=?",
                        (author, title, series, series_num, bid))
        conn.commit()
    finally:
        conn.close()
    flash(f'Updated {len(updates)} book{"s" if len(updates) != 1 else ""}', 'success')
    # The series itself may have been renamed — land on the page that has the books
    target = next((u[1][2] for u in updates if u[1][2]), None)
    if all(u[1][2] == series_name for u in updates):
        target = series_name
    if target:
        return redirect(url_for('series_books', series_name=target))
    return redirect(url_for('series_list'))


# ── CSV export / import ───────────────────────────────────────────
# Column order is the CSV contract: export writes it, import requires it.
# Empty CSV cell == NULL. All quoting/escaping is left to the csv module
# (RFC 4180) — author fields contain commas by design ("LastName, FirstName").
_CSV_COLUMNS = ['id', 'author', 'title', 'series', 'series_num',
                'isbn', 'cover_id', 'ol_key', 'synopsis']
# Max lengths mirror the table schema (varchar sizes; synopsis is TEXT=64KB)
_CSV_LIMITS = {'author': 500, 'title': 500, 'series': 500,
               'series_num': 100, 'isbn': 20, 'ol_key': 40, 'synopsis': 60000}


def _validate_csv_row(rownum, row):
    """Return (values_tuple, error). Empty strings become None (NULL)."""
    if len(row) != len(_CSV_COLUMNS):
        return None, f'row {rownum}: expected {len(_CSV_COLUMNS)} fields, got {len(row)}'
    rec = dict(zip(_CSV_COLUMNS, (v.strip() for v in row)))
    for col in ('id', 'cover_id'):
        if rec[col] and not rec[col].isdigit():
            return None, f'row {rownum}: {col} must be a whole number, got {rec[col]!r}'
    if not rec['author'] or not rec['title']:
        return None, f'row {rownum}: author and title are required'
    for col, limit in _CSV_LIMITS.items():
        if len(rec[col]) > limit:
            return None, f'row {rownum}: {col} longer than {limit} characters'
    return tuple((rec[c] or None) for c in _CSV_COLUMNS), None


@app.route('/admin/export')
@login_required
def export_books():
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                f"SELECT {', '.join(_CSV_COLUMNS)} FROM books ORDER BY id")
            rows = cur.fetchall()
    finally:
        conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for r in rows:
        writer.writerow(['' if r[c] is None else r[c] for c in _CSV_COLUMNS])
    filename = f"books-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    # BOM so Excel/LibreOffice detect UTF-8
    return Response(
        ('\ufeff' + buf.getvalue()).encode('utf-8'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.route('/admin/import', methods=['GET', 'POST'])
@login_required
def import_books():
    if request.method == 'GET':
        return render_template('import.html')
    _check_csrf()
    file = request.files.get('file')
    if not file or not file.filename:
        flash('Choose a CSV file to import', 'warning')
        return render_template('import.html')
    try:
        text = file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        flash('File is not valid UTF-8 — export the CSV again and retry', 'danger')
        return render_template('import.html')

    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None or [h.strip().lower() for h in header] != _CSV_COLUMNS:
        flash(f"First row must be the header: {','.join(_CSV_COLUMNS)}", 'danger')
        return render_template('import.html')

    records, errors = [], []
    for rownum, row in enumerate(reader, start=2):
        if not row:
            continue
        rec, err = _validate_csv_row(rownum, row)
        if err:
            errors.append(err)
            if len(errors) >= 10:
                errors.append('… stopping after 10 errors')
                break
        else:
            records.append(rec)
    if errors:
        return render_template('import.html', errors=errors)
    if not records:
        flash('No data rows found in the file', 'warning')
        return render_template('import.html')

    # Stage into a scratch table; nothing touches `books` until confirmed
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute("DROP TABLE IF EXISTS books_import")
            cur.execute(f"CREATE TABLE books_import ({_BOOKS_COLUMNS_DDL})")
            cur.executemany(
                f"INSERT INTO books_import ({', '.join(_CSV_COLUMNS)}) "
                f"VALUES ({', '.join(['?'] * len(_CSV_COLUMNS))})",
                records)
            cur.execute("SELECT COUNT(*) AS cnt FROM books")
            current = cur.fetchone()['cnt']
        conn.commit()
    finally:
        conn.close()
    return render_template('import.html', confirm=True,
                           staged=len(records), current=current)


@app.route('/admin/import/confirm', methods=['POST'])
@login_required
def import_books_confirm():
    _check_csrf()
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='books_import'")
            if not cur.fetchone():
                flash('Nothing staged — upload a CSV first', 'warning')
                return redirect(url_for('import_books'))
            cur.execute("SELECT COUNT(*) AS cnt FROM books_import")
            staged = cur.fetchone()['cnt']
            # Swap inside the transaction (SQLite DDL is transactional);
            # previous data survives as books_old until the next import
            cur.execute("DROP TABLE IF EXISTS books_old")
            cur.execute("ALTER TABLE books RENAME TO books_old")
            cur.execute("ALTER TABLE books_import RENAME TO books")
        conn.commit()
    finally:
        conn.close()
    flash(f'Imported {staged} books. The previous data is kept as table '
          f'"books_old" until the next import.', 'success')
    return redirect(url_for('all_books'))


app.add_url_rule('/api/isbn/<isbn>', 'api_isbn_lookup',
                 login_required(api_isbn_lookup))


@app.route('/books/add-isbn')
@login_required
def add_by_isbn():
    return render_template('add_isbn.html')


@app.route('/books/add', methods=['GET', 'POST'])
@login_required
def add_book():
    if request.method == 'POST':
        _check_csrf()
        author = request.form.get('author', '').strip()
        title = request.form.get('title', '').strip()
        series = request.form.get('series', '').strip() or None
        series_num = request.form.get('series_num', '').strip() or None
        isbn = _clean_isbn(request.form.get('isbn', ''))
        if not author or not title:
            flash('Author and title are required', 'warning')
            return render_template('book_form.html', book=request.form, action='Add')
        cover_id, ol_key = _lookup_isbn(isbn) if isbn else (None, None)
        conn = get_db()
        try:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    "INSERT INTO books (author, title, series, series_num, isbn, cover_id, ol_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (author, title, series, series_num, isbn, cover_id, ol_key)
                )
            conn.commit()
        finally:
            conn.close()
        flash('Book added successfully', 'success')
        return redirect(url_for('index'))
    return render_template('book_form.html', book=None, action='Add')


@app.route('/books/<int:book_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_book(book_id):
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM books WHERE id = ?", (book_id,))
            book = cur.fetchone()
        book = dict(book) if book else None
        if not book:
            flash('Book not found', 'warning')
            return redirect(url_for('index'))
        if request.method == 'POST':
            _check_csrf()
            author = request.form.get('author', '').strip()
            title = request.form.get('title', '').strip()
            series = request.form.get('series', '').strip() or None
            series_num = request.form.get('series_num', '').strip() or None
            isbn = _clean_isbn(request.form.get('isbn', ''))
            if not author or not title:
                flash('Author and title are required', 'warning')
                return render_template('book_form.html', book=book, action='Edit', book_id=book_id)
            with closing(conn.cursor()) as cur:
                if isbn != book.get('isbn'):
                    # ISBN changed: refresh cover/work identity, clear cached synopsis
                    cover_id, ol_key = _lookup_isbn(isbn) if isbn else (None, None)
                    cur.execute(
                        "UPDATE books SET author=?, title=?, series=?, series_num=?, "
                        "isbn=?, cover_id=?, ol_key=?, synopsis=NULL WHERE id=?",
                        (author, title, series, series_num, isbn, cover_id, ol_key, book_id)
                    )
                else:
                    cur.execute(
                        "UPDATE books SET author=?, title=?, series=?, series_num=? WHERE id=?",
                        (author, title, series, series_num, book_id)
                    )
            conn.commit()
            flash('Book updated', 'success')
            return redirect(url_for('index'))
    finally:
        conn.close()
    return render_template('book_form.html', book=book, action='Edit', book_id=book_id)


@app.route('/books/<int:book_id>/delete', methods=['POST'])
@login_required
def delete_book(book_id):
    _check_csrf()
    conn = get_db()
    try:
        with closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.commit()
    finally:
        conn.close()
    flash('Book deleted', 'success')
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False)
