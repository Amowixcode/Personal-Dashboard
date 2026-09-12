"""Maps source name to projector."""

from __future__ import annotations

from app.project.base import Projector
from app.project.projectors.dummy import DummyProjector

_PROJECTORS: dict[str, Projector] = {
    "dummy": DummyProjector(),
}


def get_projector(source_name: str) -> Projector | None:
    return _PROJECTORS.get(source_name)
