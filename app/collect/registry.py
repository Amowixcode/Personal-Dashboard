"""Discovers collectors in app/collect/sources/ and upserts one `sources`
row per collector.

Convention: each module in app/collect/sources/ exports a module-level
`collector` object -- an instance implementing the Collector protocol (see
dummy.py). Discovery imports every non-private module in the package and
collects each module's `collector` attribute. This is deliberately the
simplest possible convention: no Protocol runtime-checks, no class
scanning, no plugin decorators. A future source just adds
`collector = MyCollector(...)` at module scope.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Sequence
from pathlib import Path

from app.collect import sources
from app.collect.base import Collector
from app.db.connection import DEFAULT_DB_PATH, write_connection


def discover_collectors() -> list[Collector]:
    collectors: list[Collector] = []
    for module_info in pkgutil.iter_modules(sources.__path__, prefix=f"{sources.__name__}."):
        leaf_name = module_info.name.rsplit(".", 1)[-1]
        if leaf_name.startswith("_"):
            continue
        module = importlib.import_module(module_info.name)
        found = getattr(module, "collector", None)
        if found is not None:
            collectors.append(found)
    return collectors


def register_collectors(
    db_path: Path | str = DEFAULT_DB_PATH,
    collectors: Sequence[Collector] | None = None,
) -> list[Collector]:
    """Upsert one `sources` row per collector. Returns the collectors used.

    `enabled` is set only on first insert (schema default 1) and is never
    touched on conflict, so a source the user disabled through the interface
    stays disabled across restarts.
    """
    if collectors is None:
        collectors = discover_collectors()
    collectors = list(collectors)

    with write_connection(db_path) as conn:
        conn.execute("BEGIN")
        try:
            for c in collectors:
                conn.execute(
                    """
                    INSERT INTO sources (name, interval_s, retention, retention_days)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        interval_s = excluded.interval_s,
                        retention = excluded.retention,
                        retention_days = excluded.retention_days
                    """,
                    (c.name, c.interval_s, str(c.retention), c.retention_days),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return collectors
