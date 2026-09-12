from app.collect.registry import discover_collectors, register_collectors
from app.collect.sources.dummy import DummyCollector
from app.db.connection import write_connection
from app.db.migrate import run_migrations


def test_discover_collectors_finds_dummy():
    collectors = discover_collectors()
    names = {c.name for c in collectors}
    assert "dummy" in names


def test_register_collectors_inserts_a_row(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = DummyCollector(interval_s=7)

    register_collectors(db_path, collectors=[collector])

    with write_connection(db_path) as conn:
        row = conn.execute(
            "SELECT interval_s, retention, retention_days, enabled FROM sources WHERE name = 'dummy'"
        ).fetchone()
    assert row == (7, "latest_only", None, 1)


def test_register_collectors_is_idempotent_and_preserves_enabled(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = DummyCollector(interval_s=7)
    register_collectors(db_path, collectors=[collector])

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        conn.execute("UPDATE sources SET enabled = 0 WHERE name = 'dummy'")
        conn.commit()

    register_collectors(db_path, collectors=[DummyCollector(interval_s=9)])

    with write_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sources WHERE name = 'dummy'").fetchone()[0]
        row = conn.execute(
            "SELECT interval_s, enabled FROM sources WHERE name = 'dummy'"
        ).fetchone()
    assert count == 1
    assert row == (9, 0)  # interval updated, enabled untouched by re-registration
