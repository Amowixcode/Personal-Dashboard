import json
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.summary import build_summary
from app.api.summary import router as summary_router
from app.config import settings
from app.db.migrate import run_migrations

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
    run_migrations()
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(summary_router)
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
