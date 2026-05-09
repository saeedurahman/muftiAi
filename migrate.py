"""Safe idempotent SQLite migration script for Mufti AI.

Usage:
    python migrate.py
    python migrate.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import sqlite as sqlite_dialect

from mufti_scraper.db.models import Base


DEFAULT_DB_URL = "sqlite:///fatawa.db"


def _supports_symbol(symbol: str) -> bool:
    enc = sys.stdout.encoding or "utf-8"
    try:
        symbol.encode(enc)
        return True
    except Exception:
        return False


OK = "✓" if _supports_symbol("✓") else "[OK]"
WARN = "⚠" if _supports_symbol("⚠") else "[WARN]"
ERR = "✗" if _supports_symbol("✗") else "[ERR]"


def parse_sqlite_path(database_url: str) -> Path:
    """Resolve sqlite:/// URL to filesystem path."""
    if not database_url.startswith("sqlite:///"):
        raise ValueError(
            "Only sqlite URLs are supported by migrate.py. "
            "Example: sqlite:///fatawa.db"
        )

    raw = database_url.replace("sqlite:///", "", 1)
    if not raw:
        raw = "fatawa.db"
    return Path(raw).resolve()


def get_existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(r[1]) for r in rows}


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return column_name in get_table_columns(conn, table_name)


def run_create_tables(conn: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    """Create all tables from SQLAlchemy models using IF NOT EXISTS."""
    created = 0
    skipped = 0

    existing_before = get_existing_tables(conn)
    dialect = sqlite_dialect.dialect()

    for table in Base.metadata.sorted_tables:
        table_name = str(table.name)
        if table_name in existing_before:
            print(f"{WARN} Table '{table_name}' already exists, skipping")
            skipped += 1
            continue

        stmt = str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip()
        if dry_run:
            print(f"DRY-RUN: would create table '{table_name}'")
            print(f"  SQL: {stmt}")
        else:
            conn.execute(stmt)
            print(f"{OK} Created table '{table_name}'")
        created += 1

    return created, skipped


def run_add_columns(conn: sqlite3.Connection, dry_run: bool) -> tuple[int, int, int]:
    """Add required columns to existing tables safely and idempotently."""
    operations: list[tuple[str, str, str]] = [
        ("users", "role", "ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user'"),
        (
            "users",
            "dashboard_access",
            "ALTER TABLE users ADD COLUMN dashboard_access BOOLEAN DEFAULT 0",
        ),
        (
            "questions",
            "assigned_mufti_id",
            "ALTER TABLE questions ADD COLUMN assigned_mufti_id INTEGER REFERENCES users(id)",
        ),
        ("questions", "assigned_at", "ALTER TABLE questions ADD COLUMN assigned_at DATETIME"),
        ("questions", "answered_at", "ALTER TABLE questions ADD COLUMN answered_at DATETIME"),
        (
            "questions",
            "payment_amount",
            "ALTER TABLE questions ADD COLUMN payment_amount FLOAT",
        ),
        (
            "questions",
            "payment_status",
            "ALTER TABLE questions ADD COLUMN payment_status VARCHAR(20) DEFAULT 'unpaid'",
        ),
    ]

    added = 0
    skipped = 0
    failed = 0
    existing_tables = get_existing_tables(conn)

    for table_name, column_name, alter_sql in operations:
        try:
            if table_name not in existing_tables:
                print(
                    f"{WARN} Table '{table_name}' not found, skipping column '{column_name}' "
                    "(table may be created by models on fresh DB)"
                )
                skipped += 1
                continue

            if column_exists(conn, table_name, column_name):
                print(f"{WARN} Column '{column_name}' already exists in {table_name}, skipping")
                skipped += 1
                continue

            if dry_run:
                print(f"DRY-RUN: would add column '{column_name}' to {table_name}")
                print(f"  SQL: {alter_sql}")
            else:
                conn.execute(alter_sql)
                print(f"{OK} Added column '{column_name}' to {table_name}")
            added += 1
        except Exception as exc:  # pragma: no cover - defensive runtime safety
            print(f"{ERR} Failed adding column '{column_name}' to {table_name}: {exc}")
            failed += 1

    return added, skipped, failed


def verify_schema(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    expected_columns = {
        "users": {"role", "dashboard_access"},
        "questions": {
            "assigned_mufti_id",
            "assigned_at",
            "answered_at",
            "payment_amount",
            "payment_status",
        },
    }
    expected_tables = {"muftis", "mufti_payments"}

    problems: list[str] = []
    existing_tables = get_existing_tables(conn)

    for table_name in sorted(expected_tables):
        if table_name not in existing_tables:
            problems.append(f"Missing table: {table_name}")

    for table_name, cols in expected_columns.items():
        if table_name not in existing_tables:
            problems.append(f"Missing table: {table_name}")
            continue
        existing_cols = get_table_columns(conn, table_name)
        for col in sorted(cols):
            if col not in existing_cols:
                problems.append(f"Missing column: {table_name}.{col}")

    return (len(problems) == 0), problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe idempotent sqlite migration script")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying the database",
    )
    args = parser.parse_args()

    db_url = os.environ.get("MUFTI_DATABASE_URL", DEFAULT_DB_URL).strip() or DEFAULT_DB_URL
    try:
        db_path = parse_sqlite_path(db_url)
    except ValueError as exc:
        print(f"{ERR} {exc}")
        return 2

    print("=== Mufti AI Migration ===")
    print(f"Database URL: {db_url}")
    print(f"Resolved SQLite file: {db_path}")
    if args.dry_run:
        print("Mode: DRY-RUN (no changes will be written)")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        print("\n[1/3] Creating tables (IF NOT EXISTS) from SQLAlchemy models...")
        created_tables, skipped_tables = run_create_tables(conn, args.dry_run)

        print("\n[2/3] Adding missing columns to existing tables...")
        added_cols, skipped_cols, failed_cols = run_add_columns(conn, args.dry_run)

        if args.dry_run:
            print("\nDry-run complete. No changes committed.")
            conn.rollback()
        else:
            conn.commit()

        print("\n[3/3] Verifying expected schema...")
        is_ok, problems = verify_schema(conn)

        print("\n=== Migration Summary ===")
        print(f"Tables created: {created_tables}")
        print(f"Tables skipped: {skipped_tables}")
        print(f"Columns added: {added_cols}")
        print(f"Columns skipped: {skipped_cols}")
        print(f"Column failures: {failed_cols}")

        if is_ok:
            print(f"{OK} Verification passed: all expected tables/columns exist.")
            print(f"{OK} Migration complete!")
            return 0 if failed_cols == 0 else 1

        if args.dry_run:
            print(
                f"{WARN} Verification reports missing items in current DB state "
                "(expected during dry-run before applying changes)."
            )
            for item in problems:
                print(f"  - {item}")
            print(f"{OK} Dry-run preview complete.")
            return 0

        print(f"{ERR} Verification found missing items:")
        for item in problems:
            print(f"  - {item}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
