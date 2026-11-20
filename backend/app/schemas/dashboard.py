"""Schemas for the PM and executive dashboards.

Almost nothing here is a new measurement — these are shapes for values the
system already computes (``DatabaseTools`` aggregates, ``Sprint.velocity``,
``SyncState``) but has never exposed over HTTP.

Nullability is load-bearing throughout. ``velocity`` is None until a sprint
completes, ``success_rate`` is None until a run finishes, ``avg_confidence``
is None with no snapshots. Callers must render those as "no data", never 0 —
the frontend contract is that a null breaks a line rather than dropping it to
the baseline.
"""

from datetime import date, datetime

from pydantic import BaseModel

# Reused, not redefined — the sprint dashboard and the stream view must not be
# able to drift on what the four confidence factors are.
from app.schemas.portfolio import ConfidenceFactors

__all__ = [
    "AgingItem",
    "ConfidenceFactors",
    "CostByStream",
    "CostByStreamResponse",
    "CycleTimeItem",
    "DeliverySummary",
    "FlowBucket",
    "FlowMetrics",
    "IntegrationHealth",
    "InvestmentRow",
    "InvestmentProfileResponse",
    "InvestmentTrendSprint",
    "InvestmentTrendResponse",
    "ReleaseReadiness",
    "RoadmapItem",
    "RoadmapResponse",
    "SprintDashboard",
    "StaleTask",
    "StatusCount",
    "TeamLoadRow",
    "ThroughputPoint",
    "VelocityPoint",
    "VelocityResponse",
    "WorkforcePoint",
    "WorkforceTrend",
]


class ReleaseReadiness(BaseModel):
    """Shape of ``DatabaseTools.get_release_readiness`` over HTTP."""

    sprint_id: str | None = None
    sprint_name: str | None = None
    completion_rate: float
    tasks_done: int
    tasks_remaining: int
    tasks_stale: int
    risk_level: str  # low | medium | high
    recommendation: str


class StaleTask(BaseModel):
    """An item sitting in progress without a change.

    Named "stale", not "blocked". The signal is ``updated_at`` older than a
    threshold, which is not the same thing as being blocked — and the autopilot
    nudge writes ``updated_at``, so it is a noisy proxy. The UI must use the
    same honest wording.
    """

    id: str
    title: str
    assignee_name: str | None = None
    story_points: int | None = None
    days_idle: int


class TeamLoadRow(BaseModel):
    """Per-member load for a sprint.

    Per-person data is a PM-altitude concern; the executive views aggregate by
    stream and never surface individuals.
    """

    id: str
    name: str
    role: str | None = None
    capacity_hours: int
    absent_hours: int = 0
    assigned_hours: int
    open_points: int
    in_progress: int
    done: int
    stale: int


class SprintDashboard(BaseModel):
    """Everything the PM sprint view needs, in one round trip."""

    sprint_id: str
    name: str
    goal: str | None = None
    start_date: date
    end_date: date
    status: str
    stream_id: str | None = None
    stream_name: str | None = None
    capacity_hours: int | None = None

    total_tasks: int
    completed_tasks: int
    total_story_points: int
    completed_story_points: int
    progress_percentage: float
    days_remaining: int | None = None

    velocity: int | None = None          # None until the sprint completes
    confidence: float | None = None      # None with no snapshot
    factors: ConfidenceFactors | None = None

    readiness: ReleaseReadiness
    stale_tasks: list[StaleTask]
    team_load: list[TeamLoadRow]


class VelocityPoint(BaseModel):
    sprint_id: str
    name: str
    start_date: date
    status: str
    committed_points: int
    completed_points: int
    velocity: int | None = None   # the stored value; None while in flight
    in_flight: bool = False


class VelocityResponse(BaseModel):
    points: list[VelocityPoint]
    rolling_mean: float | None = None
    sigma: float | None = None
    sprints_counted: int = 0
    unit: str = "pts"  # "items" when the team does not estimate in points


