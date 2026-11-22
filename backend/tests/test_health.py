"""Pure tests for the health endpoint (issue #39): no network, no DB.

Same philosophy as test_release_notes: dependency_overrides swaps get_db for
a fake that replays canned results in call order (SELECT 1, then the
SyncState lookup), so the 200/503 contract is what gets asserted.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.sync_state import SyncState


class FakeResult:
    """Just enough of a SQLAlchemy result: one row or None."""

    def __init__(self, row=None):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class FakeDB:
    """AsyncSession stand-in: pops one canned result per execute(), in order."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, query):
        return self._results.pop(0)


class DeadDB:
    async def execute(self, query):
        raise RuntimeError("connection refused")


def override(db):
    async def _override():
        yield db

    return _override


def sync_row(hours_ago=2.4):
    return SyncState(
        provider="mock",
        last_sync_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        last_status="ok",
        last_error="boom: token expired",
        inserted_count=3,
        updated_count=1,
    )


def get_health(db):
    app.dependency_overrides[get_db] = override(db)
    try:
        return TestClient(app).get("/api/v1/health")
    finally:
        app.dependency_overrides.clear()


def test_ok_reports_db_and_sync_age():
    r = get_health(FakeDB([FakeResult(), FakeResult(sync_row(2.4))]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["db"] == "ok"
    assert body["sync"]["provider"] == "mock"
    assert isinstance(body["sync"]["age_hours"], float)
    assert 2.3 <= body["sync"]["age_hours"] <= 2.5
    assert body["sync"]["last_status"] == "ok"
    assert body["sync"]["last_error"] == "boom: token expired"


def test_db_down_is_503_degraded():
    r = get_health(DeadDB())
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] == "unreachable"
    assert "connection refused" in body["detail"]


def test_no_sync_rows_reports_null():
    r = get_health(FakeDB([FakeResult(), FakeResult()]))
    assert r.status_code == 200
    body = r.json()
    assert body["db"] == "ok"
    assert body["sync"] is None
