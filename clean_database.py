#!/usr/bin/env python3
"""
Remove Banuri fatwas whose question field matches known navigation/noise markers.

Default is dry-run (preview only). Pass --delete to remove rows.

Usage:
    python clean_database.py              # preview — no deletes
    python clean_database.py --delete    # actually delete

Uses MUFTI_DATABASE_URL if set (same as scraper/API), else sqlite:///fatawa.db
relative to this script's directory when the path is relative.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_DB_URL = "sqlite:///fatawa.db"

BANURI_SOURCE = "Jamia Binoria (Darul Ifta)"

# Noise markers in question field (any match => candidate row)
NOISE_MARKERS = (
    "اسلامی نام",
    "مسنون و ماثور دعائیں",
    "نشر و اشاعت",
)


def resolve_sqlite_database_path() -> Path:
    url = os.environ.get("MUFTI_DATABASE_URL", DEFAULT_DB_URL).strip()
    if not url.startswith("sqlite:///"):
        print(
            "clean_database.py only supports SQLite URLs like sqlite:///path/to.db\n"
            f"Got: {url!r}\n"
            "Unset MUFTI_DATABASE_URL or point it at sqlite:///fatawa.db",
            file=sys.stderr,
        )
        sys.exit(1)
    raw = url[len("sqlite:///") :].lstrip("/")
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def build_where_clause() -> tuple[str, list[str]]:
    conds = []
    params: list[str] = [BANURI_SOURCE]
    for m in NOISE_MARKERS:
        conds.append("question LIKE ?")
        params.append(f"%{m}%")
    where = "source = ? AND (" + " OR ".join(conds) + ")"
    return where, params


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete noisy Banuri fatwas from sqlite (dry-run by default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would be deleted (default; no database changes)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete matching rows",
    )
    args = parser.parse_args()
    if args.delete and args.dry_run:
        parser.error("Use either --delete or --dry-run, not both")
    dry_run = not args.delete

    db_path = resolve_sqlite_database_path()
    if not db_path.is_file():
        print(f"Database file not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    where_sql, params = build_where_clause()

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()

        cur.execute(f"SELECT COUNT(*) FROM fatwas WHERE {where_sql}", params)
        noisy_count = cur.fetchone()[0]
        print(f"Noisy Banuri records found (matching markers): {noisy_count}")

        if noisy_count == 0:
            cur.execute(
                "SELECT COUNT(*) FROM fatwas WHERE source = ?", (BANURI_SOURCE,)
            )
            remaining = cur.fetchone()[0]
            print(f"Remaining Banuri fatwas: {remaining}")
            return

        if dry_run:
            print("Dry-run mode - no rows deleted. Pass --delete to remove them.")
            cur.execute(
                "SELECT COUNT(*) FROM fatwas WHERE source = ?", (BANURI_SOURCE,)
            )
            remaining = cur.fetchone()[0]
            print(f"Remaining Banuri fatwas (unchanged): {remaining}")
            return

        cur.execute(f"DELETE FROM fatwas WHERE {where_sql}", params)
        deleted = cur.rowcount
        conn.commit()
        print(f"Deleted records: {deleted}")

        cur.execute(
            "SELECT COUNT(*) FROM fatwas WHERE source = ?", (BANURI_SOURCE,)
        )
        remaining = cur.fetchone()[0]
        print(f"Remaining Banuri fatwas: {remaining}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
