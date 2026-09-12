"""Projector protocol and derived-item shape.

Spec: CLAUDE.md, "Projector protocol". A projector reads one snapshot and
returns derived rows. It is pure: no network, no writes, same input gives
same output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class Snapshot:
    id: int
    source_id: int
    fetched_at: datetime
    last_seen_at: datetime
    payload: Any


@dataclass(frozen=True)
class DerivedItem:
    external_id: str
    kind: str
    title: str
    section: str
    detail: str | None = None
    due_at: datetime | None = None
    actionable: bool = False


class Projector(Protocol):
    source_name: str

    def project(self, snapshot: Snapshot) -> Sequence[DerivedItem]:
        ...
