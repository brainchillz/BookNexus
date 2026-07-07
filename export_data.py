#!/usr/bin/env python3
"""Export the books table for a public data repo.

Writes to stdout:
  --csv   diffable dataset, one row per book
  --sql   MySQL seed for fresh installs (drop/create/insert), same data

The synopsis column is omitted from both: its text shouldn't live in a
public repo, and the app re-fetches synopses lazily from Open Library via
ol_key, so fresh installs self-heal.

DB config comes from the environment / .env, same as app.py.
"""
import csv
import os
import sys

import pymysql
import pymysql.cursors
from dotenv import load_dotenv

load_dotenv()

COLUMNS = ['id', 'author', 'title', 'series', 'series_num',
           'isbn', 'cover_id', 'ol_key']

# Must match the live schema (books.sql history / CLAUDE.md)
SCHEMA = """\
SET NAMES utf8mb4;
DROP TABLE IF EXISTS `books`;
CREATE TABLE `books` (
  `id` int NOT NULL AUTO_INCREMENT,
  `author` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `title` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `series` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `series_num` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `isbn` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cover_id` int DEFAULT NULL,
  `ol_key` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `synopsis` text COLLATE utf8mb4_unicode_ci,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def fetch_rows(conn):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM books ORDER BY id")
        return cur.fetchall()


def write_csv(rows):
    writer = csv.writer(sys.stdout)
    writer.writerow(COLUMNS)
    for r in rows:
        writer.writerow(['' if r[c] is None else r[c] for c in COLUMNS])


def write_sql(conn, rows):
    sys.stdout.write(SCHEMA)
    batch = 200
    for i in range(0, len(rows), batch):
        values = ',\n'.join(
            '(' + ', '.join(conn.escape(r[c]) for c in COLUMNS) + ')'
            for r in rows[i:i + batch])
        sys.stdout.write(
            f"INSERT INTO `books` ({', '.join(f'`{c}`' for c in COLUMNS)}) "
            f"VALUES\n{values};\n")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode not in ('--csv', '--sql'):
        sys.exit('usage: export_data.py --csv|--sql')
    conn = pymysql.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', ''),
        database=os.environ.get('DB_NAME', 'books'),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor)
    try:
        rows = fetch_rows(conn)
        if mode == '--csv':
            write_csv(rows)
        else:
            write_sql(conn, rows)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
