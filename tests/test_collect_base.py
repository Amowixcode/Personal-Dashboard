from app.collect.base import Retention


def test_retention_values():
    assert {r.value for r in Retention} == {"forever", "latest_only", "days", "downsample"}
