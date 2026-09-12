"""Database connections: WAL mode, one shared write connection behind a lock.

Reads open a fresh connection per call and may run concurrently. Writes go
through the single shared connection returned by `write_connection()`, which
serializes all writers through a module-level lock -- this is what prevents
"database is locked" errors under concurrent writers, not `busy_timeout`
alone (that pragma is defense in depth for connections outside this module,
e.g. an ad hoc `sqlite3` shell against the same file).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path("data") / "dashboard.db"

_write_lock = threading.Lock()
_write_conn: sqlite3.Connection | None = None
_write_conn_path: Path | None = None


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def _new_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _configure(conn)
    return conn


@contextmanager
def write_connection(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Yield the single shared write connection, holding the write lock.

    Only one caller, in only one thread, is inside this context at a time;
    every other caller blocks until it exits. The caller owns transaction
    boundaries (call `conn.execute("BEGIN")`, then `conn.commit()` or
    `conn.rollback()`) -- this does not commit or roll back for you.
    """
    global _write_conn, _write_conn_path
    path = Path(db_path)
    with _write_lock:
        if _write_conn is None or _write_conn_path != path:
            if _write_conn is not None:
                _write_conn.close()
            _write_conn = _new_connection(path)
            _write_conn_path = path
        yield _write_conn


@contextmanager
def read_connection(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open a fresh read connection. Safe to call concurrently from many threads."""
    conn = _new_connection(Path(db_path))
    try:
        yield conn
    finally:
        conn.close()
