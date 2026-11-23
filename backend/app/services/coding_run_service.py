"""Orchestration + guardrails for the coding-agent PR loop.

Single choke point for every AgentRun transition. The Celery tasks are thin
glue around these methods; all guardrails (dir allowlist, max diff, require
tests, iteration cap, cost ceiling, CI poll bound) live here and raise
``GuardrailViolation`` so the caller can mark the run ``failed`` with a reason.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.coding_agent import (
    AgentRunResult,
    AgentSpec,
    CodingAgentError,
    GuardrailViolation,
    Suitability,
)
from app.integrations.coding_agent_factory import get_coding_agent
from app.models.agent_run import AgentRun
from app.models.proposal import Proposal
from app.models.task import Task
from app.services.run_event_service import RunEventRecorder

logger = logging.getLogger(__name__)


def _diff_paths(diff: str) -> list[str]:
    """Repo-relative paths a diff touches (from `+++ b/<path>` headers)."""
    return [
        ln[len("+++ b/"):].strip()
        for ln in diff.splitlines()
        if ln.startswith("+++ b/")
    ]


class CodingRunService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.agent = get_coding_agent()

    # ---- loading -------------------------------------------------------
    async def get_run(self, run_id: str) -> AgentRun:
        res = await self.db.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = res.scalar_one_or_none()
        if run is None:
            raise CodingAgentError(f"AgentRun not found: {run_id}")
        return run

    async def get_run_for_task(self, task_id: str) -> AgentRun | None:
        res = await self.db.execute(select(AgentRun).where(AgentRun.task_id == task_id))
        return res.scalar_one_or_none()

    async def get_task(self, run: AgentRun) -> Task:
        res = await self.db.execute(select(Task).where(Task.id == run.task_id))
        task = res.scalar_one_or_none()
        if task is None:
            raise CodingAgentError(f"Task not found for run {run.id}")
        return task

    @staticmethod
    def _task_dict(task: Task) -> dict:
        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "technology": task.technology,
            "story_points": task.story_points,
        }

    async def create_run(self, task_id: str, repository: str | None = None) -> AgentRun:
        existing = await self.get_run_for_task(task_id)
        if existing is not None:
            # A re-dispatch (e.g. retry of a failed run) may target a new repo.
            if repository is not None:
                existing.repository = repository
                await self.db.flush()
            return existing
        run = AgentRun(task_id=task_id, status="dispatched", repository=repository)
        self.db.add(run)
        await self.db.flush()
        return run

    async def fail(self, run: AgentRun, reason: str) -> None:
        run.status = "failed"
        run.error = reason[:2000]
        await self.db.flush()
        await self._safe_emit(RunEventRecorder(run.id), "error", reason[:300])

    # ---- run events (best-effort timeline; never block the run) ---------
    def _attach_recorder(self, run: AgentRun) -> RunEventRecorder:
        """Build a recorder for this run and point the agent's per-run event
        sink at it (only real adapters expose one; the mock has none)."""
        recorder = RunEventRecorder(run.id)
        if hasattr(self.agent, "event_sink"):
            self.agent.event_sink = recorder.emit
        return recorder

    @staticmethod
    async def _safe_emit(
        recorder: RunEventRecorder, kind: str, message: str, payload: dict | None = None
    ) -> None:
        """Emit a run event, swallowing any failure — telemetry must never
        fail a run (the recorder itself never raises; this is a backstop)."""
        try:
            await recorder.emit(kind, message, payload)
        except Exception:  # noqa: BLE001
            logger.debug("run-event emit failed for kind=%s", kind, exc_info=True)

    # ---- guardrails ----------------------------------------------------
    def _enforce_diff_guardrails(self, result: AgentRunResult) -> None:
        if result.empty or result.diff_lines == 0:
            raise GuardrailViolation("Agent produced an empty diff.")
        if result.diff_lines > settings.coding_agent_max_diff_lines:
            raise GuardrailViolation(
                f"Diff too large: {result.diff_lines} > {settings.coding_agent_max_diff_lines} lines."
            )
        paths = _diff_paths(result.diff)
        allowed = [d.strip() for d in settings.coding_agent_allowed_dirs.split(",") if d.strip()]
        if allowed:
            bad = [p for p in paths if not any(p.startswith(a) for a in allowed)]
            if bad:
                raise GuardrailViolation(f"Diff touches disallowed paths: {bad}")
        if settings.require_tests and not any("test" in p.lower() for p in paths):
            raise GuardrailViolation("require_tests is on but the diff touches no test files.")

    # ---- steps ---------------------------------------------------------
    async def do_groom(self, run: AgentRun, task: Task) -> AgentSpec:
        spec = await self.agent.groom(self._task_dict(task))
        run.spec = spec.model_dump()
        run.status = "groomed"
        await self.db.flush()
        await self._safe_emit(
            RunEventRecorder(run.id), "lifecycle", f"Spec groomed: {spec.summary[:200]}"
        )
        return spec

    async def do_triage(self, run: AgentRun, task: Task, spec: AgentSpec) -> Suitability:
        suit = await self.agent.triage(self._task_dict(task), spec)
        recorder = RunEventRecorder(run.id)
        if not suit.suitable:
            run.status = "skipped_human_only"
            run.error = f"Triage: {suit.reason}"
            await self.db.flush()
            await self._safe_emit(
                recorder, "lifecycle", f"Triage: skipped — {suit.reason[:200]}"
            )
        else:
            await self._safe_emit(recorder, "lifecycle", "Triage: suitable")
        return suit

    async def do_run(self, run: AgentRun, spec: AgentSpec) -> AgentRunResult:
        recorder = self._attach_recorder(run)
        run.status = "running"
        # Publish the transition before the (potentially long) agent run — the
        # task's transaction otherwise only commits at the end, so pollers
        # would see "dispatched" the whole time Claude is coding.
        await self.db.commit()
        branch = f"agent/{run.task_id[:8]}"
        result = await self.agent.run(spec, branch, repo=run.repository)
        self._enforce_diff_guardrails(result)
        await self._safe_emit(
            recorder, "lifecycle", f"Guardrails passed ({result.diff_lines} changed lines)"
        )
        run.cost_estimate += result.cost
        if run.cost_estimate > settings.coding_agent_cost_ceiling:
            raise GuardrailViolation(
                f"Cost ceiling exceeded: {run.cost_estimate} > {settings.coding_agent_cost_ceiling}."
            )
        run.branch_name = result.branch_name
        run.pr_id = result.pr_id
        run.pr_url = result.pr_url
        run.result = result.model_dump()
        run.ci_poll_count = 0
        run.status = "ci_pending"
        await self.db.flush()
        await self._safe_emit(recorder, "lifecycle", "CI pending")
        return result

    async def do_poll_ci(self, run: AgentRun) -> str:
        """Returns 'success' | 'failed' | 'running'. Sets run.status accordingly."""
        status = await self.agent.get_ci_status(
            run.pr_id or "", run.branch_name or "", run.ci_poll_count, repo=run.repository
        )
        if status.state == "success":
            await self.register_proposal(run)
            run.status = "pr_open"
            await self.db.flush()
            await self._safe_emit(
                RunEventRecorder(run.id),
                "lifecycle",
                "PR ready for review",
                {"pr_url": run.pr_url},
            )
            return "success"
        if status.state in ("failed", "error"):
            run.status = "ci_failed"
            run.error = f"CI {status.state}"
            await self.db.flush()
            return "failed"
        # pending / running / not_found → keep polling, bounded.
        run.ci_poll_count += 1
        if run.ci_poll_count >= settings.coding_agent_max_ci_polls:
            run.status = "ci_failed"
            run.error = "CI did not complete within the poll budget."
            await self.db.flush()
            return "failed"
        await self.db.flush()
        return "running"

    async def register_proposal(self, run: AgentRun) -> Proposal:
        """Create/refresh the coding_pr Proposal — ONLY called on green CI (idempotent)."""
        result = run.result or {}
        content = {
            "run_id": run.id,
            "task_id": run.task_id,
            "pr_id": run.pr_id,
            "pr_url": run.pr_url,
            "branch": run.branch_name,
            "diff_summary": result.get("summary"),
            "diff": result.get("diff"),
            "diff_lines": result.get("diff_lines"),
            "spec": run.spec,
        }
        reasoning = (run.spec or {}).get("summary") or "Coding-agent pull request."
        if run.proposal_id:
            res = await self.db.execute(select(Proposal).where(Proposal.id == run.proposal_id))
            proposal = res.scalar_one_or_none()
            if proposal is not None:
                proposal.content = content
                proposal.reasoning = reasoning
                proposal.state = "pending_review"
                await self.db.flush()
                return proposal
        proposal = Proposal(
            type="coding_pr",
            state="pending_review",
            content=content,
            reasoning=reasoning,
            confidence="medium",
            created_by_agent="coding-agent",
        )
        self.db.add(proposal)
        await self.db.flush()
        run.proposal_id = proposal.id
        await self.db.flush()
        return proposal

    async def do_revise(self, run: AgentRun, comment: str) -> AgentRunResult:
        # Bound the loop: both an iteration cap AND a cost ceiling (criterion C).
        if run.iteration >= settings.coding_agent_max_iterations:
            raise GuardrailViolation(
                f"Iteration cap reached ({settings.coding_agent_max_iterations})."
            )
        if run.cost_estimate >= settings.coding_agent_cost_ceiling:
            raise GuardrailViolation("Cost ceiling reached before revise.")
        recorder = self._attach_recorder(run)
        spec = AgentSpec(**(run.spec or {}))
        prev = AgentRunResult(**(run.result or {"branch_name": run.branch_name or ""}))
        run.status = "running"
        # Publish before the long revision session (see do_run).
        await self.db.commit()
        result = await self.agent.revise(spec, prev, comment, repo=run.repository)
        self._enforce_diff_guardrails(result)
        await self._safe_emit(
            recorder, "lifecycle", f"Guardrails passed ({result.diff_lines} changed lines)"
        )
        run.cost_estimate += result.cost
        run.iteration += 1
        run.last_review_comment = comment
        run.result = result.model_dump()
        run.ci_poll_count = 0
        run.status = "ci_pending"
        await self.db.flush()
        await self._safe_emit(recorder, "lifecycle", "CI pending")
        return result

    async def do_merge(self, run: AgentRun) -> None:
        if run.status != "pr_open":
            raise CodingAgentError(f"Cannot merge run in status {run.status}.")
        # Require prior human approval of the linked proposal (criteria A & D).
        if run.proposal_id:
            res = await self.db.execute(select(Proposal).where(Proposal.id == run.proposal_id))
            proposal = res.scalar_one_or_none()
            if proposal is None or proposal.state != "approved":
                raise CodingAgentError("Proposal must be approved before merge.")
        merge = await self.agent.merge(run.pr_id or "", repo=run.repository)
        if not merge.merged:
            raise CodingAgentError(f"Merge failed: {merge.reason}")
        run.status = "merged"
        await self.db.flush()
        await self.on_merge(run)

    async def on_merge(self, run: AgentRun) -> None:
        """Close the flywheel: linked task → done, recompute sprint confidence."""
        task = await self.get_task(run)
        previous_status = task.status
        task.status = "done"
        if previous_status != "done":
            # The agent closing a task is a real transition and belongs in the
            # flow log, otherwise agent-delivered work is invisible to cycle time.
            from app.services.task_service import TaskService

            TaskService(self.db).record_status_change(
                task, previous_status, "done", source="agent"
            )
        await self.db.flush()
        if task.sprint_id:
            from app.services.confidence_service import ConfidenceService

            await ConfidenceService(self.db).snapshot_sprint(task.sprint_id)
