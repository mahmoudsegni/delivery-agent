"""Pure tests for the Telegram bot command routing (issue #18).

No network, no DB: ``create_task_session`` is patched to an opaque session,
and the two DB touches (``member_for_chat`` / ``BriefService.latest``) are
stubbed at class level. Rendering runs for real on an in-memory Brief.
"""

from datetime import date, datetime, timezone
import json

from app.integrations import standup_state
from app.integrations import telegram_bot
from app.integrations.telegram_bot import handle_update
from app.models.brief import Brief
from app.models.confidence import ConfidenceSnapshot
from app.models.llm_call import LLMCall
from app.models.sprint import Sprint
from app.models.standup import StandupEntry, StandupRound
from app.schemas.dashboard import TeamLoadRow
from app.services.agent_analysis_service import AgentAnalysisService
from app.services.ask_service import AskService
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
    def __init__(self, session=None):
        self.session = object() if session is None else session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


class FakeRedis:
    """Just enough async Redis (str values) for the ask-history path."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: int | None = None

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl = ex

    async def delete(self, key):
        self.data.pop(key, None)

    async def aclose(self):
        pass


def _member() -> TeamMember:
    return TeamMember(name="Sarah Chen", email="sarah@northwind.dev")


def _brief() -> Brief:
    return Brief(
        brief_date=date(2026, 11, 16),
        headline="Sprint 2404 on track",
        content={"shipped": ["Payment retry job"], "numbers": {"confidence": "82%"}},
    )


async def _run(monkeypatch, text, *, member=True, brief=True, session=None):
    monkeypatch.setattr(
        telegram_bot, "create_task_session", lambda: _FakeSessionCtx(session)
    )
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


def _blockers_data(**over):
    base = {
        "sprint": "Sprint 2404",
        "asOf": "2026-11-18T09:00:00+00:00",
        "staleThresholdDays": 3,
        "summary": {"totalBlockers": 2, "critical": 1, "medium": 1, "low": 0},
        "blockers": [
            {
                "severity": "High",
                "item": "DEV-101",
                "title": "Fix checkout timeout",
                "type": "On Hold",
                "daysBlocked": 4,
                "member": "Sarah Chen",
            },
            {
                "severity": "Medium",
                "item": "DEV-102",
                "title": "Migrate icons",
                "type": "Waiting",
                "daysBlocked": 5,
                "member": "Unassigned",
            },
        ],
        "overloaded": [],
    }
    base.update(over)
    return base


async def _run_blockers(monkeypatch, *, member=True, data=None):
    """Stub the pure extraction; fail hard if /blockers ever routes to the LLM/persisting path."""
    async def _data(self):
        return _blockers_data() if data is None else data

    def _forbidden(self):
        raise AssertionError("/blockers must stay deterministic: no detect_blockers/_save")

    monkeypatch.setattr(AgentAnalysisService, "detect_blockers_data", _data)
    monkeypatch.setattr(AgentAnalysisService, "detect_blockers", _forbidden)
    monkeypatch.setattr(AgentAnalysisService, "_save", _forbidden)
    return await _run(monkeypatch, "/blockers", member=member)


async def _run_ask(
    monkeypatch, *texts, member=True, answer: str | Exception | list[str | Exception] = "Two items slipped",
    redis: FakeRedis | None = None,
):
    """Patch AskService.answer at class level; returns (replies, questions, histories).

    ``answer`` is one value reused for every call, or a list zipped with ``texts``.
    """
    redis = redis or FakeRedis()  # fresh memory per test
    monkeypatch.setattr(standup_state, "get_redis", lambda: redis)
    asked: list[str] = []
    histories: list[list | None] = []
    answers = answer if isinstance(answer, list) else [answer] * len(texts)

    async def _answer(self, question, history=None):
        asked.append(question)
        histories.append(history)
        result = answers[len(asked) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(AskService, "answer", _answer)
    sent: list[tuple[str, str]] = []
    for text in texts:
        client, _ = await _run(monkeypatch, text, member=member)
        sent.extend(client.sent)
    return sent, asked, histories


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


async def test_ask_answers_question(monkeypatch):
    sent, asked, histories = await _run_ask(monkeypatch, "/ask what slipped this sprint?")
    assert asked == ["what slipped this sprint?"]
    assert histories == [None]  # first turn has no memory
    assert sent[0][1] == "Two items slipped"


async def test_ask_followup_receives_previous_turn(monkeypatch):
    sent, asked, histories = await _run_ask(
        monkeypatch, "/ask what slipped?", "/ask what about last sprint?"
    )
    assert len(asked) == 2
    assert histories[1] == [
        {"role": "user", "content": "what slipped?"},
        {"role": "assistant", "content": "Two items slipped"},
    ]


async def test_ask_llm_failure_leaves_history_empty(monkeypatch):
    sent, asked, histories = await _run_ask(
        monkeypatch, "/ask mood?", "/ask anything else?", answer=[RuntimeError("boom"), "ok"]
    )
    assert "Couldn't answer that" in sent[0][1]
    assert sent[1][1] == "ok"
    assert histories[1] is None  # failed turn appended nothing


async def test_ask_history_capped_at_six_messages(monkeypatch):
    _, _, histories = await _run_ask(
        monkeypatch, *[f"/ask q{i}" for i in range(9)], answer="a"
    )
    assert histories[8] == [
        {"role": "user", "content": "q5"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "q6"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "q7"},
        {"role": "assistant", "content": "a"},
    ]


async def test_ask_bare_command_usage_hint(monkeypatch):
    sent, asked, _ = await _run_ask(monkeypatch, "/ask")
    assert not asked
    assert "Usage: /ask" in sent[0][1]


async def test_ask_llm_failure_graceful(monkeypatch):
    sent, asked, _ = await _run_ask(
        monkeypatch, "/ask what's the mood?", answer=RuntimeError("boom")
    )
    assert asked == ["what's the mood?"]
    assert "Couldn't answer that" in sent[0][1]


async def test_ask_persists_history_to_redis(monkeypatch):
    r = FakeRedis()
    await _run_ask(monkeypatch, "/ask what slipped?", redis=r)
    assert json.loads(r.data["telegram:ask:42"]) == [
        {"role": "user", "content": "what slipped?"},
        {"role": "assistant", "content": "Two items slipped"},
    ]
    assert r.ttl == 86400


async def test_ask_helpers_round_trip_with_cap():
    r = FakeRedis()
    turns = [{"role": "user", "content": f"q{i}"} for i in range(8)]
    await standup_state.set_ask_history(r, "42", turns)
    assert await standup_state.get_ask_history(r, "42") == turns[-6:]
    await standup_state.delete_ask_history(r, "42")
    assert await standup_state.get_ask_history(r, "42") is None


async def test_reset_clears_ask_history(monkeypatch):
    sent, asked, histories = await _run_ask(
        monkeypatch, "/ask what slipped?", "/reset", "/ask anything else?"
    )
    assert "Fresh start" in sent[1][1]
    assert histories == [None, None]  # post-reset turn starts from scratch


async def test_reset_unlinked_chat(monkeypatch):
    client, _ = await _run(monkeypatch, "/reset", member=False)
    assert "/start <code>" in client.sent[0][1]


async def test_ask_unlinked_chat(monkeypatch):
    sent, asked, _ = await _run_ask(monkeypatch, "/ask anything", member=False)
    assert not asked
    assert "/start <code>" in sent[0][1]


async def test_blockers_lists_stuck_items(monkeypatch):
    client, _ = await _run_blockers(monkeypatch)
    reply = client.sent[0][1]
    assert "🚧 2 stuck item(s) — 1 high, 1 medium, 0 low (Sprint 2404)" in reply
    assert "[High] DEV-101 — Fix checkout timeout · On Hold, 4d (Sarah Chen)" in reply
    assert "[Medium] DEV-102 — Migrate icons · Waiting, 5d (Unassigned)" in reply


async def test_blockers_caps_at_ten_lines_and_4000_chars(monkeypatch):
    def items(title_len):
        return [
            {"severity": "High", "item": f"DEV-{i}", "title": "T" * title_len,
             "type": "On Hold", "daysBlocked": 9, "member": "Sarah Chen"}
            for i in range(40)
        ]

    counts = {"totalBlockers": 40, "critical": 40, "medium": 0, "low": 0}
    # Short titles: the [:10] slice shows the first ten items only.
    client, _ = await _run_blockers(
        monkeypatch, data=_blockers_data(summary=counts, blockers=items(40))
    )
    reply = client.sent[0][1]
    assert "DEV-9" in reply and "DEV-10" not in reply
    # Long titles: the reply itself stays under Telegram's cap.
    client, _ = await _run_blockers(
        monkeypatch, data=_blockers_data(summary=counts, blockers=items(480))
    )
    assert len(client.sent[0][1]) <= 4000


async def test_blockers_clean_board(monkeypatch):
    client, _ = await _run_blockers(
        monkeypatch,
        data=_blockers_data(
            summary={"totalBlockers": 0, "critical": 0, "medium": 0, "low": 0}, blockers=[]
        ),
    )
    assert "Nothing stuck right now" in client.sent[0][1]


async def test_blockers_unlinked_chat(monkeypatch):
    client, _ = await _run_blockers(monkeypatch, member=False)
    assert "/start <code>" in client.sent[0][1]


async def test_help_lists_commands(monkeypatch):
    client, _ = await _run(monkeypatch, "/help")
    reply = client.sent[0][1]
    assert "/start" in reply and "/status" in reply and "/blockers" in reply and "/reset" in reply and "standup" in reply
    assert "/skip" in reply


class _Rows:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ScriptedSession:
    """Just enough async session for mark_skipped: ordered query results."""

    def __init__(self, results):
        self.results = list(results)
        self.added = []

    async def execute(self, query):
        return _Rows(self.results.pop(0))  # IndexError = one query too many

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def _open_round() -> StandupRound:
    return StandupRound(status="collecting", trigger="manual")


async def test_skip_creates_entry_with_reason():
    session = _ScriptedSession([_member(), _open_round(), None])
    reply = await StandupService(session).mark_skipped("42", "pto")
    assert "out for today's standup" in reply
    entry, = session.added
    assert entry.raw_text == {"skipped": True, "reason": "pto"}


async def test_skip_repeat_updates_existing_entry():
    existing = StandupEntry(round_id="r1", member_id="m1", raw_text={"messages": []})
    session = _ScriptedSession([_member(), _open_round(), existing])
    reply = await StandupService(session).mark_skipped("42", "")
    assert "out for today's standup" in reply
    assert not session.added  # repeat /skip updates the same row, no duplicate insert
    assert existing.raw_text == {"messages": [], "skipped": True, "reason": "out today"}


async def test_skip_without_open_round():
    session = _ScriptedSession([_member(), None])
    reply = await StandupService(session).mark_skipped("42", "pto")
    assert "No open standup round" in reply


async def _run_skip(monkeypatch, text, *, member=True):
    calls = []

    async def _skip(self, chat_id, reason):
        calls.append((chat_id, reason))
        return "You're marked out for today's standup — no more nudges 🏖"

    monkeypatch.setattr(StandupService, "mark_skipped", _skip)
    client, _ = await _run(monkeypatch, text, member=member)
    return client, calls


async def test_skip_relays_reason(monkeypatch):
    client, calls = await _run_skip(monkeypatch, "/skip pto")
    assert calls == [("42", "pto")]
    assert "out for today's standup" in client.sent[0][1]


async def test_skip_bare_command(monkeypatch):
    _, calls = await _run_skip(monkeypatch, "/skip")
    assert calls == [("42", "")]


async def test_skip_unlinked_chat(monkeypatch):
    client, calls = await _run_skip(monkeypatch, "/skip pto", member=False)
    assert not calls
    assert "/start <code>" in client.sent[0][1]


async def test_unknown_command_not_recorded_as_answer(monkeypatch):
    client, recorded = await _run(monkeypatch, "/foo bar")
    assert not recorded
    assert "/help" in client.sent[0][1]


# ------------------------------------------------------------------
# /team (issue #33)
# ------------------------------------------------------------------


def _load_row(name, capacity, assigned, points=0) -> TeamLoadRow:
    return TeamLoadRow(
        id=name,
        name=name,
        role="Developer",
        capacity_hours=capacity,
        assigned_hours=assigned,
        open_points=points,
        in_progress=0,
        done=0,
        stale=0,
    )


_DEFAULT_TEAM = [
    _load_row("Sarah Chen", 40, 50, points=8),  # over
    _load_row("Alex Kim", 40, 34, points=5),  # 85%
    _load_row("Idle Person", 0, 0),  # skipped
    _load_row("Marta Diaz", 30, 21),  # 70%
]


async def _run_team(monkeypatch, *, member=True, rows=_DEFAULT_TEAM):
    async def _fetch(session):
        return rows

    monkeypatch.setattr(telegram_bot, "_fetch_team_load", _fetch)
    return await _run(monkeypatch, "/team", member=member)


async def test_team_renders_and_sorts(monkeypatch):
    client, recorded = await _run_team(monkeypatch)
    assert not recorded
    reply = client.sent[0][1]
    assert "👥 Team load — 1 over capacity" in reply
    assert "⚠️ Sarah Chen — 50/40h (125%) · 8 pts open" in reply
    assert "Alex Kim — 34/40h (85%) · 5 pts open" in reply
    assert "Idle Person" not in reply
    assert reply.index("Sarah Chen") < reply.index("Alex Kim") < reply.index("Marta Diaz")
    assert len(reply) < 4096


async def test_team_zero_capacity_counts_as_over(monkeypatch):
    client, _ = await _run_team(
        monkeypatch, rows=[_load_row("Sarah Chen", 0, 5, points=2)]
    )
    reply = client.sent[0][1]
    assert "1 over capacity" in reply
    assert "⚠️ Sarah Chen — 5/0h (n/a) · 2 pts open" in reply


async def test_team_row_cap(monkeypatch):
    rows = [_load_row(f"Member {i:02d}", 40, 40) for i in range(25)]
    client, _ = await _run_team(monkeypatch, rows=rows)
    reply = client.sent[0][1]
    assert "+5 more" in reply
    assert "Member 19" in reply
    assert "Member 20" not in reply  # 20 rows kept, numbered 00..19


async def test_team_char_cap(monkeypatch):
    rows = [_load_row("N" * 300, 40, 40) for _ in range(20)]
    client, _ = await _run_team(monkeypatch, rows=rows)
    assert len(client.sent[0][1]) <= 4000  # 20 rows x ~330 chars, hard-sliced


async def test_team_no_active_sprint(monkeypatch):
    client, _ = await _run_team(monkeypatch, rows=None)
    assert client.sent[0][1] == (
        "No active sprint — /team shows load for the current sprint."
    )


async def test_team_unlinked_chat(monkeypatch):
    client, _ = await _run_team(monkeypatch, member=False)
    assert "/start <code>" in client.sent[0][1]


async def test_help_lists_team(monkeypatch):
    client, _ = await _run(monkeypatch, "/help")
    assert "/team" in client.sent[0][1]


# ------------------------------------------------------------------
# /usage (issue #45)
# ------------------------------------------------------------------


class _ScalarRows:
    """execute() result stub: ``.scalars().all()`` like SQLAlchemy."""

    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _UsageSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, query):
        return _ScalarRows(self.rows)


def _call(feature: str, total_tokens: int, latency_ms: int) -> LLMCall:
    return LLMCall(
        feature=feature,
        model="llama-3",
        prompt_tokens=total_tokens // 2,
        completion_tokens=total_tokens - total_tokens // 2,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        created_at=datetime.now(timezone.utc),
    )


async def _run_usage(monkeypatch, *, member=True, rows=None):
    session = _UsageSession(rows or [])
    return await _run(monkeypatch, "/usage", member=member, session=session)


async def test_usage_happy_path(monkeypatch):
    rows = [
        _call("brief", 8_200, 300),
        _call("ask", 3_100, 500),
        _call("ask", 60, 100),
    ]
    client, recorded = await _run_usage(monkeypatch, rows=rows)
    assert not recorded
    reply = client.sent[0][1]
    assert "LLM usage (7d): 3 calls · 11,360 tokens · avg 300ms" in reply
    assert "top: brief 8.2k · ask 3.2k" in reply
    assert len(reply) < 4096


async def test_usage_empty_ledger(monkeypatch):
    client, _ = await _run_usage(monkeypatch, rows=[])
    assert client.sent[0][1] == "No LLM calls logged in the last 7 days."


async def test_usage_unlinked_chat(monkeypatch):
    client, _ = await _run_usage(monkeypatch, member=False)
    assert "/start <code>" in client.sent[0][1]


def test_usage_token_formats_and_tiebreak():
    reply = telegram_bot._render_usage(
        {
            "calls": 3,
            "total_tokens": 1_500_999,
            "avg_latency_ms": 10,
            "by_feature": {
                "big": {"calls": 1, "total_tokens": 1_500_000},
                "beta": {"calls": 1, "total_tokens": 999},
                "alpha": {"calls": 1, "total_tokens": 999},
            },
        }
    )
    assert "top: big 1.5M · alpha 999 · beta 999" in reply


async def test_help_lists_usage(monkeypatch):
    client, _ = await _run(monkeypatch, "/help")
    assert "/usage" in client.sent[0][1]


async def test_plain_text_still_records_answer(monkeypatch):
    client, recorded = await _run(monkeypatch, "Shipped the retry job")
    assert recorded == ["Shipped the retry job"]
    assert not client.sent  # AnswerResult(reply=None) -> nothing sent
