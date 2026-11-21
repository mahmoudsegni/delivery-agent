"""Pure tests for the team calendar ICS feed (issue #41).

Same philosophy as test_absences / test_passcode_gate: no network, no DB.
The builder is tested against fake objects; the middleware exemption is
driven through TestClient with the DB dependency overridden by fakes.
"""

from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.services.team_service import build_ics


class FakeSprint:
    def __init__(self, id, name, status, start_date, end_date):
        self.id, self.name, self.status = id, name, status
        self.start_date, self.end_date = start_date, end_date


class FakeAbsenceRow:
    """Row-shaped like the endpoint's absence query (joined member_name)."""

    def __init__(self, id, member_name, note, starts_on, ends_on):
        self.id, self.member_name, self.note = id, member_name, note
        self.starts_on, self.ends_on = starts_on, ends_on


SPRINT = FakeSprint("s1", "2412", "active", date(2026, 12, 7), date(2026, 12, 18))
ABSENCE = FakeAbsenceRow("a1", "Sarah Chen", "PTO", date(2026, 12, 24), date(2026, 12, 25))


# --- builder ------------------------------------------------------------------


def test_dtend_is_exclusive_end_plus_one_day():
    ics = build_ics([SPRINT], [ABSENCE])
    assert "DTSTART;VALUE=DATE:20261207\r\nDTEND;VALUE=DATE:20261219" in ics
    assert "DTSTART;VALUE=DATE:20261224\r\nDTEND;VALUE=DATE:20261226" in ics


def test_uids_are_stable_and_typed():
    ics = build_ics([SPRINT], [ABSENCE])
    assert "UID:sprint-s1@delivery-agent" in ics
    assert "UID:absence-a1@delivery-agent" in ics


def test_text_values_escaped():
    odd = FakeAbsenceRow("a2", "Chen; Sarah", "a,b\\c", date(2026, 12, 1), date(2026, 12, 1))
    ics = build_ics([], [odd])
    assert "SUMMARY:Chen\\; Sarah — time off" in ics
    assert "DESCRIPTION:a\\,b\\\\c" in ics


def test_note_newlines_become_literal_n():
    nl = FakeAbsenceRow("a3", "Sarah Chen", "line1\nline2", date(2026, 12, 1), date(2026, 12, 1))
    assert "DESCRIPTION:line1\\nline2" in build_ics([], [nl])


def test_crlf_endings_and_no_bare_lf():
    ics = build_ics([SPRINT], [ABSENCE])
    assert "\n" not in ics.replace("\r\n", "")  # every LF is part of CRLF
    assert ics.endswith("END:VCALENDAR\r\n")


def test_long_lines_fold_with_leading_space_continuation():
    long_name = ("Sarah " * 20).strip()  # SUMMARY far past 74 chars
    s = FakeSprint("s2", long_name, "planning", date(2026, 12, 7), date(2026, 12, 18))
    ics = build_ics([s], [])
    for line in ics.split("\r\n"):
        assert len(line) <= 75
    # Unfolding (drop the leading space of continuations) rebuilds the summary.
    unfolded = ics.replace("\r\n ", "")
    assert f"SUMMARY:Sprint {long_name} (planning)" in unfolded


def test_description_omitted_when_note_unset():
    assert "DESCRIPTION" not in build_ics([SPRINT], [])


# --- middleware token exemption -----------------------------------------------


class FakeExec:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeDB:
    """Answers the endpoint's two queries in call order: sprints, absences."""

    def __init__(self, sprints, absences):
        self._queue = [FakeExec(sprints), FakeExec(absences)]

    async def execute(self, stmt):
        return self._queue.pop(0)


def _override_db():
    app.dependency_overrides[get_db] = lambda: FakeDB([SPRINT], [ABSENCE])


def _feed(client, token=None):
    url = "/api/v1/team/calendar.ics"
    return client.get(url + f"?token={token}" if token else url)


def _client():
    return TestClient(app)


def test_feed_gated_without_token():
    settings.passcode, settings.ics_token = "sesame", "cal-tok"
    _override_db()
    try:
        assert _feed(_client()).status_code == 401
        assert _feed(_client(), "wrong").status_code == 401
    finally:
        settings.passcode, settings.ics_token = "", ""
        app.dependency_overrides.clear()


def test_feed_passes_with_correct_token():
    settings.passcode, settings.ics_token = "sesame", "cal-tok"
    _override_db()
    try:
        res = _feed(_client(), "cal-tok")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/calendar")
        assert "BEGIN:VCALENDAR" in res.text
        assert 'filename="team.ics"' in res.headers.get("content-disposition", "")
    finally:
        settings.passcode, settings.ics_token = "", ""
        app.dependency_overrides.clear()


def test_no_token_configured_means_no_exemption():
    settings.passcode, settings.ics_token = "sesame", ""
    _override_db()
    try:
        assert _feed(_client(), "").status_code == 401
    finally:
        settings.passcode, settings.ics_token = "", ""
        app.dependency_overrides.clear()
