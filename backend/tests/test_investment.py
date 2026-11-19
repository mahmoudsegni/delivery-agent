"""Pure tests for the work-type investment split — no DB, no network
(test_github_provider philosophy): fabricated grouped rows go straight into
the response shaper; ``investment_profile`` is thin glue over it. The trend
tests do the same for ``_trend_window`` / ``_trend_response``, and drive the
real router only for the ``sprints`` param bounds, which FastAPI rejects
before any DB dependency runs (test_passcode_gate trick)."""

from datetime import date

from fastapi.testclient import TestClient

from app.main import app
from app.models.sprint import Sprint
from app.services.dashboard_service import (
    _investment_response,
    _trend_response,
    _trend_window,
)

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


# ---- trend (issue #34) ----

# SQL hands closed sprints back newest-first, like the query orders them.
S1 = Sprint(id="s1", name="Sprint 40", status="completed", end_date=date(2026, 10, 2))
S2 = Sprint(id="s2", name="Sprint 41", status="completed", end_date=date(2026, 10, 16))
S3 = Sprint(id="s3", name="Sprint 42", status="completed", end_date=date(2026, 10, 30))
ACTIVE = Sprint(id="s4", name="Sprint 43", status="active", end_date=date(2026, 11, 13))


def test_trend_window_is_n_minus_1_closed_plus_active_oldest_first():
    # N=4 → 3 closed (oldest first) + the active sprint last.
    window = _trend_window([S3, S2, S1], ACTIVE, 4)
    assert [s.id for s in window] == ["s1", "s2", "s3", "s4"]


def test_trend_window_dedupes_active_sprint_past_its_end_date():
    # An "active" sprint that also shows among closed appears once, last.
    window = _trend_window([ACTIVE, S3, S2], ACTIVE, 4)
    assert [s.id for s in window] == ["s2", "s3", "s4"]


def test_trend_window_without_active_sprint():
    # No active sprint → still N-1 closed sprints, oldest first.
    window = _trend_window([S3, S2, S1], None, 4)
    assert [s.id for s in window] == ["s1", "s2", "s3"]


def test_trend_splits_types_per_sprint_consistently():
    # Bug grows 8 → 13 → 21; Task only exists in the middle sprint.
    raw = [
        ("s2", "Bug", 8, 8, 2),
        ("s3", "Bug", 13, 6, 3),
        ("s3", "Task", 5, 5, 1),
        ("s4", "Bug", 21, 4, 4),
    ]
    out = _trend_response([S2, S3, ACTIVE], raw)
    assert [(s.sprint_id, s.end_date) for s in out.sprints] == [
        ("s2", date(2026, 10, 16)),
        ("s3", date(2026, 10, 30)),
        ("s4", date(2026, 11, 13)),
    ]
    bug = {s.sprint_id: next(r for r in s.rows if r.work_item_type == "Bug") for s in out.sprints}
    assert [bug["s2"].total_points, bug["s3"].total_points, bug["s4"].total_points] == [8, 13, 21]
    assert [r.work_item_type for r in out.sprints[1].rows if r.work_item_type == "Task"] == ["Task"]
    assert all("Task" not in [r.work_item_type for r in s.rows] for s in (out.sprints[0], out.sprints[2]))


def test_trend_empty_board_is_empty_sprints_not_an_error():
    assert _trend_response([], []).sprints == []


def test_trend_param_bounds():
    from app.config import settings

    settings.passcode = ""  # gate would 401 before FastAPI's 422 otherwise
    try:
        client = TestClient(app)
        assert client.get("/api/v1/dashboard/investment/trend?sprints=1").status_code == 422
        assert client.get("/api/v1/dashboard/investment/trend?sprints=9").status_code == 422
    finally:
        settings.passcode = ""
