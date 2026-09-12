from app.project.projectors.dummy import DummyProjector
from app.project.registry import get_projector


def test_get_projector_finds_dummy():
    projector = get_projector("dummy")
    assert isinstance(projector, DummyProjector)


def test_get_projector_returns_none_for_unknown_source():
    assert get_projector("no-such-source") is None