class ThroughputPoint(BaseModel):
    date: date
    completed: int
    points: int


class DeliverySummary(BaseModel):
    """Throughput over a window.

    Approximate by construction: completion date is inferred from
    ``Task.updated_at``, which any later edit moves and which provider sync
    rewrites in bulk. Labelled "approx." in the UI until the task-status event
    log replaces it.
    """

    days: int
    completed: int
    points: int
    daily: list[ThroughputPoint]
    approximate: bool = True


class IntegrationHealth(BaseModel):
    """Can these numbers be trusted right now?"""

    provider: str | None = None
    last_sync_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    inserted_count: int | None = None
    updated_count: int | None = None
    last_brief_date: date | None = None
    last_snapshot_date: date | None = None
    snapshot_lag_days: int | None = None


class CostByStream(BaseModel):
    stream_id: str | None = None
    stream_name: str
    cost: float
    runs: int
    merged: int


class CostByStreamResponse(BaseModel):
    streams: list[CostByStream]
    total_cost: float
    unattributed_cost: float


class InvestmentRow(BaseModel):
    """Story points for one work item type: where the sprint's effort goes."""

    work_item_type: str
    total_points: float
    done_points: float
    open_points: float
    item_count: int


class InvestmentProfileResponse(BaseModel):
    rows: list[InvestmentRow]
    sprint_id: str | None
    sprint_name: str | None
    total_points: float


class InvestmentTrendSprint(BaseModel):
    """One sprint's investment split, inside the trend window."""

    sprint_id: str
    name: str
    end_date: date
    rows: list[InvestmentRow]


class InvestmentTrendResponse(BaseModel):
    """Per-sprint type splits, oldest → newest, active sprint last."""

    sprints: list[InvestmentTrendSprint]


class WorkforcePoint(BaseModel):
    """One bucket of agent output, for the executive ROI trend."""

    date: date
    dispatched: int
    merged: int
    failed: int
    cost: float


class WorkforceTrend(BaseModel):
    points: list[WorkforcePoint]
    bucket_days: int


# ---- flow metrics (task status event log) ----


class FlowBucket(BaseModel):
    date: date
    count: int


class CycleTimeItem(BaseModel):
    task_id: str
    title: str
    days: float
    completed_on: date


class StatusCount(BaseModel):
    date: date
    backlog: int = 0
    todo: int = 0
    in_progress: int = 0
    review: int = 0
    done: int = 0


class AgingItem(BaseModel):
    task_id: str
    title: str
    status: str
    days_in_status: float
    assignee_name: str | None = None


class FlowMetrics(BaseModel):
    """Derived from ``task_status_events`` — the only honest source for these.

    Snapshots cannot produce cycle time: they record a daily position, not a
    transition. ``history_starts`` is the earliest non-backfilled event, so the
    UI can say how far back the numbers actually go.
    """

    days: int
    history_starts: date | None = None
    backfilled_only: bool = False

    throughput: list[FlowBucket]
    cycle_time_p50: float | None = None
    cycle_time_p85: float | None = None
    lead_time_p50: float | None = None
    lead_time_p85: float | None = None
    items: list[CycleTimeItem]
    cfd: list[StatusCount]
    aging_wip: list[AgingItem]


# ---- roadmap ----


class RoadmapItem(BaseModel):
    """A feature's span, derived from the sprint dates of the tasks inside it.

    There is no ``features.target_date`` column, so a feature with no
    sprint-assigned tasks has no span and is returned with null dates rather
    than an invented one.
    """

    id: str
    name: str
    stream_id: str | None = None
    stream_name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str
    progress: float
    total_tasks: int
    completed_tasks: int
    total_points: int
    completed_points: int


class RoadmapResponse(BaseModel):
    items: list[RoadmapItem]
    window_start: date | None = None
    window_end: date | None = None
    today: date
