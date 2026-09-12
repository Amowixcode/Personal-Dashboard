import json
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.sources import build_sources_status
from app.api.sources import router as sources_router
from app.api.summary import build_summary
from app.api.summary import router as summary_router
from app.collect.registry import register_collectors
from app.collect.runner import build_scheduler
from app.config import settings
from app.db.connection import DEFAULT_DB_PATH
from app.db.migrate import run_migrations
from app.retention import get_retention_status, register_retention_job

_WEB_DIR = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=_WEB_DIR / "templates")


def configure_logging() -> None:
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "dashboard.log", maxBytes=1_000_000, backupCount=3
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Resolved as a module global at call time, not bound at import time,
    # so tests can monkeypatch app.main.DEFAULT_DB_PATH before entering
    # `with TestClient(app) as client:` (which is what actually invokes
    # this function) and have every call below use it.
    db_path = DEFAULT_DB_PATH
    run_migrations(db_path)
    collectors = register_collectors(db_path)
    scheduler = build_scheduler(collectors, db_path)
    register_retention_job(scheduler, db_path)
    scheduler.start()
    app.state.scheduler = scheduler
    app.state.collectors = {c.name: c for c in collectors}
    app.state.db_path = db_path
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
app.include_router(summary_router)
app.include_router(sources_router)
app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def front_page(request: Request):
    summary = build_summary()
    # Escaped so a title/message containing "</script>" can't break out of
    # the embedded JSON's script tag.
    summary_json = json.dumps(summary).replace("<", "\\u003c")
    return templates.TemplateResponse(
        request, "index.html", {"summary_json": summary_json}
    )


@app.get("/debug")
def debug_page(request: Request):
    db_path = getattr(request.app.state, "db_path", None)
    debug_data = {
        "sources": build_sources_status(db_path),
        "retention": get_retention_status(),
    }
    debug_json = json.dumps(debug_data).replace("<", "\\u003c")
    return templates.TemplateResponse(
        request, "debug.html", {"debug_json": debug_json}
    )
