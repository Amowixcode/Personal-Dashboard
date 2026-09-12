import sqlite3

import pytest

from app.db.connection import write_connection
from app.db.migrate import run_migrations


def _insert_source(conn, name):
    conn.execute("BEGIN")
    cur = conn.execute(
        "INSERT INTO sources (name, interval_s, retention, enabled) VALUES (?, 60, 'forever', 1)",
        (name,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_item(conn, **fields):
    conn.execute("BEGIN")
    conn.execute(
        """
        INSERT INTO items
            (kind, title, actionable, section, origin, source_id, external_id,
             created_at, updated_at)
        VALUES (:kind, :title, :actionable, :section, :origin, :source_id,
                :external_id, :created_at, :updated_at)
        """,
        fields,
    )
    conn.commit()


def test_two_user_items_with_null_source_and_external_id_both_insert(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)

    with write_connection(db_path) as conn:
        for title in ("first user task", "second user task"):
            _insert_item(
                conn,
                kind="task",
                title=title,
                actionable=1,
                section="today",
                origin="user",
                source_id=None,
                external_id=None,
                created_at="2026-09-12T00:00:00Z",
                updated_at="2026-09-12T00:00:00Z",
            )

        count = conn.execute("SELECT COUNT(*) FROM items WHERE origin='user'").fetchone()[0]
    assert count == 2


def test_duplicate_source_and_external_id_rejected_for_derived_items(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)

    with write_connection(db_path) as conn:
        source_id = _insert_source(conn, "gcal")

        _insert_item(
            conn,
            kind="deadline",
            title="first derived item",
            actionable=0,
            section="calendar",
            origin="derived",
            source_id=source_id,
            external_id="evt-1",
            created_at="2026-09-12T00:00:00Z",
            updated_at="2026-09-12T00:00:00Z",
        )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_item(
                conn,
                kind="deadline",
                title="duplicate derived item",
                actionable=0,
                section="calendar",
                origin="derived",
                source_id=source_id,
                external_id="evt-1",
                created_at="2026-09-12T00:00:00Z",
                updated_at="2026-09-12T00:00:00Z",
            )
        conn.rollback()
