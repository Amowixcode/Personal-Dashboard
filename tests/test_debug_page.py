"""Tests for GET /debug.

/debug is a client-rendered shell exactly like index.html (see
test_front_page.py precedent): the server only embeds JSON into a
<script type="application/json"> tag, and app/web/static/debug.js builds
the actual DOM, including the CSS class that visually distinguishes a
"never succeeded" source. Without a browser/JS runtime in this stack (per
CLAUDE.md: no frontend toolchain), the acceptance criterion is verified
two ways: the embedded server-side data has the right last_ok_at values,
and the shipped debug.js contains the conditional that attaches the
distinguishing class.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import app.api.sources as sources_module
from app.db.connection import write_connection
from app.db.migrate import run_migrations
from app.main import app

_DEBUG_JS = Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "debug.js"


def _client_with_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    monkeypatch.setattr(sources_module, "DEFAULT_DB_PATH", db_path)
    return TestClient(app), db_path


def test_debug_page_renders(tmp_path, monkeypatch):
    client, _ = _client_with_db(tmp_path, monkeypatch)

    response = client.get("/debug")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<script src="/static/debug.js">' in response.text


def test_debug_page_embeds_never_succeeded_vs_recent_success(tmp_path, monkeypatch):
    client, db_path = _client_with_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).isoformat()

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO sources (name, interval_s, retention) VALUES ('never', 60, 'forever')")
        conn.execute(
            "INSERT INTO sources (name, interval_s, retention, last_run_at, last_ok_at) "
            "VALUES ('fresh', 60, 'forever', ?, ?)",
            (now, now),
        )
        conn.commit()

    body = client.get("/debug").text
    start = body.index('id="initial-debug"')
    json_start = body.index(">", start) + 1
    json_end = body.index("</script>", json_start)
    embedded = json.loads(body[json_start:json_end])

    by_name = {s["name"]: s for s in embedded["sources"]}
    assert by_name["never"]["last_ok_at"] is None
    assert by_name["fresh"]["last_ok_at"] is not None
    assert "retention" in embedded


def test_debug_js_renders_never_succeeded_sources_distinctly():
    js = _DEBUG_JS.read_text(encoding="utf-8")
    assert "never-succeeded" in js
