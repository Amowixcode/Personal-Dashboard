import sqlite3

from app.db.migrate import run_migrations


def test_migrate_creates_expected_tables(tmp_path):
    db_path = tmp_path / "test.db"

    applied = run_migrations(db_path)

    assert applied == [1, 2]
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {
        "schema_migrations", "sources", "snapshots", "runs", "items", "user_overrides", "focus",
    } <= tables


def test_migrate_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"

    first = run_migrations(db_path)
    second = run_migrations(db_path)

    assert first == [1, 2]
    assert second == []

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    finally:
        conn.close()
    assert rows == [(1,), (2,)]
