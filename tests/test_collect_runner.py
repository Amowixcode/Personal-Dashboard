import asyncio
import time

import httpx

from app.collect import runner
from app.collect.base import Retention
from app.collect.registry import register_collectors
from app.collect.sources.dummy import DummyCollector
from app.db.connection import write_connection
from app.db.migrate import run_migrations


class AlwaysFailsCollector:
    name = "always_fails"
    interval_s = 60
    retention = Retention.LATEST_ONLY
    retention_days = None
    timeout_s = 1.0

    async def fetch(self):
        raise RuntimeError("nope")


class HangingCollector:
    name = "hanging"
    interval_s = 60
    retention = Retention.LATEST_ONLY
    retention_days = None
    timeout_s = 0.1

    def __init__(self):
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        await asyncio.sleep(2.0)
        return {"never": "reached"}


class FlakyCollector:
    name = "flaky"
    interval_s = 60
    retention = Retention.LATEST_ONLY
    retention_days = None
    timeout_s = 1.0

    def __init__(self, exceptions):
        self._to_raise = list(exceptions)
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        if self._to_raise:
            raise self._to_raise.pop(0)
        return {"ok": True}


def _run(coro):
    return asyncio.run(coro)


def _runs_for(db_path, name):
    with write_connection(db_path) as conn:
        return conn.execute(
            "SELECT ok, error FROM runs WHERE source_id = (SELECT id FROM sources WHERE name = ?) "
            "ORDER BY id",
            (name,),
        ).fetchall()


def test_snapshots_grow_only_when_payload_changes(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    payloads = iter(
        [
            {"counter": 1, "timestamp": "t1"},
            {"counter": 1, "timestamp": "t1"},  # identical -> dedup
            {"counter": 2, "timestamp": "t2"},  # different -> new row
        ]
    )
    collector = DummyCollector(payloads=payloads)
    register_collectors(db_path, collectors=[collector])

    for _ in range(3):
        _run(runner.run_once(collector, db_path))

    with write_connection(db_path) as conn:
        snapshots = conn.execute(
            "SELECT payload_hash FROM snapshots ORDER BY id"
        ).fetchall()
        run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    assert len(snapshots) == 2
    assert snapshots[0][0] != snapshots[1][0]
    assert run_count == 3  # every call is still recorded as a run


def test_failing_collector_does_not_block_dummy(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    failing = AlwaysFailsCollector()
    dummy = DummyCollector()
    register_collectors(db_path, collectors=[failing, dummy])

    _run(runner.run_once(failing, db_path))
    _run(runner.run_once(dummy, db_path))

    failing_runs = _runs_for(db_path, "always_fails")
    dummy_runs = _runs_for(db_path, "dummy")
    with write_connection(db_path) as conn:
        dummy_snapshots = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE source_id = (SELECT id FROM sources WHERE name = 'dummy')"
        ).fetchone()[0]

    assert failing_runs == [(0, "RuntimeError: nope")]
    assert dummy_runs == [(1, None)]
    assert dummy_snapshots == 1


def test_hanging_collector_is_cut_off_at_timeout(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = HangingCollector()
    register_collectors(db_path, collectors=[collector])

    start = time.monotonic()
    _run(runner.run_once(collector, db_path))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert collector.calls == 1  # not retried

    (row,) = _runs_for(db_path, "hanging")
    assert row[0] == 0
    assert "TimeoutError" in row[1]


def test_retries_on_network_error_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_RETRY_BASE_DELAY_S", 0.001)
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = FlakyCollector([httpx.ConnectError("boom"), httpx.ConnectError("boom again")])
    register_collectors(db_path, collectors=[collector])

    _run(runner.run_once(collector, db_path))

    assert collector.calls == 3
    (row,) = _runs_for(db_path, "flaky")
    assert row[0] == 1


def test_retries_exhausted_on_persistent_5xx_records_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_RETRY_BASE_DELAY_S", 0.001)
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    request = httpx.Request("GET", "http://example.invalid")
    response = httpx.Response(503, request=request)
    error = httpx.HTTPStatusError("server error", request=request, response=response)
    collector = FlakyCollector([error, error, error])
    register_collectors(db_path, collectors=[collector])

    _run(runner.run_once(collector, db_path))

    assert collector.calls == 3
    (row,) = _runs_for(db_path, "flaky")
    assert row[0] == 0
    assert "HTTPStatusError" in row[1]


def test_non_retryable_error_fails_on_first_attempt(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = FlakyCollector([ValueError("bad data")])
    register_collectors(db_path, collectors=[collector])

    _run(runner.run_once(collector, db_path))

    assert collector.calls == 1
    (row,) = _runs_for(db_path, "flaky")
    assert row[0] == 0


def test_concurrent_run_once_calls_are_serialized_by_the_guard(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)

    class SlowCollector:
        name = "slow"
        interval_s = 60
        retention = Retention.LATEST_ONLY
        retention_days = None
        timeout_s = 5.0

        def __init__(self):
            self.calls = 0

        async def fetch(self):
            self.calls += 1
            await asyncio.sleep(0.05)
            return {"n": self.calls}

    collector = SlowCollector()
    register_collectors(db_path, collectors=[collector])

    async def body():
        return await asyncio.gather(
            runner.run_once(collector, db_path),
            runner.run_once(collector, db_path),
        )

    results = asyncio.run(body())

    assert sorted(results) == [False, True]
    assert collector.calls == 1
    assert len(_runs_for(db_path, "slow")) == 1
