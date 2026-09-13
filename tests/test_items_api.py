import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.api.items as items_module
from app.collect import runner as collect_runner
from app.collect.registry import register_collectors
from app.collect.sources.dummy import DummyCollector
from app.db.connection import write_connection
from app.db.migrate import run_migrations
from app.main import app
from app.project.reproject import reproject


def _run(coro):
    return asyncio.run(coro)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _get_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    monkeypatch.setattr(items_module, "DEFAULT_DB_PATH", db_path)
    return TestClient(app), db_path


def _make_derived_item(db_path):
    """One real dummy-source snapshot, projected into a single derived item
    with external_id '1', title 'Dummy tick #1', section 'ops'.
    """
    collector = DummyCollector(payloads=iter([{"counter": 1, "timestamp": "t1"}]))
    register_collectors(db_path, collectors=[collector])
    _run(collect_runner.run_once(collector, db_path))

    with write_connection(db_path) as conn:
        source_id = conn.execute("SELECT id FROM sources WHERE name = 'dummy'").fetchone()[0]
        item_id = conn.execute(
            "SELECT id FROM items WHERE source_id = ? AND external_id = '1'", (source_id,)
        ).fetchone()[0]
    return source_id, item_id


def _completed_at(db_path, item_id):
    with write_connection(db_path) as conn:
        return conn.execute(
            "SELECT completed_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()[0]


def test_post_item_creates_user_origin_row(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/items",
        json={"kind": "task", "title": "buy milk", "section": "today"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["origin"] == "user"
    assert body["source_id"] is None
    assert body["external_id"] is None
    assert body["title"] == "buy milk"

    with write_connection(db_path) as conn:
        row = conn.execute(
            "SELECT origin, source_id, external_id FROM items WHERE id = ?", (body["id"],)
        ).fetchone()
    assert row == ("user", None, None)


def test_patch_user_item_updates_directly(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    created = client.post(
        "/api/items", json={"kind": "task", "title": "old title", "section": "today"}
    ).json()

    response = client.patch(f"/api/items/{created['id']}", json={"title": "new title"})

    assert response.status_code == 200
    assert response.json()["title"] == "new title"
    with write_connection(db_path) as conn:
        title = conn.execute(
            "SELECT title FROM items WHERE id = ?", (created["id"],)
        ).fetchone()[0]
    assert title == "new title"


def test_patch_derived_item_writes_override_not_direct(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    source_id, item_id = _make_derived_item(db_path)

    now = _iso(datetime.now(timezone.utc))
    response = client.patch(f"/api/items/{item_id}", json={"completed_at": now})

    assert response.status_code == 200
    assert response.json()["completed_at"] is not None

    with write_connection(db_path) as conn:
        override = conn.execute(
            "SELECT field, value FROM user_overrides WHERE source_id = ? AND external_id = '1'",
            (source_id,),
        ).fetchone()
    assert override == ("completed_at", now)


def test_checked_off_derived_item_survives_reproject(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    source_id, item_id = _make_derived_item(db_path)

    now = _iso(datetime.now(timezone.utc))
    client.patch(f"/api/items/{item_id}", json={"completed_at": now})
    assert _completed_at(db_path, item_id) is not None

    reproject("dummy", db_path)

    with write_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, completed_at FROM items WHERE source_id = ? AND external_id = '1'",
            (source_id,),
        ).fetchone()
    assert row[1] is not None


def test_checked_off_derived_item_survives_source_running_again(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    source_id, item_id = _make_derived_item(db_path)

    now = _iso(datetime.now(timezone.utc))
    client.patch(f"/api/items/{item_id}", json={"completed_at": now})

    collector = DummyCollector(payloads=iter([{"counter": 1, "timestamp": "t2"}]))
    _run(collect_runner.run_once(collector, db_path))

    assert _completed_at(db_path, item_id) is not None


def test_delete_user_item_removes_row(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    created = client.post(
        "/api/items", json={"kind": "task", "title": "throwaway", "section": "today"}
    ).json()

    response = client.delete(f"/api/items/{created['id']}")

    assert response.status_code == 200
    with write_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM items WHERE id = ?", (created["id"],)
        ).fetchone()
    assert row is None


def test_delete_derived_item_hides_without_removing(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    source_id, item_id = _make_derived_item(db_path)

    response = client.delete(f"/api/items/{item_id}")

    assert response.status_code == 200
    with write_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, dismissed_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert row is not None
    assert row[1] is not None

    listed = client.get("/api/items").json()
    assert all(item["id"] != item_id for item in listed)

    listed_with_done = client.get("/api/items", params={"include_done": "true"}).json()
    assert all(item["id"] != item_id for item in listed_with_done)


def test_get_items_filters(tmp_path, monkeypatch):
    client, db_path = _get_client(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    early = client.post(
        "/api/items",
        json={
            "kind": "task",
            "title": "early",
            "section": "studies",
            "due_at": _iso(now),
        },
    ).json()
    late = client.post(
        "/api/items",
        json={
            "kind": "deadline",
            "title": "late",
            "section": "finance",
            "due_at": _iso(now + timedelta(days=10)),
        },
    ).json()
    client.patch(f"/api/items/{early['id']}", json={"completed_at": _iso(now)})

    by_section = client.get("/api/items", params={"section": "finance"}).json()
    assert [i["id"] for i in by_section] == [late["id"]]

    by_kind = client.get("/api/items", params={"kind": "deadline"}).json()
    assert [i["id"] for i in by_kind] == [late["id"]]

    by_due_before = client.get(
        "/api/items", params={"due_before": _iso(now + timedelta(days=1))}
    ).json()
    assert all(i["id"] != late["id"] for i in by_due_before)

    default_listing = client.get("/api/items").json()
    assert all(i["id"] != early["id"] for i in default_listing)

    with_done = client.get("/api/items", params={"include_done": "true"}).json()
    assert any(i["id"] == early["id"] for i in with_done)


def test_focus_marking_reflected_in_summary(tmp_path, monkeypatch):
    import app.api.summary as summary_module

    client, db_path = _get_client(tmp_path, monkeypatch)
    monkeypatch.setattr(summary_module, "DEFAULT_DB_PATH", db_path)

    first = client.post(
        "/api/items", json={"kind": "task", "title": "first", "section": "today"}
    ).json()
    second = client.post(
        "/api/items", json={"kind": "task", "title": "second", "section": "today"}
    ).json()

    client.patch(f"/api/items/{first['id']}", json={"focus": True})
    summary = client.get("/summary").json()
    assert summary["focus"] == {"id": first["id"], "title": "first"}

    client.patch(f"/api/items/{second['id']}", json={"focus": True})
    summary = client.get("/summary").json()
    assert summary["focus"] == {"id": second["id"], "title": "second"}
