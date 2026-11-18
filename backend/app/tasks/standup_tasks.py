"""Celery tasks for the standup flow: scheduled start, DMs, and analysis."""

import asyncio
import logging

from celery import Task
from sqlalchemy import select

from app.config import settings
from app.integrations.telegram_client import TelegramClient
from app.integrations.webex_client import WebexClient, WebexRecipientError, can_deliver
from app.models.standup import StandupEntry, StandupRound
from app.models.team import TeamMember
from app.services.standup_service import QUESTIONS, STEPS, StandupService
from app.tasks.celery_app import celery_app
from app.tasks.db import create_task_session

logger = logging.getLogger(__name__)

_INTRO = "Good morning! 🌅 Time for your daily standup."
_NUDGE = "⏰ Standup closes in ~15 minutes — reply here or in the app. Takes 30 seconds."


async def _deliver_to_member_webex(member: TeamMember, text: str) -> dict:
    """Webex guards + DM for one member. Returns {"sent": True} or {"skipped": reason}."""
    if not settings.webex_bot_token:
        return {"skipped": "webex_not_configured"}
    if not can_deliver(member.email):
        # Synthesized by the ADO sync (on-prem gives DOMAIN\\user, not an email).
        return {"skipped": "no_real_email"}
    client = WebexClient(settings.webex_bot_token, base_url=settings.webex_api_base)
    try:
        await client.send_to_email(member.email, text)
    except WebexRecipientError as exc:
        # Permanent for this member (no Webex account) — don't burn the retries.
        logger.warning("Standup DM skipped for %s: %s", member.email, exc)
        return {"skipped": "not_on_webex"}
    return {"sent": True}


async def _send_question(round_id: str, member_id: str) -> dict:
    async with create_task_session() as session:
        result = await session.execute(
            select(TeamMember).where(TeamMember.id == member_id)
        )
        member = result.scalar_one_or_none()
        if member is None:
            return {"skipped": "no_member"}

    if settings.app_public_url:
        # Webex can't call back into a LAN deployment, so answers are collected
        # through the in-app form; the DM just carries the personal link.
        link = (
            f"{settings.app_public_url.rstrip('/')}/?standup={round_id}&member={member_id}"
        )
        text = f"{_INTRO}\n\n👉 [Post your update here]({link}) — takes 30 seconds."
    else:
        text = f"{_INTRO}\n\n{QUESTIONS[STEPS[0]]}"
    outcome = await _deliver_to_member_webex(member, text)
    return {"round_id": round_id, "member_id": member_id, **outcome}


async def _start(trigger: str) -> dict:
    async with create_task_session() as session:
        service = StandupService(session)
        round_, members = await service.start_round(trigger=trigger)
        round_id = round_.id
        member_ids = [m.id for m in members]

    # Enqueue one DM per member + a hard cutoff to close/analyze the round.
    for member_id in member_ids:
        send_standup_question.delay(round_id, member_id)
    close_standup_round.apply_async(
        args=[round_id], countdown=settings.standup_cutoff_seconds
    )
    return {"round_id": round_id, "members": len(member_ids)}


async def _close(round_id: str) -> dict:
    async with create_task_session() as session:
        service = StandupService(session)
        round_ = await service.close_and_analyze(round_id)
        return {"round_id": round_id, "status": round_.status}


async def _nudge() -> dict:
    async with create_task_session() as session:
        round_ = (
            await session.execute(
                select(StandupRound)
                .where(StandupRound.status == "collecting")
                .order_by(StandupRound.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if round_ is None:
            return {"skipped": "no_open_round"}
        members = await StandupService(session).members_to_prompt()
        answered = set(
            (
                await session.execute(
                    select(StandupEntry.member_id).where(
                        StandupEntry.round_id == round_.id
                    )
                )
            ).scalars()
        )
        pending = [m for m in members if m.id not in answered]
        if not pending:
            return {"skipped": "all_answered"}

    nudged, skipped = 0, []
    for member in pending:
        if settings.app_public_url:
            link = (
                f"{settings.app_public_url.rstrip('/')}"
                f"/?standup={round_.id}&member={member.id}"
            )
            text = f"{_NUDGE}\n\n👉 [Post your update here]({link})"
        else:
            text = _NUDGE
        if settings.telegram_bot_token and member.telegram_chat_id:
            await TelegramClient(settings.telegram_bot_token).send_message(
                member.telegram_chat_id, text
            )
            outcome = {"sent": True}
        else:
            outcome = await _deliver_to_member_webex(member, text)
        if outcome.get("sent"):
            nudged += 1
        else:
            skipped.append({"member_id": member.id, **outcome})
    return {"round_id": round_.id, "nudged": nudged, "skipped": skipped}


@celery_app.task(bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True)
def send_standup_question(self: Task, round_id: str, member_id: str) -> dict:
    """DM a member the first standup question."""
    return asyncio.run(_send_question(round_id, member_id))


@celery_app.task(bind=True)
def start_scheduled_standup(self: Task) -> dict:
    """Beat-triggered: start a standup round and prompt everyone."""
    logger.info("Starting scheduled standup round")
    return asyncio.run(_start("scheduled"))


@celery_app.task(bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=True)
def close_standup_round(self: Task, round_id: str) -> dict:
    """Close collection and analyze (idempotent)."""
    logger.info("Closing standup round %s", round_id)
    return asyncio.run(_close(round_id))


@celery_app.task(bind=True)
def nudge_pending_standup(self: Task) -> dict:
    """Beat-triggered: DM members who haven't answered the open round."""
    logger.info("Nudging pending standup responders")
    return asyncio.run(_nudge())
