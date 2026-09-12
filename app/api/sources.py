"""GET /api/sources and POST /api/sources/{name}/run: the debug page's
data, and its one control.

GET reads only, via read_connection(). POST looks the collector up in
request.app.state.collectors -- populated once at startup by app.main's
lifespan -- and calls run_once() directly, the same function the
scheduler's jobs call, so app.collect.runner's in-process concurrency
guard covers both paths identically.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.collect.runner import run_once
from app.db.connection import DEFAULT_DB_PATH, read_connection

router = APIRouter()

_RECENT_RUNS_LIMIT = 20


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _row_to_run(row: tuple) -> dict[str, Any]:
    run_id, started_at, finished_at, ok, error, duration_ms = row
    return {
        "id": run_id,
        "started_at": _iso_z(_parse(started_at)),
        "finished_at": _iso_z(_parse(finished_at)) if finished_at else None,
        "ok": bool(ok) if ok is not None else None,
        "error": error,
        "duration_ms": duration_ms,
    }


def build_sources_status(db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """One entry per row in `sources`, ordered by name.

    Resolved as a module global at call time (same reasoning as
    app.api.summary.build_summary) so tests can monkeypatch
    app.api.sources.DEFAULT_DB_PATH.
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    now_utc = datetime.now(timezone.utc)

    result: list[dict[str, Any]] = []
    with read_connection(db_path) as conn:
        source_rows = conn.execute(
            "SELECT id, name, interval_s, last_run_at, last_ok_at, last_error, "
            "last_duration_ms, consecutive_failures FROM sources ORDER BY name"
        ).fetchall()

        for (source_id, name, interval_s, last_run_at, last_ok_at, last_error,
             last_duration_ms, consecutive_failures) in source_rows:
            run_rows = conn.execute(
                "SELECT id, started_at, finished_at, ok, error, duration_ms FROM runs "
                "WHERE source_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
                (source_id, _RECENT_RUNS_LIMIT),
            ).fetchall()

            age_s = (now_utc - _parse(last_ok_at)).total_seconds() if last_ok_at else None
            result.append({
                "name": name,
                "interval_s": interval_s,
                "last_run_at": _iso_z(_parse(last_run_at)) if last_run_at else None,
                "last_ok_at": _iso_z(_parse(last_ok_at)) if last_ok_at else None,
                "age_s": age_s,
                "last_error": last_error,
                "last_duration_ms": last_duration_ms,
                "consecutive_failures": consecutive_failures,
                "recent_runs": [_row_to_run(r) for r in run_rows],
            })
    return result


@router.get("/api/sources")
def get_sources(request: Request) -> list[dict[str, Any]]:
    db_path = getattr(request.app.state, "db_path", None)
    return build_sources_status(db_path)


@router.post("/api/sources/{name}/run")
async def run_source_now(name: str, request: Request) -> dict[str, Any]:
    collectors = getattr(request.app.state, "collectors", {})
    collector = collectors.get(name)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {name}")

    db_path = getattr(request.app.state, "db_path", DEFAULT_DB_PATH)
    ran = await run_once(collector, db_path)
    if not ran:
        raise HTTPException(status_code=409, detail=f"{name} is already running")

    return {"name": name, "ran": True}
