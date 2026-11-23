"""Coding-agent endpoints: dispatch, run status, explicit merge, revise.

Merge is intentionally a SEPARATE endpoint from proposal approval — approval is
the human review of the PR; merge is the explicit, deliberate act that lands it
(criteria A & D). Nothing here merges automatically.
"""

import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.integrations.coding_agent import CodingAgentError
from app.integrations.task_provider import ReadOnlyProviderError
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.services.coding_run_service import CodingRunService
from app.services.repo_service import RepoService
from app.services.workforce_stats_service import WorkforceStatsService
from app.tasks.coding_tasks import (
    dispatch_coding_agent,
    merge_coding_run,
    revise_coding_run,
)

router = APIRouter(prefix="/coding", tags=["coding"])
logger = logging.getLogger(__name__)


class DispatchRequest(BaseModel):
    task_id: str
    # Target repo (name or URL) for this run; None falls back to the configured
    # default repo on the adapter.
    repository: str | None = None


class ReviseRequest(BaseModel):
    comment: str = Field(..., min_length=1)


class RepoCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    seed: bool = True
    # Re-push the sample app even if the default branch already has content.
    force_seed: bool = False


class AgentRunResponse(BaseModel):
    id: str
    task_id: str
    proposal_id: str | None
    status: str
    branch_name: str | None
    repository: str | None
    pr_id: str | None
    pr_url: str | None
    iteration: int
    cost_estimate: float
    error: str | None
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def of(cls, run: AgentRun) -> "AgentRunResponse":
        return cls(
            id=run.id,
            task_id=run.task_id,
            proposal_id=run.proposal_id,
            status=run.status,
            branch_name=run.branch_name,
            repository=run.repository,
            pr_id=run.pr_id,
            pr_url=run.pr_url,
            iteration=run.iteration,
            cost_estimate=run.cost_estimate,
            error=run.error,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


async def _load(db: AsyncSession, run_id: str) -> AgentRun:
    res = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = res.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.post("/dispatch", status_code=status.HTTP_202_ACCEPTED)
async def dispatch(req: DispatchRequest, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Hand a task to the coding agent (called when the form picks '🤖 AI Agent')."""
    svc = CodingRunService(db)
    existing = await svc.get_run_for_task(req.task_id)
    # An in-flight run is returned as-is; a terminal-failed one can be retried.
    if existing is not None and existing.status not in ("failed", "ci_failed"):
        return {"run_id": existing.id, "status": existing.status}
    run = await svc.create_run(req.task_id, repository=req.repository)
    run.status = "dispatched"
    run.error = None
    await db.commit()
    dispatch_coding_agent.delay(run.id)
    return {"run_id": run.id, "status": "dispatched"}


@router.get("/repositories")
async def list_repositories(db: AsyncSession = Depends(get_db)) -> dict[str, list[str]]:
    """Repos to offer in the dispatch form: configured known repos + any repo a
    prior run targeted + whatever exists in ADO. The form can also submit a
    brand-new value."""
    known = [r.strip() for r in settings.coding_agent_known_repos.split(",") if r.strip()]
    res = await db.execute(
        select(AgentRun.repository).where(AgentRun.repository.is_not(None)).distinct()
    )
    used = [r for (r,) in res.all() if r]
    # Live ADO repos are best-effort: no creds / unreachable => skip silently.
    try:
        remote = await RepoService().list_ado_repositories()
    except Exception:  # noqa: BLE001
        remote = []
    repositories: list[str] = []
    for r in [*known, *used, *remote]:
        if r and r not in repositories:
            repositories.append(r)
    return {"repositories": repositories}


@router.post("/repositories", status_code=status.HTTP_201_CREATED)
async def create_repository(req: RepoCreateRequest) -> dict[str, Any]:
    """Create an ADO repo (idempotent) and seed empty ones with the sample app."""
    try:
        return await RepoService().create_and_seed(
            req.name, seed=req.seed, force_seed=req.force_seed
        )
    except ReadOnlyProviderError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except CodingAgentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except httpx.HTTPError as exc:
        # Transport-level failure (ADO unreachable / unconfigured) — a clean
        # 502 beats an opaque 500 from the generic handler.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps unreachable: {exc}",
        )


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Aggregate AI-workforce numbers (Command Center stat tiles)."""
    return await WorkforceStatsService(db).get_stats()


@router.get("/runs", response_model=list[AgentRunResponse])
async def list_runs(
    status_filter: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[AgentRunResponse]:
    query = select(AgentRun).order_by(AgentRun.created_at.desc())
    if status_filter:
        query = query.where(AgentRun.status == status_filter)
    res = await db.execute(query)
    return [AgentRunResponse.of(r) for r in res.scalars().all()]


@router.get("/runs/{task_id}", response_model=AgentRunResponse | None)
async def get_run_for_task(
    task_id: str, db: AsyncSession = Depends(get_db)
) -> AgentRunResponse | None:
    svc = CodingRunService(db)
    run = await svc.get_run_for_task(task_id)
    return AgentRunResponse.of(run) if run else None


@router.get("/runs/{run_id}/events")
async def list_run_events(
    run_id: str, after_seq: int = 0, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Progress events for a run, incrementally: pass the last seen ``seq`` as
    ``after_seq`` and only newer events come back. ``run_status`` rides along
    so pollers can stop once the run reaches a terminal state."""
    run = await _load(db, run_id)
    res = await db.execute(
        select(AgentRunEvent)
        .where(AgentRunEvent.run_id == run_id, AgentRunEvent.seq > after_seq)
        .order_by(AgentRunEvent.seq)
    )
    events = res.scalars().all()
    return {
        "events": [
            {
                "seq": e.seq,
                "kind": e.kind,
                "message": e.message,
                "payload": e.payload,
                "created_at": e.created_at,
            }
            for e in events
        ],
        "run_status": run.status,
    }


@router.post("/runs/{run_id}/merge", status_code=status.HTTP_202_ACCEPTED)
async def merge_run(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    run = await _load(db, run_id)
    if run.status != "pr_open":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Run is not mergeable in status '{run.status}'.",
        )
    merge_coding_run.delay(run_id)
    return {"run_id": run_id, "status": "merging"}


@router.post("/runs/{run_id}/revise", status_code=status.HTTP_202_ACCEPTED)
async def revise_run(
    run_id: str, req: ReviseRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    await _load(db, run_id)  # 404 check only; Celery task re-loads it
    revise_coding_run.delay(run_id, req.comment)
    return {"run_id": run_id, "status": "revising"}
