import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.api.summary import build_summary
from app.db.connection import write_connection
from app.db.migrate import run_migrations
from app.main import app


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _insert_source(conn, name, interval_s=300, last_ok_at=None, last_error=None,
                    consecutive_failures=0, enabled=1):
    conn.execute(
        """
        INSERT INTO sources
            (name, interval_s, retention, enabled, last_ok_at, last_error, consecutive_failures)
        VALUES (?, ?, 'forever', ?, ?, ?, ?)
        """,
        (name, interval_s, enabled, last_ok_at, last_error, consecutive_failures),
    )


def _insert_item(conn, due_at, completed_at=None, title="item", kind="task", actionable=0):
    now = _iso(datetime.now(timezone.utc))
    conn.execute(
        """
        INSERT INTO items
            (kind, title, due_at, completed_at, actionable, section, origin,
             source_id, external_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'ops', 'user', NULL, NULL, ?, ?)
        """,
        (kind, title, due_at, completed_at, actionable, now, now),
    )


def test_summary_shape(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)

    summary = build_summary(db_path)

    assert set(summary.keys()) == {
        "generated_at", "next_event", "focus", "today", "this_week", "sections", "stale_sources",
    }
    assert summary["generated_at"].endswith("Z")
    assert summary["next_event"] is None
    assert summary["focus"] is None
    assert summary["today"] == []
    assert summary["this_week"] == []
    assert summary["sections"] == []
    assert summary["stale_sources"] == []


def test_today_and_this_week_partitioning(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)

    now_utc = datetime.now(timezone.utc)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        _insert_item(conn, due_at=_iso(now_utc), title="due now")
        _insert_item(conn, due_at=_iso(now_utc + timedelta(days=3)), title="due in 3 days")
        _insert_item(conn, due_at=_iso(now_utc + timedelta(days=30)), title="due in 30 days")
        _insert_item(conn, due_at=_iso(now_utc), completed_at=_iso(now_utc), title="already done")
        conn.commit()

    summary = build_summary(db_path)

    today_titles = {item["title"] for item in summary["today"]}
    week_titles = {item["title"] for item in summary["this_week"]}

    assert "due now" in today_titles
    assert "already done" not in today_titles
    assert "already done" not in week_titles
    assert "due in 3 days" in week_titles
    assert "due in 3 days" not in today_titles
    assert "due in 30 days" not in week_titles
    assert "due in 30 days" not in today_titles

    for item in summary["today"] + summary["this_week"]:
        assert item["due_at"].endswith("Z")


def test_sections_status_and_stale_sources(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    now_utc = datetime.now(timezone.utc)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        _insert_source(conn, "healthy", interval_s=300, last_ok_at=_iso(now_utc), consecutive_failures=0)
        _insert_source(
            conn, "flaky", interval_s=300, last_ok_at=_iso(now_utc), consecutive_failures=1,
            last_error="boom",
        )
        _insert_source(
            conn, "broken", interval_s=300, last_ok_at=_iso(now_utc), consecutive_failures=5,
            last_error="RuntimeError: nope",
        )
        _insert_source(
            conn, "gone_stale", interval_s=60,
            last_ok_at=_iso(now_utc - timedelta(seconds=60 * 3 + 30)), consecutive_failures=0,
        )
        _insert_source(
            conn, "disabled", interval_s=60,
            last_ok_at=_iso(now_utc - timedelta(days=10)), enabled=0,
        )
        conn.commit()

    summary = build_summary(db_path)
    by_key = {section["key"]: section for section in summary["sections"]}

    assert "disabled" not in by_key
    assert by_key["healthy"]["status"] == "ok"
    assert by_key["flaky"]["status"] == "attention"
    assert by_key["broken"]["status"] == "error"
    assert by_key["gone_stale"]["status"] == "stale"
    assert summary["stale_sources"] == ["gone_stale"]


def test_summary_responds_fast_with_1000_items_and_10_sources(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    now_utc = datetime.now(timezone.utc)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        for i in range(10):
            _insert_source(conn, f"source{i}", last_ok_at=_iso(now_utc))
        for i in range(1000):
            _insert_item(conn, due_at=_iso(now_utc + timedelta(hours=i % 48)), title=f"item {i}")
        conn.commit()

    start = time.perf_counter()
    summary = build_summary(db_path)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05, f"build_summary took {elapsed * 1000:.1f}ms"
    assert len(summary["sections"]) == 10


def test_summary_endpoint_makes_no_outbound_network_call(tmp_path, monkeypatch):
    import httpx

    import app.api.summary as summary_module

    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    monkeypatch.setattr(summary_module, "DEFAULT_DB_PATH", db_path)

    # Patch httpx's real network transports specifically, not raw sockets or
    # Client.send: TestClient itself is an httpx.Client whose requests go
    # through an in-memory ASGI transport, not HTTPTransport/
    # AsyncHTTPTransport, so this catches a genuine outbound HTTP call
    # without breaking the test client's own request dispatch (patching
    # Client.send would block that too) or raw sockets (which the test
    # harness's own event-loop plumbing uses internally on Windows).
    def _blocked(self, *args, **kwargs):
        raise AssertionError("summary endpoint made an outbound HTTP call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)

    client = TestClient(app)
    response = client.get("/summary")

    assert response.status_code == 200
    body = response.json()
    assert "today" in body
