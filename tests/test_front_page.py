import json

import httpx
from fastapi.testclient import TestClient

import app.api.summary as summary_module
from app.db.migrate import run_migrations
from app.main import app


def _client_with_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    monkeypatch.setattr(summary_module, "DEFAULT_DB_PATH", db_path)
    return TestClient(app)


def test_front_page_renders_with_embedded_summary(tmp_path, monkeypatch):
    client = _client_with_db(tmp_path, monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Personal Dashboard" in body
    assert '<script src="/static/app.js">' in body
    assert '<link rel="stylesheet" href="/static/style.css">' in body

    start = body.index('id="initial-summary"')
    json_start = body.index(">", start) + 1
    json_end = body.index("</script>", json_start)
    embedded = json.loads(body[json_start:json_end])
    assert set(embedded.keys()) == {
        "generated_at", "next_event", "focus", "today", "this_week", "sections", "stale_sources",
    }


def test_front_page_references_no_external_host(tmp_path, monkeypatch):
    client = _client_with_db(tmp_path, monkeypatch)

    response = client.get("/")
    body = response.text

    for banned in ("http://", "https://", "cdn.", "//fonts.", "googleapis"):
        assert banned not in body, f"found external reference: {banned!r}"


def test_static_files_are_served(tmp_path, monkeypatch):
    client = _client_with_db(tmp_path, monkeypatch)

    css = client.get("/static/style.css")
    js = client.get("/static/app.js")

    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]


def test_front_page_makes_no_outbound_network_call(tmp_path, monkeypatch):
    client = _client_with_db(tmp_path, monkeypatch)

    def _blocked(self, *args, **kwargs):
        raise AssertionError("front page made an outbound HTTP call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)

    response = client.get("/")

    assert response.status_code == 200
