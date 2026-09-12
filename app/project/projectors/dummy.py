"""Projector for the dummy collector. Pure: no network, no writes."""

from __future__ import annotations

from app.project.base import DerivedItem, Snapshot


class DummyProjector:
    source_name = "dummy"

    def project(self, snapshot: Snapshot) -> list[DerivedItem]:
        counter = snapshot.payload["counter"]
        timestamp = snapshot.payload.get("timestamp")
        return [
            DerivedItem(
                external_id=str(counter),
                kind="task",
                title=f"Dummy tick #{counter}",
                section="ops",
                detail=f"fetched at {timestamp}" if timestamp else None,
                actionable=False,
            )
        ]
