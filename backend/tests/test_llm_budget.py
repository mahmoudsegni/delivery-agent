"""Pure tests for the LLM daily-budget watchdog (issue #62).

No network, no DB: canned query results (or a ledger session that applies the
sum query's own where-clause cutoff), in-memory Redis, recorded Telegram.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models.llm_call import LLMCall
from app.models.team import TeamMember
from app.tasks import llm_tasks


class FakeResult:
    def __init__(self, scalar=None, scalars=()):
        self._scalar = scalar
        self._scalars = list(scalars)

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return list(self._scalars)


class FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt):
        assert self._results, f"unexpected extra query: {stmt}"
        return self._results.pop(0)


class LedgerSession:
    """Executes the sum query against fake rows, using the statement's own cutoff."""

    def __init__(self, rows, members):
        self.rows, self.members = rows, members
        self.queries = 0

    async def execute(self, stmt):
        self.queries += 1
        if self.queries == 1:  # the SUM ... WHERE created_at >= today_start
            cutoff = stmt.whereclause.right.value
            total = sum(r.total_tokens for r in self.rows if r.created_at >= cutoff)
            return FakeResult(scalar=total)
        return FakeResult(scalars=self.members)


def _fake_ctx(session):
    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return _Ctx()


class FakeRedis:
    """Just enough async Redis (str values) for the once-per-day marker."""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.ex = None

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ex = ex

    async def aclose(self):
        pass


def _member(name, chat_id):
    return TeamMember(id=str(uuid.uuid4()), name=name,
                      email=f"{name.split()[0].lower()}@northwind.dev",
                      telegram_chat_id=chat_id)


def _ledger_row(total_tokens, days_ago=0.0):
    return LLMCall(
        feature="brief",
        model="llama-3",
        prompt_tokens=total_tokens // 2,
        completion_tokens=total_tokens - total_tokens // 2,
        total_tokens=total_tokens,
        latency_ms=100,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


async def _run(monkeypatch, *, ledger=None, today_tokens=0, members=(),
              redis_data=None, budget=1000):
    monkeypatch.setattr(settings, "llm_daily_token_budget", budget)
    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    if ledger is None:
        session = FakeSession(
            [FakeResult(scalar=today_tokens), FakeResult(scalars=members)]
        )
    else:
        session = LedgerSession(ledger, members)
    monkeypatch.setattr(llm_tasks, "create_task_session", lambda: _fake_ctx(session))
    redis = FakeRedis(redis_data)
    monkeypatch.setattr(llm_tasks, "get_redis", lambda: redis)
    sent = []

    class FakeTelegram:
        def __init__(self, token):
            pass

        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(llm_tasks, "TelegramClient", FakeTelegram)
    result = await llm_tasks.check_llm_budget()
    return result, sent, redis


async def test_over_budget_dms_each_linked_member_once(monkeypatch):
    sarah, tom = _member("Sarah Chen", "42"), _member("Tom Okafor", "43")
    result, sent, redis = await _run(monkeypatch, today_tokens=1_500, members=[sarah, tom])
    assert result["status"] == "over" and result["alerted"] and result["sent"] == 2
    assert [c for c, _ in sent] == ["42", "43"]
    assert "💸 LLM daily budget blown: 1.5k / 1.0k tokens (150%)" in sent[0][1]
    assert "/usage" in sent[0][1]
    assert redis.data["llm_budget_alert"] == datetime.now(timezone.utc).date().isoformat()
    assert redis.ex == 48 * 3600


async def test_marker_already_set_to_today_sends_nothing(monkeypatch):
    marker = datetime.now(timezone.utc).date().isoformat()
    result, sent, _ = await _run(
        monkeypatch, today_tokens=1_500,
        members=[_member("Sarah Chen", "42")],
        redis_data={"llm_budget_alert": marker},
    )
    assert result == {"status": "over", "alerted": False}
    assert not sent


async def test_under_budget_sends_nothing_and_sets_no_marker(monkeypatch):
    result, sent, redis = await _run(
        monkeypatch, today_tokens=999, members=[_member("Sarah Chen", "42")]
    )
    assert result == {"status": "ok", "today_tokens": 999, "budget": 1000}
    assert not sent and "llm_budget_alert" not in redis.data


async def test_zero_budget_skips_entirely(monkeypatch):
    # Empty fake session: any query would assert "unexpected extra query".
    result, sent, redis = await _run(monkeypatch, budget=0)
    assert result == {"skipped": "no_budget"}
    assert not sent and not redis.data


async def test_sum_counts_only_rows_from_today_utc(monkeypatch):
    # One row today at 1200 + one 2 days old at 500, budget 1000: only today counts.
    rows = [_ledger_row(1_200), _ledger_row(500, days_ago=2)]
    result, sent, _ = await _run(
        monkeypatch, ledger=rows, members=[_member("Sarah Chen", "42")]
    )
    assert result["today_tokens"] == 1_200
    assert result["status"] == "over" and result["sent"] == 1
