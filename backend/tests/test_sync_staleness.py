"""Pure tests for the sync-stall watchdog (issue #50).

No network, no DB: ``create_task_session`` is patched to a fake session whose
queries return canned results in order, Redis is an in-memory fake, and the
Telegram client is recorded, never sent.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models.sync_state import SyncState
from app.models.team import TeamMember
from app.tasks import tfs_tasks


class FakeResult:
    def __init__(self, scalar=None, scalars=()):
        self._scalar = scalar
        self._scalars = list(scalars)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return list(self._scalars)


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


class FakeRedis:
    """Just enough async Redis (str values) for the stall-marker guard."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def aclose(self):
        pass


def _state(status, age_hours, error=None):
    return SyncState(
        provider="tfs",
        last_sync_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        last_status=status,
        last_error=error,
    )


def _member(name, chat_id):
    return TeamMember(id=str(uuid.uuid4()), name=name,
                      email=f"{name.split()[0].lower()}@northwind.dev",
                      telegram_chat_id=chat_id)


async def _run(monkeypatch, *, state=None, members=(), redis_data=None,
              sync_enabled=True):
    monkeypatch.setattr(settings, "task_sync_enabled", sync_enabled)
    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    monkeypatch.setattr(
        tfs_tasks, "create_task_session",
        lambda: _fake_ctx(
            [FakeResult(scalar=state), FakeResult(scalars=members)]
        ),
    )
    redis = FakeRedis(redis_data)
    monkeypatch.setattr(tfs_tasks, "get_redis", lambda: redis)
    sent = []

    class FakeTelegram:
        def __init__(self, token):
            pass

        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(tfs_tasks, "TelegramClient", FakeTelegram)
    result = await tfs_tasks.check_sync_staleness()
    return result, sent, redis


async def test_stalled_error_sync_alerts_each_linked_member_once(monkeypatch):
    state = _state("error", 0.1, error="TFS unreachable")
    sarah, tom = _member("Sarah Chen", "42"), _member("Tom Okafor", "43")
    result, sent, redis = await _run(monkeypatch, state=state, members=[sarah, tom])
    assert result["status"] == "stalled" and result["sent"] == 2
    assert [c for c, _ in sent] == ["42", "43"]
    assert "Backlog sync stalled" in sent[0][1]
    assert "TFS unreachable" in sent[0][1]
    assert redis.data["sync_stall_alert"] == state.last_sync_at.isoformat()


async def test_same_stall_again_sends_nothing(monkeypatch):
    state = _state("error", 0.1, error="TFS unreachable")
    sarah = _member("Sarah Chen", "42")
    marker = state.last_sync_at.isoformat()
    result, sent, _ = await _run(
        monkeypatch, state=state, members=[sarah],
        redis_data={"sync_stall_alert": marker},
    )
    assert result == {"status": "stalled", "alerted": False}
    assert not sent


async def test_fresh_ok_sync_sends_nothing_and_clears_marker(monkeypatch):
    state = _state("ok", 1.0)
    result, sent, redis = await _run(
        monkeypatch, state=state, members=[_member("Sarah Chen", "42")],
        redis_data={"sync_stall_alert": "old"},
    )
    assert result == {"status": "ok", "age_hours": 1.0}
    assert not sent and "sync_stall_alert" not in redis.data


async def test_disabled_sync_returns_early(monkeypatch):
    # Empty fake session: any query would assert "unexpected extra query".
    result, sent, redis = await _run(monkeypatch, sync_enabled=False)
    assert result == {"skipped": "sync_disabled"}
    assert not sent and not redis.data


async def test_stale_ok_sync_alerts_with_age_in_message(monkeypatch):
    state = _state("ok", 8.0)  # past the 6h default threshold
    result, sent, _ = await _run(
        monkeypatch, state=state, members=[_member("Sarah Chen", "42")]
    )
    assert result["sent"] == 1 and "8h ago" in sent[0][1]
