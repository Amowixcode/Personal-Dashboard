"""Tests for GET /api/sources and POST /api/sources/{name}/run.

GET is tested against a bare TestClient (no lifespan involved: the read
path only needs app.api.sources.DEFAULT_DB_PATH, mirroring
app.api.summary's own test convention in test_summary.py).

POST needs request.app.state.collectors, which only exists once
app.main's lifespan has actually run, so those tests use
`with TestClient(app) as client:` and monkeypatch app.main.DEFAULT_DB_PATH
*before* entering the context (lifespan startup runs at __enter__).
"""

import asyncio
import concurrent.futures
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.api.sources as sources_module
import app.main as main_module
from app.collect.base import Retention
from app.db.connection import write_connection
from app.db.migrate import run_migrations
from app.main import app


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _insert_source(conn, name, interval_s=300, last_run_at=None, last_ok_at=None,
                    last_error=None, last_duration_ms=None, consecutive_failures=0):
    conn.execute(
        """
        INSERT INTO sources
            (name, interval_s, retention, last_run_at, last_ok_at, last_error,
             last_duration_ms, consecutive_failures)
        VALUES (?, ?, 'forever', ?, ?, ?, ?, ?)
        """,
        (name, interval_s, last_run_at, last_ok_at, last_error, last_duration_ms,
         consecutive_failures),
    )
    return conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()[0]


def _insert_run(conn, source_id, started_at, finished_at=None, ok=1, error=None, duration_ms=10):
    conn.execute(
        "INSERT INTO runs (source_id, started_at, finished_at, ok, error, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, started_at, finished_at, ok, error, duration_ms),
    )


def _get_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    monkeypatch.setattr(sources_module, "DEFAULT_DB_PATH", db_path)
    return TestClient(app), db_path


def test_get_sources_returns_shape_with_recent_runs_capped_at_20(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        source_id = _insert_source(conn, "dummy", interval_s=5, last_run_at=_iso(now), last_ok_at=_iso(now))
        for i in range(25):
            _insert_run(conn, source_id, _iso(now - timedelta(seconds=i)))
        conn.commit()

    response = client.get("/api/sources")

    assert response.status_code == 200
    source = response.json()[0]
    assert source["name"] == "dummy"
    assert source["interval_s"] == 5
    assert source["age_s"] is not None
    assert len(source["recent_runs"]) == 20
    started = [r["started_at"] for r in source["recent_runs"]]
    assert started == sorted(started, reverse=True)


def test_get_sources_marks_never_succeeded_source(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        _insert_source(conn, "never", interval_s=60)
        conn.commit()

    source = client.get("/api/sources").json()[0]
    assert source["last_ok_at"] is None
    assert source["age_s"] is None


def _configure_post_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", db_path)
    return db_path


def test_run_now_triggers_a_real_fetch_and_the_row_updates(tmp_path, monkeypatch):
    _configure_post_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.post("/api/sources/dummy/run")
        assert response.status_code == 200
        assert response.json() == {"name": "dummy", "ran": True}

        follow_up = client.get("/api/sources").json()
        dummy = next(s for s in follow_up if s["name"] == "dummy")
        assert dummy["last_ok_at"] is not None
        assert len(dummy["recent_runs"]) == 1
        assert dummy["recent_runs"][0]["ok"] is True


def test_run_now_404_for_unknown_source(tmp_path, monkeypatch):
    _configure_post_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.post("/api/sources/does-not-exist/run")

    assert response.status_code == 404


def test_run_now_409_when_already_running(tmp_path, monkeypatch):
    """A genuine concurrent-HTTP test: Starlette's TestClient runs the app
    through an anyio blocking portal, which accepts calls from multiple
    threads concurrently, so two real overlapping POSTs is practical here.
    """
    db_path = _configure_post_db(tmp_path, monkeypatch)

    class SlowCollector:
        name = "slow"
        interval_s = 60
        retention = Retention.LATEST_ONLY
        retention_days = None
        timeout_s = 5.0

        async def fetch(self):
            await asyncio.sleep(0.3)
            return {"ok": True}

    with TestClient(app) as client:
        with write_connection(db_path) as conn:
            conn.execute("BEGIN")
            _insert_source(conn, "slow", interval_s=60)
            conn.commit()
        app.state.collectors["slow"] = SlowCollector()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.post, "/api/sources/slow/run")
            time.sleep(0.05)  # let the first request take the guard first
            second = pool.submit(client.post, "/api/sources/slow/run")
            statuses = {first.result().status_code, second.result().status_code}

    assert statuses == {200, 409}
