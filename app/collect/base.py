"""Collector protocol and retention policy.

Spec: CLAUDE.md, "Collector protocol". A collector fetches raw data. It
interprets nothing and does not touch the database.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol


class Retention(StrEnum):
    FOREVER = "forever"           # irreplaceable: bank CSV, workout export
    LATEST_ONLY = "latest_only"   # source is its own archive: calendar, GitHub
    DAYS = "days"                 # ephemeral: Entur departures
    DOWNSAMPLE = "downsample"     # measurements: response time, quotas


class Collector(Protocol):
    name: str
    interval_s: int
    retention: Retention
    retention_days: int | None
    timeout_s: float

    async def fetch(self) -> Any:
        """Returns a JSON-serializable raw payload. Raises on failure."""
        ...
