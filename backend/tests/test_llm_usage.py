"""Pure tests for the LLM usage ledger (issue #43).

Same philosophy as test_groq_retry: no network, no DB — httpx.MockTransport
serves the responses, ``_record_usage`` is monkeypatched to capture (or blow
up), so nothing touches a session or waits.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from app.api.v1.dashboard import summarize_usage
from app.config import settings
from app.services import groq_client
from app.services.groq_client import groq_chat


def ok_with_usage() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        },
    )


def mount(monkeypatch, handler) -> list[dict]:
    """Point groq_chat at the fake transport; return the captured ledger rows."""
    recorded: list[dict] = []

    async def fake_record(feature, model, pt, ct, tt, latency_ms):
        recorded.append(
            {
                "feature": feature,
                "model": model,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "latency_ms": latency_ms,
            }
        )

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    monkeypatch.setattr(groq_client, "_record_usage", fake_record)
    monkeypatch.setattr(groq_client.httpx, "AsyncClient", factory)
    return recorded


def serve(request):
    return ok_with_usage()


def test_usage_recorded_on_success(monkeypatch):
    recorded = mount(monkeypatch, serve)
    out = asyncio.run(groq_chat([{"role": "user", "content": "hi"}], feature="ask"))
    assert out == "pong"
    assert recorded == [
        {
            "feature": "ask",
            "model": settings.groq_model,
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "latency_ms": recorded[0]["latency_ms"],
        }
    ]
    assert recorded[0]["latency_ms"] >= 0


def test_ledger_failure_never_breaks_the_call(monkeypatch):
    mount(monkeypatch, serve)

    class BadSessionMaker:
        # __call__ returns an async-context manager whose entry blows up.
        def __call__(self):
            raise RuntimeError("db down")

    import app.database as dbmod

    monkeypatch.setattr(dbmod, "async_session_maker", BadSessionMaker())
    out = asyncio.run(groq_chat([{"role": "user", "content": "hi"}], feature="brief"))
    assert out == "pong"


def test_demo_mode_logs_zero_tokens(monkeypatch):
    recorded: list[dict] = []

    async def fake_record(feature, model, pt, ct, tt, latency_ms):
        recorded.append({"feature": feature, "model": model, "total_tokens": tt})

    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(groq_client, "_record_usage", fake_record)
    asyncio.run(groq_chat([{"role": "user", "content": "hi"}], feature="standup"))
    assert recorded == [{"feature": "standup", "model": "demo", "total_tokens": 0}]


def row(feature: str, total_tokens: int, latency_ms: int, age_days: float) -> SimpleNamespace:
    return SimpleNamespace(
        feature=feature,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )


def test_summarize_usage_totals_split_and_window():
    rows = [
        row("ask", 100, 200, 0.5),
        row("ask", 50, 400, 1.0),
        row("brief", 10, 1000, 2.0),
        row("ask", 999, 999, 9.0),  # older than the window → excluded
    ]
    out = summarize_usage(rows, 7)
    assert out["calls"] == 3
    assert out["total_tokens"] == 160
    assert out["avg_latency_ms"] == round((200 + 400 + 1000) / 3)
    assert out["by_feature"] == {
        "ask": {"calls": 2, "total_tokens": 150},
        "brief": {"calls": 1, "total_tokens": 10},
    }


def test_summarize_usage_empty():
    assert summarize_usage([], 7) == {
        "days": 7,
        "calls": 0,
        "total_tokens": 0,
        "avg_latency_ms": 0,
        "by_feature": {},
    }
