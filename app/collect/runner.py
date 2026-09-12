"""One APScheduler job per collector.

Spec: CLAUDE.md, "Runner". Projector invocation and apply_overrides() are
out of scope for this issue (added in the issue that introduces the
Projector protocol): this module only fetches, hashes, writes snapshots and
runs rows, and updates sources bookkeeping columns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.collect.base import Collector
from app.db.connection import DEFAULT_DB_PATH, write_connection
from app.project.base import Snapshot
from app.project.reproject import apply_overrides, project_snapshot

logger = logging.getLogger(__name__)

_JITTER_MAX_S = 30
_RETRY_ATTEMPTS = 3  # one initial attempt + two retries
_RETRY_BASE_DELAY_S = 1.0  # doubles each retry: 1s, then 2s


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_error(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _is_retryable(exc: BaseException) -> bool:
    """Network errors and 5xx responses only, per spec."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


async def _fetch_with_retry(collector: Collector) -> object:
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncio.wait_for(collector.fetch(), timeout=collector.timeout_s)
        except TimeoutError:
            # Our own per-run cutoff. Never a "network error" for retry
            # purposes: a hung collector must be cut off at timeout_s, not
            # retried into 3x timeout_s plus backoff.
            raise
        except Exception as exc:
            if attempt >= _RETRY_ATTEMPTS or not _is_retryable(exc):
                raise
            delay = _RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            logger.warning(
                "collector %r attempt %d failed (%s), retrying in %.1fs",
                collector.name, attempt, _format_error(exc), delay,
            )
            await asyncio.sleep(delay)


def _source_id(conn, name: str) -> int | None:
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    return row[0] if row is not None else None


def _record_success(db_path, name: str, started_at: str, duration_ms: int, payload: object) -> None:
    finished_at = _now_iso()
    payload_json = json.dumps(payload, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        try:
            source_id = _source_id(conn, name)
            if source_id is None:
                conn.rollback()
                logger.error("run_once: source %r is not registered, skipping", name)
                return

            newest = conn.execute(
                "SELECT id, payload_hash FROM snapshots WHERE source_id = ? "
                "ORDER BY fetched_at DESC, id DESC LIMIT 1",
                (source_id,),
            ).fetchone()

            new_snapshot_id = None
            if newest is not None and newest[1] == payload_hash:
                conn.execute(
                    "UPDATE snapshots SET last_seen_at = ? WHERE id = ?",
                    (finished_at, newest[0]),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO snapshots (source_id, fetched_at, last_seen_at, payload_hash, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (source_id, finished_at, finished_at, payload_hash, payload_json),
                )
                new_snapshot_id = cur.lastrowid

            conn.execute(
                """
                INSERT INTO runs (source_id, started_at, finished_at, ok, error, duration_ms)
                VALUES (?, ?, ?, 1, NULL, ?)
                """,
                (source_id, started_at, finished_at, duration_ms),
            )
            # last_error is intentionally left as-is on success: the spec
            # only says to update last_run_at/last_ok_at/consecutive_failures
            # here, and keeping the last error visible after recovery is
            # useful for the debug page ("it failed 3 times yesterday").
            conn.execute(
                """
                UPDATE sources
                SET last_run_at = ?, last_ok_at = ?, last_duration_ms = ?, consecutive_failures = 0
                WHERE id = ?
                """,
                (finished_at, finished_at, duration_ms, source_id),
            )

            if new_snapshot_id is not None:
                # Projection is best-effort and isolated in its own
                # savepoint: raw snapshot capture is the reason payloads
                # are stored at all, and a broken projector must never
                # prevent that capture from being committed.
                conn.execute("SAVEPOINT project_new_snapshot")
                try:
                    snapshot = Snapshot(
                        id=new_snapshot_id,
                        source_id=source_id,
                        fetched_at=datetime.fromisoformat(finished_at),
                        last_seen_at=datetime.fromisoformat(finished_at),
                        payload=payload,
                    )
                    project_snapshot(conn, source_id, name, snapshot)
                    apply_overrides(conn, source_id=source_id)
                except Exception:
                    conn.execute("ROLLBACK TO project_new_snapshot")
                    logger.exception(
                        "projection failed for source %r snapshot %s", name, new_snapshot_id
                    )
                finally:
                    conn.execute("RELEASE project_new_snapshot")

            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _record_failure(db_path, name: str, started_at: str, duration_ms: int, error: str) -> None:
    finished_at = _now_iso()
    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        try:
            source_id = _source_id(conn, name)
            if source_id is None:
                conn.rollback()
                logger.error("run_once: source %r is not registered, skipping", name)
                return

            conn.execute(
                """
                INSERT INTO runs (source_id, started_at, finished_at, ok, error, duration_ms)
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (source_id, started_at, finished_at, error, duration_ms),
            )
            conn.execute(
                """
                UPDATE sources
                SET last_run_at = ?, last_error = ?, last_duration_ms = ?,
                    consecutive_failures = consecutive_failures + 1
                WHERE id = ?
                """,
                (finished_at, error, duration_ms, source_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


async def run_once(collector: Collector, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Run one collector once. Never raises: a collector's failure must
    never affect another collector's job or the scheduler.
    """
    started_at = _now_iso()
    start = time.monotonic()
    try:
        try:
            payload = await _fetch_with_retry(collector)
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            _record_failure(db_path, collector.name, started_at, duration_ms, _format_error(exc))
            return

        duration_ms = int((time.monotonic() - start) * 1000)
        _record_success(db_path, collector.name, started_at, duration_ms, payload)
    except Exception:
        logger.exception("run_once: unexpected error running collector %r", collector.name)


def build_scheduler(
    collectors: Sequence[Collector],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> AsyncIOScheduler:
    """Build (but do not start) a scheduler with one job per collector."""
    scheduler = AsyncIOScheduler()
    for collector in collectors:
        scheduler.add_job(
            run_once,
            trigger=IntervalTrigger(seconds=collector.interval_s, jitter=_JITTER_MAX_S),
            args=(collector, db_path),
            id=f"collector:{collector.name}",
            coalesce=True,
            max_instances=1,
            # No misfire grace period: a laptop can sleep for hours, and a
            # missed run must still coalesce into a single catch-up run on
            # wake, not be silently dropped once it's "too late" by
            # APScheduler's 1-second default.
            misfire_grace_time=None,
            replace_existing=True,
        )
    return scheduler
