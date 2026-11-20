"""Pure tests for global search (issue #35). No network, no DB.

The risky parts are the ones that silently misbehave: wildcard escaping at
the trust boundary, the min-length gate, and the section caps. Those are
asserted directly against the compiled SQL and the route's declared
constraints, not against a live database.
"""

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.v1.search import LIMIT, escape_like, search
from app.main import app


class TestEscapeLike:
    def test_escapes_all_wildcards_and_the_escape_char(self):
        assert escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"

    def test_plain_query_untouched(self):
        assert escape_like("Sprint 24 chat") == "Sprint 24 chat"


class TestMinLength:
    def test_short_q_is_rejected(self):
        client = TestClient(app)
        assert client.get("/api/v1/search?q=x").status_code == 422


class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _StubDB:
    """Captures executed statements, returns canned rows."""

    def __init__(self, task_rows=(), member_rows=()):
        self.task_rows, self.member_rows = task_rows, member_rows
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        # Second statement executed is the member query.
        return _StubResult(self.task_rows if len(self.statements) == 1 else self.member_rows)


TASK = SimpleNamespace(
    id="t1",
    title="Fix login",
    status="todo",
    sprint_id="s1",
    sprint_name="Sprint 2406",
    assignee_name="Sarah Chen",
)
MEMBER = SimpleNamespace(id="m1", name="Sarah Chen", role="lead")


class TestQueries:
    def test_wildcards_in_q_reach_sql_escaped(self):
        db = _StubDB([TASK], [MEMBER])

        asyncio.run(search(q="100%_", db=db))
        compiled = str(
            db.statements[0].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "\\%" in compiled  # literal wildcards, not operators
        assert "\\_" in compiled
        assert "ESCAPE" in compiled

    def test_sections_capped_at_limit(self):
        db = _StubDB([TASK], [MEMBER])

        asyncio.run(search(q="sarah", db=db))
        for stmt in db.statements:
            compiled = str(
                stmt.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            assert f"LIMIT {LIMIT}" in compiled

    def test_no_match_yields_empty_lists_not_null(self):
        db = _StubDB([], [])

        payload = asyncio.run(search(q="zz", db=db)).model_dump()
        assert payload == {"tasks": [], "members": []}

    def test_rows_map_into_response_fields(self):
        db = _StubDB([TASK], [MEMBER])

        payload = asyncio.run(search(q="sarah", db=db)).model_dump()
        assert payload["tasks"][0]["sprint_name"] == "Sprint 2406"
        assert payload["members"][0] == {"id": "m1", "name": "Sarah Chen", "role": "lead"}


class TestRouteWiring:
    def test_router_registered_under_search(self):
        # A missing (not short) q: 422 proves the route is mounted, and it
        # never touches the DB.
        client = TestClient(app)
        assert client.get("/api/v1/search").status_code == 422
