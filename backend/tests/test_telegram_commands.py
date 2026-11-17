"""Pure tests for the Telegram bot command routing (issue #18).

No network, no DB: ``create_task_session`` is patched to an opaque session,
and the two DB touches (``member_for_chat`` / ``BriefService.latest``) are
stubbed at class level. Rendering runs for real on an in-memory Brief.
"""

from datetime import date

from app.integrations import telegram_bot
from app.integrations.telegram_bot import handle_update
from app.models.brief import Brief
from app.models.confidence import ConfidenceSnapshot
from app.models.sprint import Sprint
from app.services.brief_service import BriefService
from app.services.standup_service import AnswerResult, StandupService
from app.models.team import TeamMember


class FakeClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))
        return {}


class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _member() -> TeamMember:
    return TeamMember(name="Sarah Chen", email="sarah@northwind.dev")


def _brief() -> Brief:
    return Brief(
        brief_date=date(2026, 11, 16),
        headline="Sprint 2404 on track",
        content={"shipped": ["Payment retry job"], "numbers": {"confidence": "82%"}},
    )


async def _run(monkeypatch, text, *, member=True, brief=True):
    monkeypatch.setattr(telegram_bot, "create_task_session", _FakeSessionCtx)
    monkeypatch.setattr(
        StandupService, "member_for_chat", lambda self, chat_id: _async(_member() if member else None)
    )
    monkeypatch.setattr(
        BriefService, "latest", lambda self: _async(_brief() if brief else None)
    )
    recorded = []

    async def _record(self, chat_id, text):
        recorded.append(text)
        return AnswerResult(reply=None)

    monkeypatch.setattr(StandupService, "record_answer", _record)

    client = FakeClient()
    await handle_update(
        {"message": {"chat": {"id": 42}, "text": text, "from": {"username": "sarah"}}},
        client,
    )
    return client, recorded


def _async(value):
    async def _wrapper():
        return value

    return _wrapper()


def _snap(value: float, direction: str = "steady") -> ConfidenceSnapshot:
    return ConfidenceSnapshot(
        sprint_id="s1",
        snapshot_date=date(2026, 11, 16),
        confidence_value=value,
        factors={"direction": direction},
    )


async def _run_confidence(monkeypatch, text, *, member=True, fetch=lambda session: (72.0, [])):
    async def _fetch(session):
        return fetch(session)

    monkeypatch.setattr(telegram_bot, "_fetch_confidence", _fetch)
    return await _run(monkeypatch, text, member=member)


async def test_confidence_with_data(monkeypatch):
    sprint_a = Sprint(name="Sprint 2404", start_date=date(2026, 11, 2), end_date=date(2026, 11, 13))
    sprint_b = Sprint(name="Sprint 2405", start_date=date(2026, 11, 9), end_date=date(2026, 11, 20))
    client, _ = await _run_confidence(
        monkeypatch,
        "/confidence",
        fetch=lambda session: (
            72.0,
            [(sprint_a, _snap(78.0, "improving")), (sprint_b, _snap(55.0, "declining"))],
        ),
    )
    (chat_id, reply), = client.sent
    assert "72" in reply
    assert "Sprint 2404" in reply and "Sprint 2405" in reply
    assert "On track" in reply and "↑" in reply and "↓" in reply
    assert len(reply) < 4096


async def test_confidence_no_data(monkeypatch):
    client, _ = await _run_confidence(
        monkeypatch, "/confidence", fetch=lambda session: (None, [])
    )
    assert "No confidence data yet" in client.sent[0][1]


async def test_confidence_unlinked_chat(monkeypatch):
    client, _ = await _run_confidence(monkeypatch, "/confidence", member=False)
    assert "/start <code>" in client.sent[0][1]


async def test_status_with_brief(monkeypatch):
    client, recorded = await _run(monkeypatch, "/status")
    assert not recorded
    (chat_id, reply), = client.sent
    assert chat_id == "42"
    assert "Sprint 2404 on track" in reply
    assert "Payment retry job" in reply
    assert "Briefs page" in reply
    assert len(reply) < 4096  # Telegram hard limit


async def test_status_without_brief(monkeypatch):
    client, _ = await _run(monkeypatch, "/status", brief=False)
    assert "No brief yet" in client.sent[0][1]


async def test_status_unlinked_chat(monkeypatch):
    client, _ = await _run(monkeypatch, "/status", member=False)
    assert "/start <code>" in client.sent[0][1]


async def test_help_lists_commands(monkeypatch):
    client, _ = await _run(monkeypatch, "/help")
    reply = client.sent[0][1]
    assert "/start" in reply and "/status" in reply and "standup" in reply


async def test_unknown_command_not_recorded_as_answer(monkeypatch):
    client, recorded = await _run(monkeypatch, "/foo bar")
    assert not recorded
    assert "/help" in client.sent[0][1]


async def test_plain_text_still_records_answer(monkeypatch):
    client, recorded = await _run(monkeypatch, "Shipped the retry job")
    assert recorded == ["Shipped the retry job"]
    assert not client.sent  # AnswerResult(reply=None) -> nothing sent
