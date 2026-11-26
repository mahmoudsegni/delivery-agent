"""Pure test for the /agent/grooming GET route (issue #56): no network, no DB.

The route is a deterministic dict passthrough, so the contract to pin is:
200 + grooming_data() returned verbatim. Same dependency_overrides pattern as
test_health.py; the service is monkeypatched so no SQL runs.
"""

from fastapi.testclient import TestClient

from app.api.v1.agent import AgentAnalysisService
from app.database import get_db
from app.main import app


def override(db):
    async def _override():
        yield db

    return _override


def get_grooming(monkeypatch, payload, db=object()):
    async def fake_grooming(self):
        return payload

    monkeypatch.setattr(AgentAnalysisService, "grooming_data", fake_grooming)
    app.dependency_overrides[get_db] = override(db)
    try:
        return TestClient(app).get("/api/v1/agent/grooming")
    finally:
        app.dependency_overrides.clear()


def test_grooming_route_passes_service_dict_through(monkeypatch):
    payload = {
        "summary": {"stale": 3, "unestimated": 0, "unassigned": 1},
        "items": [
            {"category": "stale", "ref": "AB-12", "title": "Old thing", "days": 41},
        ],
    }
    r = get_grooming(monkeypatch, payload)
    assert r.status_code == 200, r.text
    assert r.json() == payload
