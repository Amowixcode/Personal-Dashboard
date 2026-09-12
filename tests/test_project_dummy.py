import socket
import sqlite3
from datetime import datetime, timezone

from app.project.base import Snapshot
from app.project.projectors.dummy import DummyProjector


def _snapshot(counter, timestamp="t1"):
    return Snapshot(
        id=1,
        source_id=1,
        fetched_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
        payload={"counter": counter, "timestamp": timestamp},
    )


def test_dummy_projector_produces_one_item_keyed_on_counter():
    items = DummyProjector().project(_snapshot(1))
    assert len(items) == 1
    assert items[0].external_id == "1"
    assert items[0].kind == "task"
    assert items[0].section == "ops"


def test_projector_performs_no_io(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("projector performed I/O")

    monkeypatch.setattr(sqlite3, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)

    items = DummyProjector().project(_snapshot(1))

    assert len(items) == 1
