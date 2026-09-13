"""Projects snapshots into items, applies user overrides, and reprocesses
a source's full history.

Spec: CLAUDE.md, "Reprocessing" and "Three classes of data". `origin='user'`
rows (source_id IS NULL) are never touched here -- every statement below
either targets `origin='derived'` explicitly or is scoped by `source_id`,
which user rows never have.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db.connection import DEFAULT_DB_PATH, write_connection
from app.project.base import Snapshot
from app.project.registry import get_projector

logger = logging.getLogger(__name__)

_ITEM_ENTITY_TYPE = "item"
# Public: app.api.items reuses this set as the allow-list of fields a PATCH
# may write, so both modules agree on what "an overridable field" means.
OVERRIDABLE_FIELDS = {
    "title",
    "detail",
    "due_at",
    "actionable",
    "section",
    "completed_at",
    "dismissed_at",
}


def project_snapshot(conn, source_id: int, source_name: str, snapshot: Snapshot) -> None:
    """Run source_name's projector on one snapshot and upsert the results
    into items on (source_id, external_id), origin='derived'. No-op if the
    source has no registered projector.
    """
    projector = get_projector(source_name)
    if projector is None:
        return

    now = datetime.now(timezone.utc).isoformat()
    for item in projector.project(snapshot):
        conn.execute(
            """
            INSERT INTO items
                (kind, title, detail, due_at, actionable, section, origin,
                 source_id, external_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'derived', ?, ?, ?, ?)
            ON CONFLICT(source_id, external_id) DO UPDATE SET
                kind = excluded.kind,
                title = excluded.title,
                detail = excluded.detail,
                due_at = excluded.due_at,
                actionable = excluded.actionable,
                section = excluded.section,
                updated_at = excluded.updated_at
            WHERE items.origin = 'derived'
            """,
            (
                item.kind,
                item.title,
                item.detail,
                item.due_at.isoformat() if item.due_at else None,
                int(item.actionable),
                item.section,
                source_id,
                item.external_id,
                now,
                now,
            ),
        )


def apply_overrides(conn, source_id: int | None = None) -> None:
    """Write the values in user_overrides onto matching items rows.

    Runs after every projection so reads stay a plain query with no join.
    """
    query = "SELECT source_id, external_id, field, value FROM user_overrides WHERE entity_type = ?"
    params: list[Any] = [_ITEM_ENTITY_TYPE]
    if source_id is not None:
        query += " AND source_id = ?"
        params.append(source_id)

    for row_source_id, external_id, field, value in conn.execute(query, params).fetchall():
        if field not in OVERRIDABLE_FIELDS:
            logger.warning("apply_overrides: ignoring unknown field %r", field)
            continue
        conn.execute(
            f"UPDATE items SET {field} = ? WHERE source_id = ? AND external_id = ? AND origin = 'derived'",
            (value, row_source_id, external_id),
        )


def reproject(source_name: str, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """In one transaction: delete every derived item for source_name,
    replay its snapshots oldest to newest through the projector, then
    apply_overrides().
    """
    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        try:
            row = conn.execute("SELECT id FROM sources WHERE name = ?", (source_name,)).fetchone()
            if row is None:
                raise ValueError(f"unknown source: {source_name!r}")
            source_id = row[0]

            conn.execute(
                "DELETE FROM items WHERE source_id = ? AND origin = 'derived'", (source_id,)
            )

            snapshots = conn.execute(
                "SELECT id, source_id, fetched_at, last_seen_at, payload FROM snapshots "
                "WHERE source_id = ? ORDER BY fetched_at ASC, id ASC",
                (source_id,),
            ).fetchall()

            for snap_id, snap_source_id, fetched_at, last_seen_at, payload_json in snapshots:
                snapshot = Snapshot(
                    id=snap_id,
                    source_id=snap_source_id,
                    fetched_at=datetime.fromisoformat(fetched_at),
                    last_seen_at=datetime.fromisoformat(last_seen_at),
                    payload=json.loads(payload_json),
                )
                project_snapshot(conn, source_id, source_name, snapshot)

            apply_overrides(conn, source_id=source_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
