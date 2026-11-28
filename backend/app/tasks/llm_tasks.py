"""Celery task: DM when today's LLM tokens cross the daily budget (issue #62)."""

import asyncio
from datetime import datetime, time, timezone

from sqlalchemy import func, select

from app.config import settings
from app.integrations.standup_state import get_redis
from app.integrations.telegram_bot import _format_tokens
from app.integrations.telegram_client import TelegramClient
from app.models.llm_call import LLMCall
from app.models.team import TeamMember
from app.tasks.celery_app import celery_app
from app.tasks.db import create_task_session

_BUDGET_KEY = "llm_budget_alert"


async def check_llm_budget() -> dict:
    """DM Telegram-linked members when today's ledger total crosses the cap (once per UTC day)."""
    if settings.llm_daily_token_budget <= 0:
        return {"skipped": "no_budget"}

    budget = settings.llm_daily_token_budget
    today_start = datetime.combine(
        datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc
    )
    async with create_task_session() as session:
        today_tokens = (
            await session.execute(
                select(func.coalesce(func.sum(LLMCall.total_tokens), 0)).where(
                    LLMCall.created_at >= today_start
                )
            )
        ).scalar_one()
        if today_tokens <= budget:
            return {"status": "ok", "today_tokens": today_tokens, "budget": budget}

        marker = today_start.date().isoformat()
        redis = get_redis()
        try:
            if await redis.get(_BUDGET_KEY) == marker:
                return {"status": "over", "alerted": False}  # already alerted today

            members = (
                await session.execute(
                    select(TeamMember).where(TeamMember.telegram_chat_id.is_not(None))
                )
            ).scalars()
            text = (
                f"💸 LLM daily budget blown: {_format_tokens(today_tokens)} / "
                f"{_format_tokens(budget)} tokens"
                f" ({round(today_tokens / budget * 100)}%) — /usage for the breakdown"
            )
            sent = 0
            if settings.telegram_bot_token:
                client = TelegramClient(settings.telegram_bot_token)
                for member in members:
                    await client.send_message(member.telegram_chat_id, text)
                    sent += 1
            # SET ... EX 48h: the date marker rolls over by itself, so no
            # recovery-delete is needed (unlike the stall watchdog).
            await redis.set(_BUDGET_KEY, marker, ex=48 * 3600)
            return {"status": "over", "alerted": True, "sent": sent,
                    "today_tokens": today_tokens}
        finally:
            await redis.aclose()


# No autoretry: a failed tick (DB/Redis down) retries naturally on the next
# beat run, and retrying a half-sent alert would re-DM members.
@celery_app.task()
def check_llm_budget_task() -> dict:
    """Beat job: DM members when today's LLM tokens cross the daily budget."""
    return asyncio.run(check_llm_budget())
