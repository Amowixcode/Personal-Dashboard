from app.project.base import DerivedItem


def test_derived_item_defaults():
    item = DerivedItem(external_id="1", kind="task", title="Do it", section="ops")
    assert item.detail is None
    assert item.due_at is None
    assert item.actionable is False
