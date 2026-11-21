"""Delivery autopilot: triage → dispatch/propose, nudges, blocker escalations.

Runtime state lives in ``app_settings["autopilot"]`` merged over env-seeded
defaults, so the off/propose/auto dial is live-togglable without a restart (the
beat tick is always scheduled; mode is read per tick). Daily counters and the
skip audit live in ``app_settings["autopilot:counters:<iso-date>"]`` — that doc
is both the budget ledger and what ``GET /autopilot`` surfaces as "today".

Triage goes through the raw-Groq JSON path (``groq_json``) — NEVER
``spec_llm.triage_spec``, whose litellm path has no Groq branch and silently
returns suitable/low. Dispatch replicates the POST /coding/dispatch sequence
exactly so autopilot runs are indistinguishable from manual ones downstream.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.webex_client import WebexClient, is_reachable
from app.models.agent_run import AgentRun
from app.models.app_setting import AppSetting
from app.models.proposal import Proposal
from app.models.sprint import Sprint
from app.models.task import Task
from app.models.team import TeamMember
from app.services.coding_run_service import CodingRunService
from app.services.groq_client import GroqError, groq_json
from app.tasks.coding_tasks import dispatch_coding_agent

logger = logging.getLogger(__name__)

# app_settings key for the runtime dial (mode/limit/repository overrides).
STATE_KEY = "autopilot"

# Keep the skip audit bounded — it's a "why was this skipped" surface, not a log.
_MAX_SKIPS = 20

_EMPTY_COUNTERS: dict[str, Any] = {
    "triaged": 0,
    "dispatched": 0,
    "suggested": 0,
    "skipped": 0,
    "nudged": 0,
    "escalated": 0,
    "skips": [],
}

_TRIAGE_SYSTEM_PROMPT = (
    "You are a delivery manager deciding whether a work item can be handed to "
    "an autonomous AI coding agent. Return ONLY a single valid JSON object. "
    "No markdown, no code fences, no prose outside the JSON."
)


def _counters_key(day: date) -> str:
    return f"autopilot:counters:{day.isoformat()}"


class AutopilotService:
    """State, counters, and the per-tick control loop."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # State (app_settings doc merged over env defaults)
    # ------------------------------------------------------------------
    @staticmethod
    def _env_defaults() -> dict[str, Any]:
        return {
            "mode": settings.autopilot_default_mode,
            "daily_limit": settings.autopilot_daily_limit,
            "repository": settings.azure_repo_id or None,
            "stale_task_days": settings.autopilot_stale_task_days,
            "blocker_escalation_days": settings.autopilot_blocker_escalation_days,
        }

    async def _save_setting(self, key: str, value: dict[str, Any]) -> None:
        setting = await self.db.get(AppSetting, key)
        if setting is None:
            setting = AppSetting(key=key, value=value)
            self.db.add(setting)
        else:
            # Assign a fresh dict — in-place JSONB mutation isn't change-tracked.
            setting.value = value
        setting.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

    async def get_state(self) -> dict[str, Any]:
        setting = await self.db.get(AppSetting, STATE_KEY)
        stored = (setting.value or {}) if setting else {}
        return {**self._env_defaults(), **stored}

    async def update_state(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge a partial update into the stored state doc. Caller commits."""
        setting = await self.db.get(AppSetting, STATE_KEY)
        stored = (setting.value or {}) if setting else {}
        merged = {**stored, **patch}
        await self._save_setting(STATE_KEY, merged)
        return {**self._env_defaults(), **merged}

    # ------------------------------------------------------------------
    # Daily counters / audit
    # ------------------------------------------------------------------
    async def get_counters(self, day: date | None = None) -> dict[str, Any]:
        day = day or date.today()
        setting = await self.db.get(AppSetting, _counters_key(day))
        stored = (setting.value or {}) if setting else {}
        counters = {**_EMPTY_COUNTERS, **stored}
        counters["skips"] = list(stored.get("skips") or [])
        return counters

    async def get_status(self) -> dict[str, Any]:
        """State + today's counters + remaining auto-dispatch budget (GET/PUT shape)."""
        state = await self.get_state()
        counters = await self.get_counters()
        budget_remaining = max(
            0, int(state["daily_limit"]) - int(counters.get("dispatched", 0))
        )
        return {**state, "today": counters, "budget_remaining": budget_remaining}

    # ------------------------------------------------------------------
    # Tick step 1-3: candidates → triage → dispatch/propose/skip
    # ------------------------------------------------------------------
    async def _candidates(self) -> list[Task]:
        """Untouched backlog/todo tasks with no coding run yet, oldest first."""
        result = await self.db.execute(
            select(Task)
            .outerjoin(AgentRun, AgentRun.task_id == Task.id)
            .where(
                Task.status.in_(("backlog", "todo")),
                Task.autopilot_state.is_(None),
                AgentRun.id.is_(None),
            )
            .order_by(Task.created_at)
            .limit(settings.autopilot_triage_per_tick)
        )
        return list(result.scalars().all())

    @staticmethod
    def _triage_prompt(task: Task) -> str:
        return (
            "Decide whether this task is suitable for an autonomous AI coding "
            "agent to implement end-to-end without human help.\n\n"
            "TASK:\n"
            f"Title: {task.title}\n"
            f"Description: {task.description or '(none)'}\n"
            f"Technology: {task.technology or '(unspecified)'}\n"
            f"Story points: {task.story_points if task.story_points is not None else '(unestimated)'}\n\n"
            "Suitable tasks are small, well-specified coding changes. "
            "Unsuitable tasks are vague, need product or design decisions, span "
            "teams, or touch infrastructure, credentials, or data migrations.\n\n"
            "Return ONLY a JSON object with this exact shape:\n"
            '{"suitable": true, "confidence": "low|medium|high", "reason": "one short sentence"}'
        )

    async def _triage(self, task: Task) -> dict[str, Any]:
        return await groq_json(
            self._triage_prompt(task), _TRIAGE_SYSTEM_PROMPT, feature="autopilot"
        )

    async def _dispatch(
        self, task: Task, repository: str | None, proposal: Proposal | None = None
    ) -> AgentRun:
        """Replicate the POST /coding/dispatch sequence exactly.

        create_run → status "dispatched" → error None → commit → Celery delay.
        The optional approved proposal is flipped to ``executed`` in the SAME
        commit so a crash between commit and delay can't re-dispatch it.
        """
        svc = CodingRunService(self.db)
        run = await svc.create_run(task.id, repository=repository)
        run.status = "dispatched"
        run.error = None
        task.autopilot_state = "dispatched"
        if proposal is not None:
            proposal.state = "executed"
            proposal.execution_result = {"run_id": run.id}
        await self.db.commit()
        dispatch_coding_agent.delay(run.id)
        return run

    def _propose(
        self, task: Task, triage: dict[str, Any], repository: str | None
    ) -> Proposal:
        proposal = Proposal(
            type="autopilot_dispatch",
            state="pending_review",
            content={
                "task_id": task.id,
                "task_title": task.title,
                "repository": repository,
                "triage": triage,
            },
            reasoning=triage.get("reason") or "Autopilot triage: suitable for the coding agent.",
            confidence=triage.get("confidence"),
            created_by_agent="autopilot",
        )
        self.db.add(proposal)
        task.autopilot_state = "suggested"
        return proposal

    # ------------------------------------------------------------------
    # Tick step 4: execute approved autopilot_dispatch proposals
    # ------------------------------------------------------------------
    async def _execute_approved(self, counters: dict[str, Any]) -> int:
        """Dispatch every approved autopilot_dispatch proposal (approved →
        executed, per the documented state machine). A human already said yes,
        so these bypass the daily auto-dispatch budget."""
        result = await self.db.execute(
            select(Proposal).where(
                Proposal.type == "autopilot_dispatch",
                Proposal.state == "approved",
            )
        )
        executed = 0
        for proposal in result.scalars().all():
            content = proposal.content or {}
            task_id = content.get("task_id")
            task = await self.db.get(Task, task_id) if task_id else None
            if task is None:
                proposal.state = "failed"
                proposal.execution_result = {"error": f"Task not found: {task_id}"}
                await self.db.flush()
                continue
            await self._dispatch(task, content.get("repository"), proposal=proposal)
            counters["dispatched"] += 1
            executed += 1
        return executed

    # ------------------------------------------------------------------
    # Tick step 5: Webex nudges for stale in-progress tasks
    # ------------------------------------------------------------------
    async def _nudge_stale(self, state: dict[str, Any], counters: dict[str, Any]) -> None:
        if not settings.webex_bot_token:
            return  # graceful skip: nudging needs the bot
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(days=int(state["stale_task_days"]))
        guard = now - timedelta(hours=24)
        result = await self.db.execute(
            select(Task, TeamMember)
            .join(TeamMember, Task.assignee_id == TeamMember.id)
            .where(
                Task.status == "in_progress",
                Task.updated_at < threshold,
                or_(
                    Task.autopilot_nudged_at.is_(None),
                    Task.autopilot_nudged_at < guard,
                ),
            )
        )
        client = WebexClient(settings.webex_bot_token, base_url=settings.webex_api_base)
        for task, member in result.all():
            # Nudges are per-task chatter: never in test mode (they'd all land
            # on the tester), never to synthesized addresses (they'd fail and
            # be retried on every tick).
            if settings.webex_redirect_to or not is_reachable(member.email):
                continue
            days = (now - task.updated_at).days if task.updated_at else 0
            text = (
                f"👋 Quick nudge from the delivery autopilot: '{task.title}' has "
                f"had no update for {days} days. Still on track, or is something "
                "blocking you?"
            )
            try:
                await client.send_to_email(member.email, text)
            except Exception as exc:  # noqa: BLE001 — a DM failure must not kill the tick
                logger.warning("Autopilot nudge failed for task %s: %s", task.id, exc)
                continue
            # Stamp only after a successful send. Note: this UPDATE resets
            # updated_at (onupdate) — accepted; the 24h guard is the dedupe.
            task.autopilot_nudged_at = now
            counters["nudged"] += 1
        await self.db.flush()

    # ------------------------------------------------------------------
    # Tick step 6: escalate long-stale blockers to the portfolio queue
    # ------------------------------------------------------------------
    async def _escalate_blockers(
        self, state: dict[str, Any], counters: dict[str, Any]
    ) -> None:
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(days=int(state["blocker_escalation_days"]))
        result = await self.db.execute(
            select(Task).where(
                Task.status == "in_progress", Task.updated_at < threshold
            )
        )
        for task in result.scalars().all():
            # Dedupe: one open (pending/approved) escalation per task.
            dup = await self.db.execute(
                select(Proposal.id)
                .where(
                    Proposal.type == "stale_blocker",
                    Proposal.state.in_(("pending_review", "approved")),
                    Proposal.content["task_id"].astext == task.id,
                )
                .limit(1)
            )
            if dup.first() is not None:
                continue
            stream_id = None
            if task.sprint_id:
                sprint = await self.db.get(Sprint, task.sprint_id)
                stream_id = sprint.stream_id if sprint else None
            days = (now - task.updated_at).days if task.updated_at else 0
            self.db.add(
                Proposal(
                    type="stale_blocker",
                    state="pending_review",
                    # escalated + stream/portfolio scope auto-surfaces this in
                    # the portfolio escalations list (see api/v1/portfolio.py).
                    scope="stream" if stream_id else "portfolio",
                    escalated=True,
                    stream_id=stream_id,
                    content={
                        "task_id": task.id,
                        "task_title": task.title,
                        "days_stuck": days,
                    },
                    reasoning=(
                        f"'{task.title}' has been in progress for {days} days "
                        "with no update — likely blocked."
                    ),
                    confidence="high",
                    created_by_agent="autopilot",
                )
            )
            counters["escalated"] += 1
        await self.db.flush()

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------
    async def tick(self) -> dict[str, Any]:
        """One autopilot pass. The caller (Celery task) owns the final commit;
        dispatches commit eagerly mid-tick (mirroring the dispatch endpoint)."""
        state = await self.get_state()
        mode = state["mode"]
        counters = await self.get_counters()
        repository = state.get("repository") or None
        daily_limit = int(state.get("daily_limit") or 0)

        # (1)-(3) Triage fresh candidates. Budget is pre-checked in auto mode so
        # an exhausted day doesn't burn Groq calls on triage we can't act on.
        budget_remaining = max(0, daily_limit - int(counters.get("dispatched", 0)))
        if mode == "auto" and budget_remaining <= 0:
            logger.info("Autopilot daily budget exhausted; skipping triage")
        else:
            for task in await self._candidates():
                if mode == "auto" and budget_remaining <= 0:
                    break
                try:
                    triage = await self._triage(task)
                except (GroqError, ValueError) as exc:
                    # Groq down/misbehaving — leave the task untouched (state
                    # stays NULL) and let the next tick retry the whole batch.
                    logger.warning("Autopilot triage failed for %s: %s", task.id, exc)
                    break
                counters["triaged"] += 1
                suitable = bool(triage.get("suitable")) and triage.get(
                    "confidence"
                ) in ("medium", "high")
                if not suitable:
                    task.autopilot_state = "skipped"
                    counters["skipped"] += 1
                    skips = counters["skips"]
                    skips.append(
                        {
                            "task_id": task.id,
                            "title": task.title,
                            "reason": triage.get("reason")
                            or f"confidence={triage.get('confidence')}",
                        }
                    )
                    counters["skips"] = skips[-_MAX_SKIPS:]
                    continue
                if mode == "auto":
                    await self._dispatch(task, repository)
                    counters["dispatched"] += 1
                    budget_remaining -= 1
                else:  # propose
                    self._propose(task, triage, repository)
                    counters["suggested"] += 1

        # (4) Human-approved dispatches execute on the next tick, always.
        await self._execute_approved(counters)

        # (5) Nudge stale in-progress work over Telegram.
        await self._nudge_stale(state, counters)

        # (6) Escalate long-stale blockers into the executive decision queue.
        await self._escalate_blockers(state, counters)

        await self._save_setting(_counters_key(date.today()), counters)
        return {
            "mode": mode,
            "budget_remaining": max(
                0, daily_limit - int(counters.get("dispatched", 0))
            ),
            "triaged": counters["triaged"],
            "dispatched": counters["dispatched"],
            "suggested": counters["suggested"],
            "skipped": counters["skipped"],
            "nudged": counters["nudged"],
            "escalated": counters["escalated"],
        }
