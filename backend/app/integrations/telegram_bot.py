"""Telegram long-polling bot for standup collection.

Run as a standalone process: ``python -m app.integrations.telegram_bot``.

Responsibilities:
- Long-poll getUpdates (offset persisted in Redis so restarts don't replay).
- ``/start <code>``  -> link a Telegram account to a TeamMember.
- ``/status``        -> reply with the latest morning brief (linked chats only).
- ``/confidence``    -> portfolio confidence + active sprint bands (linked chats only).
- ``/forecast``     -> Monte Carlo completion odds for the active sprint (linked chats only).
- ``/blockers``      -> deterministic stuck-work report from the heuristic detector (linked chats only).
- ``/groom``         -> backlog hygiene: stale, unestimated, unassigned (linked chats only).
- ``/team``          -> per-member capacity vs assigned hours for the active sprint (linked chats only).
- ``/usage``         -> LLM token spend for the last 7 days, from the ledger (linked chats only).
- ``/skip [reason]`` -> mark yourself out of the open standup round (no nudges, not missing).
- ``/help``          -> list commands.
- plain text          -> answer to the current standup question.

All DB / conversation logic lives in StandupService/BriefService; this module
is just the transport loop + routing.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.api.v1.dashboard import summarize_usage
from app.config import settings
from app.integrations import standup_state
from app.integrations.telegram_client import TelegramClient
from app.models.confidence import ConfidenceSnapshot
from app.models.llm_call import LLMCall
from app.models.sprint import Sprint
from app.schemas.dashboard import TeamLoadRow
from app.services.agent_analysis_service import AgentAnalysisService
from app.services.ask_service import AskService
from app.services.brief_service import BriefService
from app.services.confidence_service import ConfidenceService
from app.services.dashboard_service import DashboardService
from app.services.forecast_service import forecast
from app.services.sprint_service import SprintNotFoundError
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
    "/forecast — will the active sprint finish on time?\n"
    "/blockers — what's stuck right now (no LLM needed)\n"
    "/team — capacity vs assigned load this sprint (no LLM needed)\n"
    "/groom — backlog hygiene: stale, unestimated, unassigned (no LLM needed)\n"
    "/usage — LLM spend, last 7 days\n"
    "/skip <reason> — mark yourself out of today's standup\n"
    "/ask <question> — free-text Q&A over the live snapshot (remembers the last few turns)\n"
    "/reset — clear /ask conversation memory\n"
    "Anything else (plain text) is your standup answer."
)
_LINK_HINT = (
    "👋 This chat isn't linked yet. Generate a code in the app and send "
    "/start <code>."
)

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


_TEAM_ROW_CAP = 20


async def _fetch_team_load(session) -> list[TeamLoadRow] | None:
    """Per-member load for the active sprint; None when no sprint is active."""
    sprint = (
        await session.execute(
            select(Sprint).where(Sprint.status == "active").limit(1)
        )
    ).scalar_one_or_none()
    if sprint is None:
        return None
    return await DashboardService(session).team_load(sprint.id)


def _render_team(rows: list[TeamLoadRow]) -> str:
    """Header + one line per member, over-allocated first, then by load %."""
    rows = [r for r in rows if r.capacity_hours or r.assigned_hours]

    def _key(r: TeamLoadRow):
        pct = (
            r.assigned_hours / r.capacity_hours
            if r.capacity_hours
            else (float("inf") if r.assigned_hours else 0.0)
        )
        return (0 if r.assigned_hours > r.capacity_hours else 1, -pct)

    rows = sorted(rows, key=_key)
    over = sum(1 for r in rows if r.assigned_hours > r.capacity_hours)
    lines = []
    for r in rows[:_TEAM_ROW_CAP]:
        pct = (
            f"{r.assigned_hours / r.capacity_hours:.0%}"
            if r.capacity_hours
            else "n/a"
        )
        flag = "⚠️ " if r.assigned_hours > r.capacity_hours else ""
        lines.append(
            f"{flag}{r.name} — {r.assigned_hours}/{r.capacity_hours}h ({pct})"
            f" · {r.open_points} pts open"
        )
    if len(rows) > _TEAM_ROW_CAP:
        lines.append(f"+{len(rows) - _TEAM_ROW_CAP} more")
    return "\n".join([f"👥 Team load — {over} over capacity"] + lines)[:4000]


_USAGE_DAYS = 7


async def _fetch_llm_usage(session) -> dict:
    """7-day LLM ledger summary via the shared dashboard aggregator."""
    rows = (
        await session.execute(
            select(LLMCall).where(
                LLMCall.created_at
                >= datetime.now(timezone.utc) - timedelta(days=_USAGE_DAYS)
            )
        )
    ).scalars().all()
    return summarize_usage(rows, _USAGE_DAYS)


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _render_usage(payload: dict) -> str:
    if not payload["calls"]:
        return "No LLM calls logged in the last 7 days."
    top = sorted(
        payload["by_feature"].items(),
        key=lambda kv: (-kv[1]["total_tokens"], kv[0]),  # tokens desc, name asc for determinism
    )[:3]
    tops = " · ".join(
        f"{name} {_format_tokens(stats['total_tokens'])}" for name, stats in top
    )
    return (
        f"LLM usage (7d): {payload['calls']} calls"
        f" · {payload['total_tokens']:,} tokens"
        f" · avg {payload['avg_latency_ms']}ms\ntop: {tops}"
    )


def _render_forecast(sprint: Sprint, payload: dict) -> str:
    """One compact block from the forecast payload (see forecast_service)."""
    if payload.get("done"):
        return f"Sprint {sprint.name} is already done."
    if not payload.get("sufficient_history", True):
        return "Not enough throughput history yet — need 2+ active weeks."
    end = sprint.end_date.isoformat()
    return (
        f"🔮 {sprint.name} — {payload['remaining']} {payload['unit']} left, ends {end}\n"
        f"p50 {payload['p50_days']}d · p85 {payload['p85_days']}d · "
        f"{payload['on_time_probability']:.0%} odds by {end}"
    )


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
            elif command == "/forecast":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    sprint = (
                        await session.execute(
                            select(Sprint)
                            .where(Sprint.status == "active")
                            .order_by(Sprint.end_date.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if sprint is None:
                        reply = "No active sprint to forecast."
                    else:
                        try:
                            reply = _render_forecast(sprint, await forecast(session, sprint.id))
                        except SprintNotFoundError:
                            reply = "Sprint vanished."
            elif command == "/blockers":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    data = await AgentAnalysisService(session).detect_blockers_data()
                    s = data["summary"]
                    lines = [
                        f"[{b['severity']}] {b['item']} — {b['title']} · {b['type']}, {b['daysBlocked']}d ({b['member']})"
                        for b in data["blockers"][:10]
                    ]
                    if not lines:
                        reply = "Nothing stuck right now 🎉"
                    else:
                        header = (
                            f"🚧 {s['totalBlockers']} stuck item(s) — "
                            f"{s['critical']} high, {s['medium']} medium, {s['low']} low"
                            + (f" ({data['sprint']})" if data["sprint"] else "")
                        )
                        reply = ("\n".join([header] + lines))[:4000]
            elif command == "/groom":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    data = await AgentAnalysisService(session).grooming_data()
                    s = data["summary"]
                    lines = [
                        f"[{i['category']}] {i['ref']} {i['title']} · {i['days']}d"
                        for i in data["items"][:10]
                    ]
                    if not lines:
                        reply = "Backlog is clean ✨"
                    else:
                        header = (
                            f"🧹 Backlog hygiene — {s['stale']} stale · "
                            f"{s['unestimated']} unestimated · {s['unassigned']} unassigned"
                        )
                        reply = ("\n".join([header] + lines))[:4000]
            elif command == "/team":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    rows = await _fetch_team_load(session)
                    reply = (
                        "No active sprint — /team shows load for the current sprint."
                        if rows is None
                        else _render_team(rows)
                    )
            elif command == "/usage":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    reply = _render_usage(await _fetch_llm_usage(session))
            elif command == "/skip":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    parts = text.split(maxsplit=1)
                    reason = parts[1].strip() if len(parts) > 1 else ""
                    reply = await service.mark_skipped(chat_id, reason)
            elif command == "/ask":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2 or not parts[1].strip():
                        reply = "Usage: /ask <question> — e.g. /ask what slipped this sprint?"
                    else:
                        question = parts[1].strip()
                        redis = standup_state.get_redis()
                        try:
                            history = await standup_state.get_ask_history(redis, chat_id)
                            answer = await AskService(session).answer(
                                question, history=history
                            )
                            reply = answer[:4000]  # stay under Telegram's 4096-char cap
                            await standup_state.set_ask_history(
                                redis,
                                chat_id,
                                (history or [])
                                + [
                                    {"role": "user", "content": question},
                                    {"role": "assistant", "content": reply},
                                ],
                            )
                        except Exception:  # noqa: BLE001 - one bad update must not kill the loop
                            logger.exception("AskService failed for chat %s", chat_id)
                            reply = "Couldn't answer that — the LLM call failed. Try again."
                        finally:
                            await redis.aclose()
            elif command == "/reset":
                if await service.member_for_chat(chat_id) is None:
                    reply = _LINK_HINT
                else:
                    r = standup_state.get_redis()
                    try:
                        await standup_state.delete_ask_history(r, chat_id)
                    finally:
                        await r.aclose()
                    reply = "Fresh start — /ask has no memory now."
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
