"""Pure tests for the standup nudge task (issue #26).

No network, no DB: ``create_task_session`` is patched to a fake session whose
queries return canned results in order, ``members_to_prompt`` is stubbed at
class level, and both delivery paths (Telegram client / Webex helper) are
recorded, never sent.
"""

import uuid

from app.config import settings
from app.models.standup import StandupRound
from app.models.team import TeamMember
from app.services.standup_service import StandupService
from app.tasks import standup_tasks


class FakeResult:
    def __init__(self, scalar=None, scalars=()):
        self._scalar = scalar
        self._scalars = list(scalars)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self._scalars


class FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt):
        assert self._results, f"unexpected extra query: {stmt}"
        return self._results.pop(0)


def _fake_ctx(results):
    session = FakeSession(results)

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return _Ctx()


def _async(value):
    async def _wrapper():
        return value

    return _wrapper()


def _member(name, chat_id=None):
    return TeamMember(id=str(uuid.uuid4()), name=name,
                      email=f"{name.split()[0].lower()}@northwind.dev",
                      telegram_chat_id=chat_id)


def _round():
    return StandupRound(id=str(uuid.uuid4()), status="collecting")


async def _run_nudge(monkeypatch, *, round_, members, answered_ids):
    monkeypatch.setattr(standup_tasks, "create_task_session", lambda: _fake_ctx(
        [FakeResult(scalar=round_), FakeResult(scalars=answered_ids)]
    ))
    if round_ is not None:
        monkeypatch.setattr(
            StandupService, "members_to_prompt", lambda self: _async(members)
        )
    tg_sent, wx_sent = [], []

    class FakeTelegram:
        def __init__(self, token):
            pass

        async def send_message(self, chat_id, text):
            tg_sent.append((chat_id, text))

    async def fake_webex(member, text):
        wx_sent.append((member.email, text))
        return {"sent": True}

    monkeypatch.setattr(standup_tasks, "TelegramClient", FakeTelegram)
    monkeypatch.setattr(standup_tasks, "_deliver_to_member_webex", fake_webex)
    return await standup_tasks._nudge(), tg_sent, wx_sent


async def test_nudge_dmss_only_members_without_entry(monkeypatch):
    round_ = _round()
    answered, pending = _member("Sarah Chen"), _member("Tom Okafor")
    result, tg_sent, wx_sent = await _run_nudge(
        monkeypatch, round_=round_, members=[answered, pending],
        answered_ids=[answered.id],
    )
    assert result == {"round_id": round_.id, "nudged": 1, "skipped": []}
    assert len(wx_sent) == 1 and wx_sent[0][0] == pending.email  # webex fallback
    assert not tg_sent


async def test_nudge_skips_when_all_answered(monkeypatch):
    member = _member("Sarah Chen")
    round_ = _round()
    result, tg_sent, wx_sent = await _run_nudge(
        monkeypatch, round_=round_, members=[member], answered_ids=[member.id],
    )
    assert result == {"skipped": "all_answered"}
    assert not tg_sent and not wx_sent


async def test_nudge_skips_without_collecting_round(monkeypatch):
    # No round result queued past the first query: if members_to_prompt were
    # called the fake session would assert on the extra query.
    result, tg_sent, wx_sent = await _run_nudge(
        monkeypatch, round_=None, members=[], answered_ids=[],
    )
    assert result == {"skipped": "no_open_round"}
    assert not tg_sent and not wx_sent


async def test_nudge_prefers_telegram_for_linked_chats(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    monkeypatch.setattr(settings, "app_public_url", "http://lan:3002/")
    linked, webex_only = _member("Sarah Chen", chat_id="42"), _member("Tom Okafor")
    round_ = _round()
    result, tg_sent, wx_sent = await _run_nudge(
        monkeypatch, round_=round_, members=[linked, webex_only],
        answered_ids=[],
    )
    assert result["nudged"] == 2
    assert [c for c, _ in tg_sent] == ["42"]
    assert "closes in ~15 minutes" in tg_sent[0][1]
    assert f"/?standup={round_.id}&member={linked.id}" in tg_sent[0][1]
    assert len(wx_sent) == 1  # unlinked member falls back to webex


async def test_send_question_still_routes_through_webex_helper(monkeypatch):
    member = _member("Sarah Chen")
    sent = []

    async def fake_webex(m, text):
        sent.append((m.email, text))
        return {"sent": True}

    monkeypatch.setattr(standup_tasks, "create_task_session", lambda: _fake_ctx(
        [FakeResult(scalar=member)]
    ))
    monkeypatch.setattr(standup_tasks, "_deliver_to_member_webex", fake_webex)
    result = await standup_tasks._send_question("r1", member.id)
    assert result == {"round_id": "r1", "member_id": member.id, "sent": True}
    assert sent and sent[0][0] == member.email
