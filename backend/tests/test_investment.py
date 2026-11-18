"""Pure tests for the work-type investment split — no DB, no network
(test_github_provider philosophy): fabricated grouped rows go straight into
the response shaper; ``investment_profile`` is thin glue over it."""

from app.models.sprint import Sprint
from app.services.dashboard_service import _investment_response

SPRINT = Sprint(id="s1", name="Sprint 42", status="active")


def test_rows_split_done_vs_open_and_sort_by_total():
    # 2 Bugs (one done 5, one open 3) → Bug 8/13; a Task open 8; untyped 2.
    raw = [
        ("Bug", 13, 5, 2),
        ("Task", 8, 0, 1),
        ("Unclassified", 2, 0, 1),
    ]
    out = _investment_response(SPRINT, raw)
    assert [r.work_item_type for r in out.rows] == ["Bug", "Task", "Unclassified"]
    bug = out.rows[0]
    assert (bug.total_points, bug.done_points, bug.open_points) == (13, 5, 8)
    assert bug.item_count == 2
    assert out.total_points == 23
    for r in out.rows:  # the split always sums back to the total
        assert r.done_points + r.open_points == r.total_points


def test_container_types_are_excluded_entirely():
    out = _investment_response(SPRINT, [("Epic", 999, 999, 9), ("Bug", 5, 5, 1)])
    assert [r.work_item_type for r in out.rows] == ["Bug"]
    assert out.total_points == 5


def test_null_points_count_items_but_add_zero():
    out = _investment_response(SPRINT, [("Bug", None, None, 3)])
    assert out.rows[0].total_points == 0
    assert out.rows[0].item_count == 3


def test_no_sprint_is_empty_not_an_error():
    out = _investment_response(None, [("Bug", 13, 5, 2)])
    assert out.rows == []
    assert out.total_points == 0
    assert out.sprint_id is None and out.sprint_name is None
