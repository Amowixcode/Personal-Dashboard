"""Write endpoints for items: POST, PATCH, DELETE, and a filtered GET.

Spec: CLAUDE.md, "HTTP endpoints", "Three classes of data", "Reprocessing".

For `origin='user'` rows, PATCH and DELETE act directly on `items`. For
`origin='derived'` rows, PATCH and DELETE never touch `items` directly --
they write to `user_overrides` (keyed on source_id/external_id) and then
call `apply_overrides()`, the same mechanism `reproject()` already relies
on, so a derived edit survives the next collector run or reprojection
exactly the way a projector-untouched field does.

"Focus" (marking today's focus item) is not a content field carried by any
source, so it does not go through `user_overrides` at all -- it lives in
its own single-row `focus` table, settable regardless of an item's origin.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.db.connection import DEFAULT_DB_PATH, read_connection, write_connection
from app.project.reproject import OVERRIDABLE_FIELDS, apply_overrides

router = APIRouter()

_VALID_KINDS = {"task", "deadline", "exam", "renewal", "birthday"}
_VALID_SECTIONS = {"today", "studies", "applications", "finance", "ops", "calendar"}


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _now_iso_z() -> str:
    return _iso_z(datetime.now(timezone.utc))


_ROW_COLUMNS = (
    "id, kind, title, detail, due_at, completed_at, actionable, section, "
    "origin, source_id, external_id, dismissed_at, created_at, updated_at"
)


def _row_to_item(row: tuple, focus_item_id: int | None) -> dict[str, Any]:
    (
        item_id,
        kind,
        title,
        detail,
        due_at,
        completed_at,
        actionable,
        section,
        origin,
        source_id,
        external_id,
        dismissed_at,
        created_at,
        updated_at,
    ) = row
    return {
        "id": item_id,
        "kind": kind,
        "title": title,
        "detail": detail,
        "due_at": _iso_z(_parse(due_at)) if due_at else None,
        "completed_at": _iso_z(_parse(completed_at)) if completed_at else None,
        "actionable": bool(actionable),
        "section": section,
        "origin": origin,
        "source_id": source_id,
        "external_id": external_id,
        "created_at": _iso_z(_parse(created_at)),
        "updated_at": _iso_z(_parse(updated_at)),
        "is_focus": focus_item_id == item_id,
    }


def _get_focus_item_id(conn) -> int | None:
    row = conn.execute("SELECT item_id FROM focus WHERE id = 1").fetchone()
    return row[0] if row else None


def _fetch_item_row(conn, item_id: int) -> tuple | None:
    return conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM items WHERE id = ?", (item_id,)
    ).fetchone()


def list_items(
    db_path: Path | str | None = None,
    section: str | None = None,
    kind: str | None = None,
    due_before: str | None = None,
    include_done: bool = False,
) -> list[dict[str, Any]]:
    if db_path is None:
        db_path = DEFAULT_DB_PATH

    clauses = ["dismissed_at IS NULL"]
    params: list[Any] = []
    if not include_done:
        clauses.append("completed_at IS NULL")
    if section is not None:
        clauses.append("section = ?")
        params.append(section)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if due_before is not None:
        clauses.append("due_at IS NOT NULL AND due_at < ?")
        params.append(due_before)

    query = (
        f"SELECT {_ROW_COLUMNS} FROM items WHERE "
        + " AND ".join(clauses)
        + " ORDER BY due_at IS NULL, due_at ASC"
    )

    with read_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        focus_item_id = _get_focus_item_id(conn)

    return [_row_to_item(r, focus_item_id) for r in rows]


def create_item(body: dict[str, Any], db_path: Path | str | None = None) -> dict[str, Any]:
    if db_path is None:
        db_path = DEFAULT_DB_PATH

    kind = body.get("kind")
    title = body.get("title")
    section = body.get("section")
    if kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {kind!r}")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if section not in _VALID_SECTIONS:
        raise HTTPException(status_code=400, detail=f"invalid section: {section!r}")

    now = _now_iso_z()
    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        try:
            cursor = conn.execute(
                """
                INSERT INTO items
                    (kind, title, detail, due_at, actionable, section, origin,
                     source_id, external_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'user', NULL, NULL, ?, ?)
                """,
                (
                    kind,
                    title,
                    body.get("detail"),
                    body.get("due_at"),
                    int(bool(body.get("actionable", False))),
                    section,
                    now,
                    now,
                ),
            )
            item_id = cursor.lastrowid
            row = _fetch_item_row(conn, item_id)
            focus_item_id = _get_focus_item_id(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return _row_to_item(row, focus_item_id)


def _set_focus(conn, item_id: int, on: bool) -> None:
    now = _now_iso_z()
    if on:
        conn.execute(
            """
            INSERT INTO focus (id, item_id, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET item_id = excluded.item_id, updated_at = excluded.updated_at
            """,
            (item_id, now),
        )
    else:
        conn.execute(
            "UPDATE focus SET item_id = NULL, updated_at = ? WHERE id = 1 AND item_id = ?",
            (now, item_id),
        )


def update_item(item_id: int, body: dict[str, Any], db_path: Path | str | None = None) -> dict[str, Any]:
    if db_path is None:
        db_path = DEFAULT_DB_PATH

    content_fields = {k: v for k, v in body.items() if k in OVERRIDABLE_FIELDS}
    focus = body.get("focus")

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        try:
            row = _fetch_item_row(conn, item_id)
            if row is None:
                raise HTTPException(status_code=404, detail="item not found")
            origin = row[8]
            source_id = row[9]
            external_id = row[10]

            if content_fields:
                if origin == "user":
                    set_clause = ", ".join(f"{field} = ?" for field in content_fields)
                    conn.execute(
                        f"UPDATE items SET {set_clause}, updated_at = ? WHERE id = ?",
                        (*content_fields.values(), _now_iso_z(), item_id),
                    )
                else:
                    now = _now_iso_z()
                    for field, value in content_fields.items():
                        conn.execute(
                            """
                            INSERT INTO user_overrides
                                (entity_type, source_id, external_id, field, value, created_at)
                            VALUES ('item', ?, ?, ?, ?, ?)
                            ON CONFLICT(entity_type, source_id, external_id, field)
                            DO UPDATE SET value = excluded.value
                            """,
                            (source_id, external_id, field, value, now),
                        )
                    apply_overrides(conn, source_id=source_id)

            if focus is not None:
                _set_focus(conn, item_id, bool(focus))

            row = _fetch_item_row(conn, item_id)
            focus_item_id = _get_focus_item_id(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return _row_to_item(row, focus_item_id)


def delete_item(item_id: int, db_path: Path | str | None = None) -> dict[str, Any]:
    if db_path is None:
        db_path = DEFAULT_DB_PATH

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        try:
            row = _fetch_item_row(conn, item_id)
            if row is None:
                raise HTTPException(status_code=404, detail="item not found")
            origin = row[8]
            source_id = row[9]
            external_id = row[10]

            if origin == "user":
                conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
                conn.commit()
                return {"id": item_id, "deleted": True}

            now = _now_iso_z()
            conn.execute(
                """
                INSERT INTO user_overrides
                    (entity_type, source_id, external_id, field, value, created_at)
                VALUES ('item', ?, ?, 'dismissed_at', ?, ?)
                ON CONFLICT(entity_type, source_id, external_id, field)
                DO UPDATE SET value = excluded.value
                """,
                (source_id, external_id, now, now),
            )
            apply_overrides(conn, source_id=source_id)
            row = _fetch_item_row(conn, item_id)
            focus_item_id = _get_focus_item_id(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return _row_to_item(row, focus_item_id)


@router.get("/api/items")
def get_items(
    section: str | None = None,
    kind: str | None = None,
    due_before: str | None = None,
    include_done: bool = False,
) -> list[dict[str, Any]]:
    return list_items(
        section=section, kind=kind, due_before=due_before, include_done=include_done
    )


@router.post("/api/items", status_code=201)
def post_item(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return create_item(body)


@router.patch("/api/items/{item_id}")
def patch_item(item_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return update_item(item_id, body)


@router.delete("/api/items/{item_id}")
def delete_item_route(item_id: int) -> dict[str, Any]:
    return delete_item(item_id)
