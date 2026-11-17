"""Telegram long-polling bot for standup collection.

Run as a standalone process: ``python -m app.integrations.telegram_bot``.

Responsibilities:
- Long-poll getUpdates (offset persisted in Redis so restarts don't replay).
- ``/start <code>``  -> link a Telegram account to a TeamMember.
- ``/status``        -> reply with the latest morning brief (linked chats only).
- ``/confidence``    -> portfolio confidence + active sprint bands (linked chats only).
- ``/help``          -> list commands.
- plain text          -> answer to the current standup question.

All DB / conversation logic lives in StandupService/BriefService; this module
is just the transport loop + routing.
"""

import asyncio
import logging

from sqlalchemy import select

from app.config import settings
from app.integrations import standup_state
from app.integrations.telegram_client import TelegramClient
from app.models.confidence import ConfidenceSnapshot
from app.models.sprint import Sprint
from app.services.ask_service import AskService
from app.services.brief_service import BriefService
from app.services.confidence_service import ConfidenceService
from app.services.standup_service import StandupService
from app.tasks.db import create_task_session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0

_HELP_TEXT = (
    "Commands:\n"
    "/start <code> — link this chat to your account\n"
    "/status — the latest morning brief\n"
    "/confidence — portfolio confidence at a glance\n"
    "/ask <question> — free-text Q&A over the live snapshot (remembers the last few turns)\n"
    "Anything else (plain text) is your standup answer."
)
_LINK_HINT = (
    "👋 This chat isn't linked yet. Generate a code in the app and send "
    "/start <code>."
)

# Per-chat /ask memory so follow-up questions ("what about last sprint?") work.
_ASK_HISTORY: dict[str, list[dict[str, str]]] = {}
# ponytail: in-process only — restart wipes it; move to Redis if the bot ever runs multi-process

_SPRINT_CAP = 5


async def _fetch_confidence(
    session,
) -> tuple[float | None, list[tuple[Sprint, ConfidenceSnapshot | None]]]:
    """Portfolio headline + (active sprint, latest snapshot) pairs."""
    conf = ConfidenceService(session)
    portfolio = await conf.get_portfolio_confidence()
    sprints = (
        await session.execute(select(Sprint).where(Sprint.status == "active"))
    ).scalars().all()
    snaps = await conf.latest_snapshots_for([s.id for s in sprints])
    return portfolio, [(s, snaps.get(s.id)) for s in sprints]


def _render_confidence(
    portfolio: float | None, sprints: list[tuple[Sprint, ConfidenceSnapshot | None]]
) -> str:
    if portfolio is None:
        return "No confidence data yet — snapshots land nightly at 23:30."
    lines = [f"*Portfolio confidence: {portfolio:.0f}/100*", "*Active sprints:*"]
    for sprint, snap in sprints[:_SPRINT_CAP]:
        if snap is None:
            lines.append(f"• {sprint.name}: no snapshot yet")
            continue
        arrow = {"improving": "↑", "declining": "↓"}.get(
            (snap.factors or {}).get("direction", "steady"), "→"
        )
        lines.append(
            f"• {sprint.name}: {snap.confidence_value:.0f}/100"
            f" ({ConfidenceService.value_to_band(snap.confidence_value)} {arrow})"
        )
    if len(sprints) > _SPRINT_CAP:
        lines.append(f"+{len(sprints) - _SPRINT_CAP} more")
    return "\n".join(lines)


async def handle_update(update: dict, client: TelegramClient) -> None:
    """Route a single Telegram update."""
    message = update.get("message")
    if not message:
        return  # ignore non-message updates (edited, channel posts, etc.)

    chat_id = str(message["chat"]["id"])
    text = (message.get("text") or "").strip()
    username = message.get("from", {}).get("username")
    if not text:
        return

    async with create_task_session() as session:
        service = StandupService(session)

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            code = parts[1].strip() if len(parts) > 1 else ""
            reply = await service.link_member(code, chat_id, username)
            await client.send_message(chat_id, reply)
            return

        if text.startswith("/"):
            command = text.split(maxsplit=1)[0].split("@")[0]
            if command == "/status":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    brief = await BriefService(session).latest()
                    reply = (
                        "No brief yet — the morning brief lands at 08:00. "
                        "Generate one from the Briefs page."
                        if brief is None
                        else BriefService.to_telegram_text(brief)
                        + "\n\n— full history on the Briefs page."
                    )
            elif command == "/confidence":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    portfolio, sprints = await _fetch_confidence(session)
                    reply = _render_confidence(portfolio, sprints)
            elif command == "/ask":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2 or not parts[1].strip():
                        reply = "Usage: /ask <question> — e.g. /ask what slipped this sprint?"
                    else:
                        question = parts[1].strip()
                        try:
                            answer = await AskService(session).answer(
                                question, history=_ASK_HISTORY.get(chat_id)
                            )
                            reply = answer[:4000]  # stay under Telegram's 4096-char cap
                            _ASK_HISTORY[chat_id] = (
                                _ASK_HISTORY.get(chat_id, [])
                                + [
                                    {"role": "user", "content": question},
                                    {"role": "assistant", "content": reply},
                                ]
                            )[-6:]
                        except Exception:  # noqa: BLE001 - one bad update must not kill the loop
                            logger.exception("AskService failed for chat %s", chat_id)
                            reply = "Couldn't answer that — the LLM call failed. Try again."
            elif command == "/help":
                reply = _HELP_TEXT
            else:
                reply = f"Unknown command {command}. Send /help to see what I understand."
            await client.send_message(chat_id, reply)
            return

        # Otherwise treat as an answer to the active standup question.
        result = await service.record_answer(chat_id, text)
        if result.reply:
            await client.send_message(chat_id, result.reply)
        if result.round_complete and result.round_id:
            from app.tasks.standup_tasks import close_standup_round

            close_standup_round.delay(result.round_id)


async def run_polling() -> None:
    client = TelegramClient(settings.telegram_bot_token)
    redis = standup_state.get_redis()
    offset = await standup_state.get_offset(redis)
    backoff = _BACKOFF_START
    logger.info("Telegram bot polling started (offset=%s)", offset)

    while True:
        try:
            updates = await client.get_updates(offset=offset, timeout=30)
            backoff = _BACKOFF_START  # reset after a successful poll
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await handle_update(update, client)
                except Exception:  # noqa: BLE001 - one bad update must not kill the loop
                    logger.exception("Error handling update %s", update.get("update_id"))
                await standup_state.set_offset(redis, offset)
        except Exception:  # noqa: BLE001 - network/API errors: back off and retry
            logger.exception("Polling error; backing off %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


def main() -> None:
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set; telegram bot exiting.")
        return
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()
