"""Nightly retention job, registered through the same runner as collectors
so it shows up on the debug page if it stops working.

Spec: CLAUDE.md, "Retention".
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.db.connection import DEFAULT_DB_PATH, write_connection

_JITTER_MAX_S = 1800  # up to 30 minutes, mirrors the collector jobs' jitter intent
_DOWNSAMPLE_HOURLY_AFTER_DAYS = 7
_DOWNSAMPLE_DAILY_AFTER_DAYS = 90


@dataclass
class RetentionStatus:
    """In-process bookkeeping for the retention job, mirroring the columns
    `sources` tracks per collector. Deliberately NOT persisted to the
    database: retention isn't a `sources` row, and adding one purely to
    hang bookkeeping off of would leak a fake "retention" source into
    /summary's `sections` list, which iterates every enabled `sources` row.

    This means retention's status resets to all-None on every process
    restart. Accepted limitation for a personal, occasionally-restarted,
    single-process app -- not something this issue fixes.
    """

    last_run_at: str | None = None
    last_ok_at: str | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None
    consecutive_failures: int = 0


_status = RetentionStatus()


def _format_error(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def get_retention_status() -> dict:
    """A snapshot of the current in-process retention bookkeeping, for the
    debug page and its API.
    """
    return asdict(_status)


def _keep_latest_only(conn, source_id: int) -> None:
    conn.execute(
        """
        DELETE FROM snapshots
        WHERE source_id = ? AND id NOT IN (
            SELECT id FROM snapshots WHERE source_id = ?
            ORDER BY fetched_at DESC, id DESC LIMIT 1
        )
        """,
        (source_id, source_id),
    )


def _delete_older_than_days(conn, source_id: int, retention_days: int | None, now: datetime) -> None:
    if retention_days is None:
        return
    cutoff = (now - timedelta(days=retention_days)).isoformat()
    conn.execute(
        "DELETE FROM snapshots WHERE source_id = ? AND fetched_at < ?",
        (source_id, cutoff),
    )


def _thin_to_one_per_bucket(
    conn,
    source_id: int,
    older_than: str,
    newer_or_equal: str | None,
    bucket_len: int,
) -> None:
    """Keep only the newest snapshot per time bucket (a string prefix of
    fetched_at -- 13 chars for an hour bucket, 10 for a day bucket, since
    every fetched_at in this codebase is written as a UTC ISO 8601 string
    with a consistent format).
    """
    query = "SELECT id, fetched_at FROM snapshots WHERE source_id = ? AND fetched_at < ?"
    params: list = [source_id, older_than]
    if newer_or_equal is not None:
        query += " AND fetched_at >= ?"
        params.append(newer_or_equal)

    rows = conn.execute(query, params).fetchall()
    best: dict[str, tuple[str, int]] = {}
    for row_id, fetched_at in rows:
        bucket = fetched_at[:bucket_len]
        current = best.get(bucket)
        if current is None or fetched_at > current[0]:
            best[bucket] = (fetched_at, row_id)

    keep_ids = {row_id for _, row_id in best.values()}
    delete_ids = [row_id for row_id, _ in rows if row_id not in keep_ids]
    if delete_ids:
        conn.executemany(
            "DELETE FROM snapshots WHERE id = ?", [(row_id,) for row_id in delete_ids]
        )


def _downsample(conn, source_id: int, now: datetime) -> None:
    hourly_cutoff = (now - timedelta(days=_DOWNSAMPLE_HOURLY_AFTER_DAYS)).isoformat()
    daily_cutoff = (now - timedelta(days=_DOWNSAMPLE_DAILY_AFTER_DAYS)).isoformat()

    # 7-90 days old: one row per hour.
    _thin_to_one_per_bucket(conn, source_id, older_than=hourly_cutoff, newer_or_equal=daily_cutoff, bucket_len=13)
    # Older than 90 days: one row per day.
    _thin_to_one_per_bucket(conn, source_id, older_than=daily_cutoff, newer_or_equal=None, bucket_len=10)


def enforce_retention(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """One pass: apply each source's retention policy to its snapshots."""
    global _status
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    try:
        now = datetime.now(timezone.utc)
        with write_connection(db_path) as conn:
            conn.execute("BEGIN")
            try:
                sources = conn.execute("SELECT id, retention, retention_days FROM sources").fetchall()
                for source_id, retention, retention_days in sources:
                    if retention == "forever":
                        continue
                    elif retention == "latest_only":
                        _keep_latest_only(conn, source_id)
                    elif retention == "days":
                        _delete_older_than_days(conn, source_id, retention_days, now)
                    elif retention == "downsample":
                        _downsample(conn, source_id, now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        _status = RetentionStatus(
            last_run_at=started_at,
            last_ok_at=_status.last_ok_at,
            last_error=_format_error(exc),
            last_duration_ms=duration_ms,
            consecutive_failures=_status.consecutive_failures + 1,
        )
        raise
    else:
        duration_ms = int((time.monotonic() - start) * 1000)
        _status = RetentionStatus(
            last_run_at=started_at,
            last_ok_at=started_at,
            last_error=_status.last_error,
            last_duration_ms=duration_ms,
            consecutive_failures=0,
        )


def register_retention_job(
    scheduler: AsyncIOScheduler, db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """Register the nightly retention pass on an existing scheduler --
    "the same runner" as the collector jobs, per spec.
    """
    scheduler.add_job(
        enforce_retention,
        trigger=IntervalTrigger(hours=24, jitter=_JITTER_MAX_S),
        args=(db_path,),
        id="retention",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=None,
        replace_existing=True,
    )
