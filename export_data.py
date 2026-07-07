#!/usr/bin/env python3
"""Export the books table for a public data repo.

Writes to stdout:
  --csv   diffable dataset, one row per book
  --sql   SQLite seed (drop/create/insert), same data

The synopsis column is omitted from both: its text shouldn't live in a
public repo, and the app re-fetches synopses lazily from Open Library via
ol_key, so fresh installs self-heal.

DB config comes from the environment / .env, same as app.py.
"""
import csv
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get('DB_PATH', 'data/books.db')

COLUMNS = ['id', 'author', 'title', 'series', 'series_num',
           'isbn', 'cover_id', 'ol_key']

SCHEMA = """\
DROP TABLE IF EXISTS books;
CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT,
    title TEXT,
    series TEXT,
    series_num TEXT,
    isbn TEXT,
    cover_id INTEGER,
    ol_key TEXT,
    synopsis TEXT
);
"""


def _sql_literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def fetch_rows(conn):
    cur = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM books ORDER BY id")
    return cur.fetchall()


def write_csv(rows):
    writer = csv.writer(sys.stdout)
    writer.writerow(COLUMNS)
    for r in rows:
        writer.writerow(['' if r[c] is None else r[c] for c in COLUMNS])


def write_sql(rows):
    sys.stdout.write(SCHEMA)
    batch = 200
    for i in range(0, len(rows), batch):
        values = ',\n'.join(
            '(' + ', '.join(_sql_literal(r[c]) for c in COLUMNS) + ')'
            for r in rows[i:i + batch])
        sys.stdout.write(
            f"INSERT INTO books ({', '.join(COLUMNS)}) VALUES\n{values};\n")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode not in ('--csv', '--sql'):
        sys.exit('usage: export_data.py --csv|--sql')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = fetch_rows(conn)
        if mode == '--csv':
            write_csv(rows)
        else:
            write_sql(rows)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
