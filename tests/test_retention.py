from datetime import datetime, timedelta, timezone

from app.db.connection import write_connection
from app.db.migrate import run_migrations
from app.retention import enforce_retention, register_retention_job
from app.collect.runner import build_scheduler
from app.collect.sources.dummy import DummyCollector


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _insert_source(conn, name, retention, retention_days=None):
    conn.execute(
        "INSERT INTO sources (name, interval_s, retention, retention_days, enabled) "
        "VALUES (?, 300, ?, ?, 1)",
        (name, retention, retention_days),
    )
    return conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()[0]


def _insert_snapshot(conn, source_id, fetched_at):
    conn.execute(
        "INSERT INTO snapshots (source_id, fetched_at, last_seen_at, payload_hash, payload) "
        "VALUES (?, ?, ?, 'hash', '{}')",
        (source_id, fetched_at, fetched_at),
    )


def _snapshot_count(db_path, source_id):
    with write_connection(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE source_id = ?", (source_id,)
        ).fetchone()[0]


def _fetched_ats(db_path, source_id):
    with write_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT fetched_at FROM snapshots WHERE source_id = ? ORDER BY fetched_at",
            (source_id,),
        ).fetchall()
    return [row[0] for row in rows]


def test_forever_deletes_nothing(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    now = datetime.now(timezone.utc)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        source_id = _insert_source(conn, "keepall", "forever")
        for days_ago in (1, 100, 500):
            _insert_snapshot(conn, source_id, _iso(now - timedelta(days=days_ago)))
        conn.commit()

    enforce_retention(db_path)

    assert _snapshot_count(db_path, source_id) == 3


def test_latest_only_keeps_newest_snapshot(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    now = datetime.now(timezone.utc)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        source_id = _insert_source(conn, "archive", "latest_only")
        for days_ago in (5, 2, 0):
            _insert_snapshot(conn, source_id, _iso(now - timedelta(days=days_ago)))
        conn.commit()

    enforce_retention(db_path)

    remaining = _fetched_ats(db_path, source_id)
    assert len(remaining) == 1
    assert remaining[0] == _iso(now - timedelta(days=0))


def test_days_deletes_older_than_retention_days(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    now = datetime.now(timezone.utc)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        source_id = _insert_source(conn, "ephemeral", "days", retention_days=30)
        _insert_snapshot(conn, source_id, _iso(now - timedelta(days=10)))
        _insert_snapshot(conn, source_id, _iso(now - timedelta(days=40)))
        conn.commit()

    enforce_retention(db_path)

    remaining = _fetched_ats(db_path, source_id)
    assert len(remaining) == 1
    assert remaining[0] == _iso(now - timedelta(days=10))


def test_downsample_thins_old_snapshots_to_hourly_and_daily(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    now = datetime.now(timezone.utc)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        source_id = _insert_source(conn, "measurements", "downsample")

        # Recent (< 7 days): all kept, even multiple in the same hour.
        recent_hour = now - timedelta(days=1)
        _insert_snapshot(conn, source_id, _iso(recent_hour))
        _insert_snapshot(conn, source_id, _iso(recent_hour + timedelta(minutes=10)))

        # 7-90 days old: three snapshots in the same hour bucket -> collapse to 1.
        old_hour = (now - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
        _insert_snapshot(conn, source_id, _iso(old_hour))
        _insert_snapshot(conn, source_id, _iso(old_hour + timedelta(minutes=15)))
        _insert_snapshot(conn, source_id, _iso(old_hour + timedelta(minutes=45)))

        # Older than 90 days: three snapshots on the same day -> collapse to 1.
        ancient_day = (now - timedelta(days=200)).replace(hour=0, minute=0, second=0, microsecond=0)
        _insert_snapshot(conn, source_id, _iso(ancient_day))
        _insert_snapshot(conn, source_id, _iso(ancient_day + timedelta(hours=5)))
        _insert_snapshot(conn, source_id, _iso(ancient_day + timedelta(hours=10)))

        conn.commit()

    enforce_retention(db_path)

    remaining = _fetched_ats(db_path, source_id)
    assert len(remaining) == 2 + 1 + 1  # recent both kept, one per older bucket


def test_register_retention_job_is_configured_per_spec(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    scheduler = build_scheduler([DummyCollector()], db_path)

    register_retention_job(scheduler, db_path)

    job = scheduler.get_job("retention")
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time is None
    assert job.trigger.interval.total_seconds() == 24 * 3600
