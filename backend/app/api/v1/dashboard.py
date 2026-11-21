"""Dashboard endpoints for the PM and executive altitudes.

These mostly expose aggregates the system already computed and discarded —
release readiness, team capacity, stored sprint velocity, sync state. See
``DashboardService`` for the query rules.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.llm_call import LLMCall
from app.schemas.dashboard import (
    CostByStreamResponse,
    DeliverySummary,
    FlowMetrics,
    IntegrationHealth,
    InvestmentProfileResponse,
    InvestmentTrendResponse,
    RoadmapResponse,
    SprintDashboard,
    TeamLoadRow,
    VelocityResponse,
    WorkforceTrend,
)
from app.services.confidence_service import ConfidenceService
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# NOTE: /sprint/active must be declared before /sprint/{sprint_id}, otherwise
# FastAPI matches "active" as a path parameter.
@router.get("/sprint/active", response_model=SprintDashboard | None)
async def get_active_sprint_dashboard(
    db: AsyncSession = Depends(get_db),
) -> SprintDashboard | None:
    """The active sprint's dashboard, or null when no sprint is active.

    Null rather than 404: "no active sprint" is an ordinary state for a team
    between sprints, and the UI renders an empty state for it.
    """
    return await DashboardService(db).sprint_dashboard(None)


@router.get("/sprint/{sprint_id}", response_model=SprintDashboard)
async def get_sprint_dashboard(
    sprint_id: str, db: AsyncSession = Depends(get_db)
) -> SprintDashboard:
    """Dashboard for a specific sprint."""
    result = await DashboardService(db).sprint_dashboard(sprint_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )
    return result


@router.post("/sprint/{sprint_id}/snapshot")
async def snapshot_sprint(sprint_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Take today's confidence snapshot now instead of waiting for the beat job.

    The daily job runs at 23:30, so without this a PM who moved cards this
    morning sees no new burndown point until tomorrow. Idempotent: the
    (sprint_id, snapshot_date) unique constraint means repeat calls upsert.
    """
    snap = await ConfidenceService(db).snapshot_sprint(sprint_id)
    return {"snapshotted": snap is not None}


@router.get("/team-load", response_model=list[TeamLoadRow])
async def get_team_load(
    sprint_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[TeamLoadRow]:
    """Assigned hours against capacity per member. Defaults to the active sprint."""
    return await DashboardService(db).team_load(sprint_id)


@router.get("/velocity", response_model=VelocityResponse)
async def get_velocity(
    stream_id: str | None = None,
    limit: int = Query(8, ge=1, le=40),
    db: AsyncSession = Depends(get_db),
) -> VelocityResponse:
    """Committed vs completed points per sprint, plus a mean/stdev forecast band."""
    return await DashboardService(db).velocity_history(stream_id, limit)


@router.get("/delivery-summary", response_model=DeliverySummary)
async def get_delivery_summary(
    days: int = Query(14, ge=1, le=365),
    stream_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> DeliverySummary:
    """Completions per day. Approximate — see the schema docstring."""
    return await DashboardService(db).delivery_summary(days, stream_id)


@router.get("/integrations", response_model=IntegrationHealth)
async def get_integration_health(
    db: AsyncSession = Depends(get_db),
) -> IntegrationHealth:
    """Freshness of sync, briefs and confidence snapshots."""
    return await DashboardService(db).integration_health()


@router.get("/cost-by-stream", response_model=CostByStreamResponse)
async def get_cost_by_stream(
    days: int | None = Query(None, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> CostByStreamResponse:
    """Agent spend attributed through task → sprint → stream."""
    return await DashboardService(db).cost_by_stream(days)


@router.get("/investment", response_model=InvestmentProfileResponse)
async def get_investment_profile(
    sprint_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> InvestmentProfileResponse:
    """Story points by work item type, done vs open. Defaults to the active sprint."""
    return await DashboardService(db).investment_profile(sprint_id)


@router.get("/investment/trend", response_model=InvestmentTrendResponse)
async def get_investment_trend(
    sprints: int = Query(4, ge=2, le=8),
    db: AsyncSession = Depends(get_db),
) -> InvestmentTrendResponse:
    """Per-sprint story-point splits by work item type: N-1 closed sprints
    plus the active one, oldest first."""
    return await DashboardService(db).investment_trend(sprints)


@router.get("/workforce-trend", response_model=WorkforceTrend)
async def get_workforce_trend(
    days: int = Query(84, ge=7, le=365),
    bucket_days: int = Query(7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
) -> WorkforceTrend:
    """Agent output over time, bucketed."""
    return await DashboardService(db).workforce_trend(days, bucket_days)


@router.get("/flow", response_model=FlowMetrics)
async def get_flow(
    days: int = Query(30, ge=1, le=365),
    stream_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> FlowMetrics:
    """Throughput, cycle time, lead time and aging WIP from the event log."""
    return await DashboardService(db).flow_metrics(days, stream_id)


@router.get("/llm-usage")
async def get_llm_usage(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """LLM ledger totals for the Command Center tile."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # ponytail: fetch window rows, aggregate in Python — SQL GROUP BY if the
    # ledger ever grows past a few hundred rows per week.
    rows = (
        (await db.execute(select(LLMCall).where(LLMCall.created_at >= cutoff)))
        .scalars()
        .all()
    )
    return summarize_usage(rows, days)


def summarize_usage(rows: Sequence[Any], days: int) -> dict:
    """Pure aggregate over ledger rows, keeping only the last ``days`` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    calls = 0
    total_tokens = 0
    latency_sum = 0
    by_feature: dict[str, dict[str, int]] = {}
    for r in rows:
        if r.created_at < cutoff:  # created_at is timezone-aware (DateTime(timezone=True))
            continue
        calls += 1
        total_tokens += r.total_tokens
        latency_sum += r.latency_ms
        f = by_feature.setdefault(r.feature, {"calls": 0, "total_tokens": 0})
        f["calls"] += 1
        f["total_tokens"] += r.total_tokens
    return {
        "days": days,
        "calls": calls,
        "total_tokens": total_tokens,
        "avg_latency_ms": round(latency_sum / calls) if calls else 0,
        "by_feature": by_feature,
    }


@router.get("/roadmap", response_model=RoadmapResponse)
async def get_roadmap(
    stream_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> RoadmapResponse:
    """Feature spans derived from their tasks' sprint dates."""
    return await DashboardService(db).roadmap(stream_id)
