"""Celery tasks for external task-provider sync (push + periodic pull)."""

import asyncio
import logging
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import select

from app.config import settings
from app.integrations.factory import get_task_provider
from app.integrations.standup_state import get_redis
from app.integrations.task_provider import ExternalTaskCreate, ExternalTaskUpdate
from app.integrations.telegram_client import TelegramClient
from app.models.sync_state import SyncState
from app.models.task import Task as TaskModel
from app.models.team import TeamMember
from app.services.task_service import TaskService
from app.tasks.celery_app import celery_app
from app.tasks.db import create_task_session

logger = logging.getLogger(__name__)


async def _assignee_name(session, assignee_id: str | None) -> str | None:
    if not assignee_id:
        return None
    result = await session.execute(
        select(TeamMember.name).where(TeamMember.id == assignee_id)
    )
    return result.scalar_one_or_none()


async def _push(task_id: str, op: str) -> dict:
    if not settings.push_enabled:
        return {"skipped": "push_disabled"}

    async with create_task_session() as session:
        result = await session.execute(
            select(TaskModel).where(TaskModel.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return {"skipped": "task_not_found"}

        # Echo guard: never push tasks that originated in the external system.
        if task.source != "local":
            return {"skipped": f"source={task.source}"}

        provider = get_task_provider()
        assignee_name = await _assignee_name(session, task.assignee_id)

        if op == "create":
            if task.external_id:
                return {"skipped": "already_has_external_id"}
            created = await provider.create_task(
                ExternalTaskCreate(
                    title=task.title,
                    description=task.description,
                    acceptance_criteria=task.acceptance_criteria,
                    status=task.status,
                    priority=task.priority,
                    assignee_name=assignee_name,
                    story_points=task.story_points,
                )
            )
            task.external_id = created.external_id
        else:  # update
            if not task.external_id:
                # Nothing to update remotely yet — treat as create.
                created = await provider.create_task(
                    ExternalTaskCreate(
                        title=task.title,
                        description=task.description,
                        acceptance_criteria=task.acceptance_criteria,
                        status=task.status,
                        priority=task.priority,
                        assignee_name=assignee_name,
                        story_points=task.story_points,
                    )
                )
                task.external_id = created.external_id
            else:
                await provider.update_task(
                    task.external_id,
                    ExternalTaskUpdate(
                        title=task.title,
                        description=task.description,
                        acceptance_criteria=task.acceptance_criteria,
                        status=task.status,
                        priority=task.priority,
                        assignee_name=assignee_name,
                        story_points=task.story_points,
                    ),
                )

        task.last_synced_at = datetime.now(timezone.utc)
        await session.commit()
        return {"task_id": task_id, "op": op, "external_id": task.external_id}


async def _sync() -> dict:
    async with create_task_session() as session:
        service = TaskService(session)
        provider = get_task_provider()
        label = settings.integration_provider

        # Upsert sync_state row
        state_result = await session.execute(
            select(SyncState).where(SyncState.provider == label)
        )
        state = state_result.scalar_one_or_none()
        if state is None:
            state = SyncState(provider=label)
            session.add(state)

        try:
            result = await service.import_from_provider(provider, source_label=label)
            inserted = len(result["inserted"])
            updated = len(result["updated"])
            state.last_sync_at = datetime.now(timezone.utc)
            state.last_status = "ok"
            state.last_error = None
            state.inserted_count = inserted
            state.updated_count = updated
            await session.commit()
            return {"provider": label, "inserted": inserted, "updated": updated}
        except Exception as exc:  # noqa: BLE001 - record then re-raise for retry
            state.last_sync_at = datetime.now(timezone.utc)
            state.last_status = "error"
            state.last_error = str(exc)[:500]
            await session.commit()
            raise


@celery_app.task(
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def push_task_to_provider(self: Task, task_id: str, op: str) -> dict:
    """Push a locally created/updated task to the external provider."""
    logger.info(f"Pushing task {task_id} (op={op}) to provider")
    return asyncio.run(_push(task_id, op))


_STALL_KEY = "sync_stall_alert"


async def check_sync_staleness() -> dict:
    """DM Telegram-linked members when the backlog pull is stalled (once per stall)."""
    if not settings.task_sync_enabled:
        return {"skipped": "sync_disabled"}

    async with create_task_session() as session:
        state = (
            await session.execute(
                select(SyncState).order_by(SyncState.last_sync_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if state is None or state.last_sync_at is None:
            return {"skipped": "never_synced"}

        ts = state.last_sync_at
        if ts.tzinfo is None:  # naive timestamps (SQLite/demo) are UTC
            ts = ts.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        stalled = state.last_status == "error" or age_hours > settings.sync_stale_hours

        redis = get_redis()
        try:
            if not stalled:
                await redis.delete(_STALL_KEY)  # recovery: next stall alerts again
                return {"status": "ok", "age_hours": round(age_hours, 1)}

            marker = ts.isoformat()
            if await redis.get(_STALL_KEY) == marker:
                return {"status": "stalled", "alerted": False}  # already alerted

            members = (
                await session.execute(
                    select(TeamMember).where(
                        TeamMember.telegram_chat_id.is_not(None)
                    )
                )
            ).scalars()
            text = f"⚠️ Backlog sync stalled: last ok sync {age_hours:.0f}h ago"
            if state.last_error:
                text += f"\n{state.last_error}"
            sent = 0
            if settings.telegram_bot_token:
                client = TelegramClient(settings.telegram_bot_token)
                for member in members:
                    await client.send_message(member.telegram_chat_id, text)
                    sent += 1
            await redis.set(_STALL_KEY, marker)
            return {"status": "stalled", "alerted": True, "sent": sent}
        finally:
            await redis.aclose()


@celery_app.task(
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def sync_tfs_tasks(self: Task) -> dict:
    """Periodic pull of work items from the external provider into our DB."""
    logger.info("Syncing tasks from external provider")
    return asyncio.run(_sync())


# No autoretry: a failed tick (DB/Redis down) retries naturally on the next
# beat run, and retrying a half-sent alert would re-DM members.
@celery_app.task()
def check_sync_staleness_task() -> dict:
    """Beat job: DM members when the backlog sync is stalled (once per stall)."""
    return asyncio.run(check_sync_staleness())
