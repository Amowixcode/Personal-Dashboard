"""Dummy collector: proves the collector pattern end to end.

Returns {"counter": N, "timestamp": "..."}. It calls no network and needs no
configuration, so it can run immediately and gives the debug page something
to show.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from app.collect.base import Retention


class DummyCollector:
    """`counter` increments once per real `fetch()` call by default, so
    consecutive production runs always produce a different payload -- that
    proves the pattern, it doesn't exercise deduplication.

    Tests that need to force two calls to return the *same* payload (to
    exercise the runner's dedup path), or a specific sequence of payloads,
    pass `payloads`: an iterator of raw payload values. When given, `fetch()`
    returns `next(payloads)` and ignores its own counter.
    """

    name = "dummy"
    retention = Retention.LATEST_ONLY
    retention_days: int | None = None

    def __init__(
        self,
        interval_s: int = 5,
        timeout_s: float = 5.0,
        payloads: Iterator[Any] | None = None,
    ) -> None:
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self._payloads = payloads
        self._counter = 0

    async def fetch(self) -> Any:
        if self._payloads is not None:
            return next(self._payloads)
        self._counter += 1
        return {
            "counter": self._counter,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


collector = DummyCollector()
