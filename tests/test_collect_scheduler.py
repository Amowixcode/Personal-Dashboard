import asyncio
from datetime import datetime, timedelta

from app.collect.base import Retention
from app.collect.registry import register_collectors
from app.collect.runner import build_scheduler
from app.db.migrate import run_migrations


class CountingCollector:
    name = "counting"
    interval_s = 1
    retention = Retention.LATEST_ONLY
    retention_days = None
    timeout_s = 5.0

    def __init__(self):
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        return {"n": self.calls}


def test_job_is_configured_per_spec(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = CountingCollector()
    register_collectors(db_path, collectors=[collector])

    scheduler = build_scheduler([collector], db_path)
    job = scheduler.get_job("collector:counting")

    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time is None
    assert job.trigger.jitter == 30
    assert job.trigger.interval.total_seconds() == 1


def test_several_missed_fires_run_the_job_once(tmp_path):
    """Simulates resume-from-sleep: the job's next_run_time is pushed 5.5
    intervals into the past, as if a laptop slept through five 1-second
    fires. Asserts coalesce=True runs the job once, not once per missed
    interval.
    """
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    collector = CountingCollector()
    register_collectors(db_path, collectors=[collector])

    async def body():
        scheduler = build_scheduler([collector], db_path)
        scheduler.start()
        try:
            job = scheduler.get_job("collector:counting")
            overdue = datetime.now(job.trigger.timezone) - timedelta(seconds=5.5)
            scheduler.modify_job(job.id, next_run_time=overdue)

            await asyncio.sleep(1.5)
        finally:
            scheduler.shutdown(wait=False)

    asyncio.run(body())

    assert collector.calls == 1
