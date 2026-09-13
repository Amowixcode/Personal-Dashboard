"""GET /summary: the front page's one call.

Spec: CLAUDE.md, "/summary", "HTTP endpoints", "Time". Only absolute
timestamps, UTC with Z. No countdowns or relative-time text computed here --
that is the client's job, which is what lets this endpoint be cached and the
front page paint immediately. Reads only (via read_connection: no write
lock, no lock contention with a running collector), no network calls.

`next_event` is always null in this issue: nothing in the repo yet
populates a calendar-derived "next event". Returning null here is correct,
not a placeholder to fill in later without an issue for it. `focus` is
populated from the single-row `focus` table that issue 8's PATCH
/api/items/{id} writes to (see app.api.items) -- a focused item that has
since been completed or dismissed reads back as no focus, rather than
resurrecting it in the response.

`sections` has one entry per enabled source (per the issue's own scope text:
status is "derived from sources.last_ok_at age and consecutive_failures"),
not per item category -- there is no column anywhere mapping a source to one
of items.section's six values, so a source is the only unit this can be
computed from today.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from app.db.connection import DEFAULT_DB_PATH, read_connection

router = APIRouter()

_OSLO = ZoneInfo("Europe/Oslo")
_THIS_WEEK_DAYS = 7

# A source is "stale" once it's gone this many multiples of its own interval
# without a success -- the exact rule named in the acceptance criteria.
_STALE_INTERVAL_MULTIPLIER = 3
# Below this many consecutive failures a source is "attention"; at or above
# it, "error". Not pinned down by the spec beyond the stale rule above --
# chosen so a couple of transient failures reads as a warning, and a
# persistent run of failures (each already survived two in-run retries, per
# the runner's own retry logic) reads as a hard error.
_ERROR_FAILURE_THRESHOLD = 3


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _now_iso_z() -> str:
    return _iso_z(datetime.now(timezone.utc))


def _oslo_day_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    """Start of today and start of tomorrow, in Europe/Oslo, expressed as
    UTC instants -- due_at is stored and compared as UTC.
    """
    now_oslo = now_utc.astimezone(_OSLO)
    start_of_today = now_oslo.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_tomorrow = start_of_today + timedelta(days=1)
    return start_of_today.astimezone(timezone.utc), start_of_tomorrow.astimezone(timezone.utc)


def _row_to_item(row: tuple) -> dict[str, Any]:
    item_id, kind, title, due_at, actionable = row
    return {
        "id": item_id,
        "kind": kind,
        "title": title,
        "due_at": _iso_z(_parse(due_at)) if due_at else None,
        "actionable": bool(actionable),
    }


def _source_status(
    last_ok_at: str | None, interval_s: int, consecutive_failures: int, now_utc: datetime
) -> str:
    if last_ok_at is not None:
        age_s = (now_utc - _parse(last_ok_at)).total_seconds()
        if age_s > interval_s * _STALE_INTERVAL_MULTIPLIER:
            return "stale"
    if consecutive_failures >= _ERROR_FAILURE_THRESHOLD:
        return "error"
    if consecutive_failures >= 1:
        return "attention"
    return "ok"


def _section_for_source(
    name: str,
    interval_s: int,
    last_ok_at: str | None,
    last_error: str | None,
    consecutive_failures: int,
    now_utc: datetime,
) -> dict[str, Any]:
    status = _source_status(last_ok_at, interval_s, consecutive_failures, now_utc)
    if status == "ok":
        message = "running normally"
    elif status == "stale":
        message = "no recent successful run"
    else:
        message = last_error or f"{consecutive_failures} consecutive failures"

    return {
        "key": name,
        "label": name,
        "status": status,
        "message": message,
        "as_of": _iso_z(_parse(last_ok_at)) if last_ok_at else None,
    }


def build_summary(db_path: Path | str | None = None) -> dict[str, Any]:
    # Resolved as a module global at call time (not bound as a default
    # parameter value at import time) so tests can monkeypatch
    # app.api.summary.DEFAULT_DB_PATH and have it take effect -- otherwise
    # the FastAPI route, which calls build_summary() with no argument,
    # would always hit the real on-disk default regardless of monkeypatching.
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    now_utc = datetime.now(timezone.utc)
    today_start, today_end = _oslo_day_bounds(now_utc)
    week_end = today_end + timedelta(days=_THIS_WEEK_DAYS)

    with read_connection(db_path) as conn:
        today_rows = conn.execute(
            "SELECT id, kind, title, due_at, actionable FROM items "
            "WHERE completed_at IS NULL AND dismissed_at IS NULL "
            "AND due_at >= ? AND due_at < ? ORDER BY due_at ASC",
            (_iso_z(today_start), _iso_z(today_end)),
        ).fetchall()

        week_rows = conn.execute(
            "SELECT id, kind, title, due_at, actionable FROM items "
            "WHERE completed_at IS NULL AND dismissed_at IS NULL "
            "AND due_at >= ? AND due_at < ? ORDER BY due_at ASC",
            (_iso_z(today_end), _iso_z(week_end)),
        ).fetchall()

        source_rows = conn.execute(
            "SELECT name, interval_s, last_ok_at, last_error, consecutive_failures "
            "FROM sources WHERE enabled = 1"
        ).fetchall()

        focus_row = conn.execute(
            "SELECT items.id, items.title FROM focus "
            "JOIN items ON items.id = focus.item_id "
            "WHERE focus.id = 1 AND items.completed_at IS NULL AND items.dismissed_at IS NULL"
        ).fetchone()

    sections = []
    stale_sources = []
    for name, interval_s, last_ok_at, last_error, consecutive_failures in source_rows:
        section = _section_for_source(
            name, interval_s, last_ok_at, last_error, consecutive_failures, now_utc
        )
        sections.append(section)
        if section["status"] == "stale":
            stale_sources.append(name)

    return {
        "generated_at": _now_iso_z(),
        "next_event": None,
        "focus": {"id": focus_row[0], "title": focus_row[1]} if focus_row else None,
        "today": [_row_to_item(r) for r in today_rows],
        "this_week": [_row_to_item(r) for r in week_rows],
        "sections": sections,
        "stale_sources": stale_sources,
    }


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return build_summary()
