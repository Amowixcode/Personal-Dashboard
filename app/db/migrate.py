"""Applies numbered .sql files in app/db/migrations/, in order, exactly once."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.db.connection import DEFAULT_DB_PATH, write_connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_FILENAME_RE = re.compile(r"^(\d+)_.*\.sql$")


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = _FILENAME_RE.match(path.name)
        if match:
            files.append((int(match.group(1)), path))
    return sorted(files, key=lambda item: item[0])


def _statements(sql_text: str) -> list[str]:
    """Split a migration file into individual statements.

    Migration files here contain only CREATE TABLE / CREATE INDEX
    statements with no semicolons inside string literals or trigger
    bodies, so a plain split on ';' is sufficient.
    """
    return [s.strip() for s in sql_text.split(";") if s.strip()]


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return set()
        raise
    return {row[0] for row in rows}


def run_migrations(db_path: Path | str = DEFAULT_DB_PATH) -> list[int]:
    """Apply migration files not yet recorded in schema_migrations.

    Returns the version numbers newly applied (empty on a fully migrated
    database). Safe to call repeatedly.
    """
    applied: list[int] = []
    with write_connection(db_path) as conn:
        already = _applied_versions(conn)
        pending = [(v, p) for v, p in _migration_files() if v not in already]
        if not pending:
            return applied

        conn.execute("BEGIN")
        try:
            for version, path in pending:
                for statement in _statements(path.read_text(encoding="utf-8")):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(timezone.utc).isoformat()),
                )
                applied.append(version)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return applied
