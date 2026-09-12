import asyncio

from app.collect import runner as collect_runner
from app.collect.registry import register_collectors
from app.collect.sources.dummy import DummyCollector
from app.db.connection import write_connection
from app.db.migrate import run_migrations
from app.project import registry as project_registry
from app.project.base import DerivedItem
from app.project.reproject import apply_overrides, reproject


def _run(coro):
    return asyncio.run(coro)


def _items_for(db_path, source_id):
    with write_connection(db_path) as conn:
        return conn.execute(
            "SELECT external_id, title, completed_at FROM items "
            "WHERE source_id = ? AND origin = 'derived' ORDER BY external_id",
            (source_id,),
        ).fetchall()


def _dummy_source_id(db_path):
    with write_connection(db_path) as conn:
        return conn.execute("SELECT id FROM sources WHERE name = 'dummy'").fetchone()[0]


def test_reproject_rewrites_all_history_not_just_new_rows(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = DummyCollector(
        payloads=iter(
            [
                {"counter": 1, "timestamp": "t1"},
                {"counter": 2, "timestamp": "t2"},
                {"counter": 3, "timestamp": "t3"},
            ]
        )
    )
    register_collectors(db_path, collectors=[collector])
    for _ in range(3):
        _run(collect_runner.run_once(collector, db_path))

    source_id = _dummy_source_id(db_path)
    before = _items_for(db_path, source_id)
    assert [row[1] for row in before] == ["Dummy tick #1", "Dummy tick #2", "Dummy tick #3"]

    class RenamedDummyProjector:
        source_name = "dummy"

        def project(self, snapshot):
            counter = snapshot.payload["counter"]
            return [
                DerivedItem(
                    external_id=str(counter),
                    kind="task",
                    title=f"Renamed tick #{counter}",
                    section="ops",
                )
            ]

    original = project_registry._PROJECTORS["dummy"]
    project_registry._PROJECTORS["dummy"] = RenamedDummyProjector()
    try:
        reproject("dummy", db_path)
    finally:
        project_registry._PROJECTORS["dummy"] = original

    after = _items_for(db_path, source_id)
    assert [row[1] for row in after] == ["Renamed tick #1", "Renamed tick #2", "Renamed tick #3"]


def test_user_origin_items_untouched_by_reproject(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = DummyCollector(payloads=iter([{"counter": 1, "timestamp": "t1"}]))
    register_collectors(db_path, collectors=[collector])
    _run(collect_runner.run_once(collector, db_path))

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO items
                (kind, title, actionable, section, origin, source_id, external_id,
                 created_at, updated_at)
            VALUES ('task', 'my own task', 1, 'today', 'user', NULL, NULL,
                    '2026-09-12T00:00:00+00:00', '2026-09-12T00:00:00+00:00')
            """
        )
        conn.commit()

    reproject("dummy", db_path)

    with write_connection(db_path) as conn:
        row = conn.execute(
            "SELECT title FROM items WHERE origin = 'user'"
        ).fetchone()
    assert row == ("my own task",)


def test_override_survives_reproject_and_source_running_again(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = DummyCollector(payloads=iter([{"counter": 1, "timestamp": "t1"}]))
    register_collectors(db_path, collectors=[collector])
    _run(collect_runner.run_once(collector, db_path))
    source_id = _dummy_source_id(db_path)

    override_value = "2026-09-12T12:00:00+00:00"
    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO user_overrides
                (entity_type, source_id, external_id, field, value, created_at)
            VALUES ('item', ?, '1', 'completed_at', ?, '2026-09-12T00:00:00+00:00')
            """,
            (source_id, override_value),
        )
        apply_overrides(conn, source_id=source_id)
        conn.commit()

    def completed_at():
        with write_connection(db_path) as conn:
            return conn.execute(
                "SELECT completed_at FROM items WHERE source_id = ? AND external_id = '1'",
                (source_id,),
            ).fetchone()[0]

    assert completed_at() == override_value

    reproject("dummy", db_path)
    assert completed_at() == override_value

    # Source "runs again": same conceptual item (external_id stays "1") but
    # the payload differs, so it's a genuinely new snapshot, not a dedup --
    # this re-triggers the runner's incremental projection + apply_overrides.
    collector._payloads = iter([{"counter": 1, "timestamp": "t2"}])
    _run(collect_runner.run_once(collector, db_path))

    assert completed_at() == override_value
