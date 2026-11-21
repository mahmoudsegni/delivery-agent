"""Pure tests for sprint CSV export (issue #40).

Same philosophy as test_release_notes: no network, no DB. FakeDB replays
canned query results in call order; TestClient drives the real router so the
200/404 contract and the exact CSV bytes of GET /sprints/{id}/export.csv are
what is asserted.
"""

from datetime import date
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.sprint import Sprint
from app.models.task import Task
from app.models.team import TeamMember

SPRINT_ID = "11111111-1111-1111-1111-111111111111"


def make_sprint():
    return Sprint(
        id=SPRINT_ID,
        name="Sprint 2404",
        start_date=date(2026, 11, 16),
        end_date=date(2026, 11, 29),
        status="active",
    )


def sprint_tasks():
    """Two rows: assigned + unassigned; title #1 forces comma+quote quoting."""
    return [
        Task(
            id=str(uuid4()),
            external_id="4120",
            title='Fix "group by" crash, loyalty',
            work_item_type="Bug",
            status="done",
            assignee=TeamMember(name="Sarah Chen"),
            story_points=3,
            estimated_hours=6,
            priority="high",
            technology=".NET",
        ),
        Task(
            id=str(uuid4()),
            external_id="4121",
            title="Storefront banner",
            work_item_type="Task",
            status="todo",
            assignee=None,
            story_points=2,
            estimated_hours=4,
            priority="medium",
            technology=None,
        ),
    ]


class FakeResult:
    def __init__(self, rows):
        self._rows = rows if isinstance(rows, list) else ([] if rows is None else [rows])

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeDB:
    """AsyncSession stand-in: pops one canned result per execute(), in order."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, query):
        return self._results.pop(0)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def close(self):
        pass


def override(db):
    async def _override():
        yield db

    return _override


def client(db):
    app.dependency_overrides[get_db] = override(db)
    return TestClient(app)


EXPECTED_CSV = (
    "external_id,title,work_item_type,status,assignee,"
    "story_points,estimated_hours,priority,technology\r\n"
    '4120,"Fix ""group by"" crash, loyalty",Bug,done,Sarah Chen,3,6,high,.NET\r\n'
    "4121,Storefront banner,Task,todo,,2,4,medium,\r\n"
)


def test_export_returns_rows_with_exact_csv_quoting():
    db = FakeDB([FakeResult(make_sprint()), FakeResult(sprint_tasks())])
    try:
        r = client(db).get(f"/api/v1/sprints/{SPRINT_ID}/export.csv")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        assert (
            r.headers["content-disposition"]
            == f'attachment; filename="sprint-{SPRINT_ID}.csv"'
        )
        assert r.text == EXPECTED_CSV
    finally:
        app.dependency_overrides.clear()


def test_export_empty_sprint_is_header_only():
    db = FakeDB([FakeResult(make_sprint()), FakeResult([])])
    try:
        r = client(db).get(f"/api/v1/sprints/{SPRINT_ID}/export.csv")
        assert r.status_code == 200
        assert r.text == EXPECTED_CSV.split("\r\n")[0] + "\r\n"
    finally:
        app.dependency_overrides.clear()


def test_unknown_sprint_is_404():
    db = FakeDB([FakeResult(None)])  # get_sprint existence check
    try:
        r = client(db).get(f"/api/v1/sprints/{uuid4()}/export.csv")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()
