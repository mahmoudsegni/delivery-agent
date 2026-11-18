"""Aggregates for the PM and executive dashboards.

Two rules govern everything in this module:

1. **Aggregate in SQL.** Every relationship on every model is ``lazy="raise"``,
   so nothing here traverses a relationship — queries select explicit columns
   and join explicitly. Counting in Python is what makes
   ``TaskService.get_summary`` slow; do not repeat it.

2. **These endpoints get polled.** ``DatabaseTools.get_developer_status`` runs
   one query per member, which is fine for a single LLM tool call and
   unacceptable for a dashboard refreshing every 30s. Anything here must be a
   handful of queries regardless of team or stream count.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any, Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.brief import Brief
from app.models.confidence import ConfidenceSnapshot
from app.models.feature import Feature
from app.models.sprint import Sprint
from app.models.stream import Stream
from app.models.sync_state import SyncState
from app.models.task import Task
from app.models.task_status_event import TaskStatusEvent
from app.models.team import TeamMember
from app.schemas.dashboard import (
    AgingItem,
    ConfidenceFactors,
    CostByStream,
    CostByStreamResponse,
    CycleTimeItem,
    DeliverySummary,
    FlowBucket,
    FlowMetrics,
    IntegrationHealth,
    InvestmentProfileResponse,
    InvestmentRow,
    ReleaseReadiness,
    RoadmapItem,
    RoadmapResponse,
    SprintDashboard,
    StaleTask,
    StatusCount,
    TeamLoadRow,
    ThroughputPoint,
    VelocityPoint,
    VelocityResponse,
    WorkforcePoint,
    WorkforceTrend,
)
from app.services.agent_analysis_service import CONTAINER_TYPES
from app.services.confidence_service import ConfidenceService

# An item counts as stale after this long without a change. Matches the
# threshold DatabaseTools already uses so the dashboards, the brief and the
# confidence engine never disagree about the same task.
STALE_DAYS = 3

# Points-in-flight statuses — everything that is not finished or unstarted.
_OPEN_STATUSES = ("todo", "in_progress", "review")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(ts: datetime | None) -> int:
    if ts is None:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, (_utcnow() - ts).days)


def _investment_response(
    sprint: Sprint | None,
    raw: Iterable[tuple[str, float | None, float | None, int]],
) -> InvestmentProfileResponse:
    """Shape grouped SQL rows into the investment-split response.

    Container types (Feature/Epic/Release) are dropped here rather than in
    SQL so parent items don't double-count their children's points — same
    rule ``detect_blockers`` applies. ``story_points`` is nullable: NULL
    contributes 0 points but the item still counts.
    """
    if sprint is None:
        return InvestmentProfileResponse(
            rows=[], sprint_id=None, sprint_name=None, total_points=0.0
        )
    rows = [
        InvestmentRow(
            work_item_type=t,
            total_points=round(float(total or 0), 1),
            done_points=round(float(done or 0), 1),
            open_points=round(float((total or 0) - (done or 0)), 1),
            item_count=items or 0,
        )
        for t, total, done, items in raw
        if t not in CONTAINER_TYPES
    ]
    rows.sort(key=lambda r: r.total_points, reverse=True)
    return InvestmentProfileResponse(
        rows=rows,
        sprint_id=sprint.id,
        sprint_name=sprint.name,
        total_points=round(sum(r.total_points for r in rows), 1),
    )


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. Returns None on an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct * (len(ordered) - 1))))
    return round(ordered[k], 1)


def _last_change():
    """When an item last really changed: ADO's state-change date when synced
    (our ``updated_at`` is rewritten by every sync), else our own timestamp."""
    return func.coalesce(Task.state_changed_at, Task.updated_at)


class DashboardService:
    """Read-only aggregates. Writes belong in the owning domain service."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._confidence = ConfidenceService(db)

    # ------------------------------------------------------------------
    # sprint dashboard
    # ------------------------------------------------------------------

    async def _resolve_sprint(self, sprint_id: str | None) -> Sprint | None:
        stmt = select(Sprint)
        if sprint_id:
            stmt = stmt.where(Sprint.id == sprint_id)
        else:
            stmt = stmt.where(Sprint.status == "active").order_by(
                Sprint.start_date.desc()
            )
        result = await self.db.execute(stmt.limit(1))
        return result.scalar_one_or_none()

    async def sprint_dashboard(self, sprint_id: str | None = None) -> SprintDashboard | None:
        """Everything the PM sprint view needs, in one response.

        Composed from existing services rather than recomputing: sprint stats,
        release readiness, confidence, team load. Returns None when there is no
        such sprint (and no active one), which the route turns into a 404 or a
        null body depending on the path.
        """
        sprint = await self._resolve_sprint(sprint_id)
        if sprint is None:
            return None

        stats = await self._sprint_stats(sprint.id)
        stale = await self._stale_tasks(sprint.id)
        team_load = await self.team_load(sprint.id)
        # Takes the Sprint object, not its id.
        conf = await self._confidence.get_sprint_confidence(sprint)

        stream_name = None
        if sprint.stream_id:
            row = await self.db.execute(
                select(Stream.name).where(Stream.id == sprint.stream_id)
            )
            stream_name = row.scalar_one_or_none()

        total = stats["total"]
        done = stats["done"]
        completion = round(done / total * 100, 1) if total else 0.0

        # Same risk heuristic as DatabaseTools.get_release_readiness, kept in
        # step deliberately so the dashboard and the agent tell one story.
        in_progress = stats["in_progress"]
        if stale or completion < 50:
            risk = "high"
        elif in_progress > done or completion < 80:
            risk = "medium"
        else:
            risk = "low"

        readiness = ReleaseReadiness(
            sprint_id=sprint.id,
            sprint_name=sprint.name,
            completion_rate=completion,
            tasks_done=done,
            tasks_remaining=total - done,
            tasks_stale=len(stale),
            risk_level=risk,
            recommendation=(
                "Ready to release"
                if risk == "low"
                else "Resolve stalled items before release"
                if risk == "high"
                else "Review remaining work before release"
            ),
        )

        today = date.today()
        days_remaining = (sprint.end_date - today).days if sprint.end_date else None

        factors = None
        if conf and conf.get("factors"):
            factors = ConfidenceFactors(**conf["factors"])

        return SprintDashboard(
            sprint_id=sprint.id,
            name=sprint.name,
            goal=sprint.goal,
            start_date=sprint.start_date,
            end_date=sprint.end_date,
            status=sprint.status,
            stream_id=sprint.stream_id,
            stream_name=stream_name,
            capacity_hours=sprint.capacity_hours,
            total_tasks=total,
            completed_tasks=done,
            total_story_points=stats["total_points"],
            completed_story_points=stats["done_points"],
            progress_percentage=completion,
            days_remaining=days_remaining,
            velocity=sprint.velocity,
            confidence=conf.get("confidence_value") if conf else None,
            factors=factors,
            readiness=readiness,
            stale_tasks=stale,
            team_load=team_load,
        )

    async def _sprint_stats(self, sprint_id: str) -> dict[str, int]:
        """Task and point counts for a sprint — one grouped query."""
        result = await self.db.execute(
            select(
                func.count(Task.id).label("total"),
                func.count(Task.id).filter(Task.status == "done").label("done"),
                func.count(Task.id)
                .filter(Task.status == "in_progress")
                .label("in_progress"),
                func.count(Task.id).filter(Task.status == "review").label("review"),
                func.coalesce(func.sum(Task.story_points), 0).label("total_points"),
                func.coalesce(
                    func.sum(Task.story_points).filter(Task.status == "done"), 0
                ).label("done_points"),
            ).where(Task.sprint_id == sprint_id)
        )
        row = result.one()
        return {
            "total": row.total or 0,
            "done": row.done or 0,
            "in_progress": row.in_progress or 0,
            "review": row.review or 0,
            "total_points": int(row.total_points or 0),
            "done_points": int(row.done_points or 0),
        }

    async def _stale_tasks(self, sprint_id: str) -> list[StaleTask]:
        """In-progress items with no change for STALE_DAYS.

        Called "stale", never "blocked" — see the schema docstring.
        """
        cutoff = _utcnow() - timedelta(days=STALE_DAYS)
        last_change = _last_change()
        result = await self.db.execute(
            select(Task.id, Task.title, Task.story_points, last_change.label("changed"), TeamMember.name)
            .outerjoin(TeamMember, TeamMember.id == Task.assignee_id)
            .where(
                Task.sprint_id == sprint_id,
                Task.status == "in_progress",
                last_change < cutoff,
            )
            .order_by(last_change)
        )
        return [
            StaleTask(
                id=r.id,
                title=r.title,
                assignee_name=r.name,
                story_points=r.story_points,
                days_idle=_days_since(r.changed),
            )
            for r in result.all()
        ]

    async def team_load(self, sprint_id: str | None = None) -> list[TeamLoadRow]:
        """Per-member load in ONE query.

        Deliberately not a wrapper over ``DatabaseTools.get_developer_status``,
        which issues a query per member.
        """
        if sprint_id is None:
            sprint = await self._resolve_sprint(None)
            sprint_id = sprint.id if sprint else None

        join_on = Task.assignee_id == TeamMember.id
        if sprint_id:
            join_on = and_(join_on, Task.sprint_id == sprint_id)

        stale_cutoff = _utcnow() - timedelta(days=STALE_DAYS)

        result = await self.db.execute(
            select(
                TeamMember.id,
                TeamMember.name,
                TeamMember.role,
                TeamMember.capacity_hours_per_sprint,
                func.count(Task.id)
                .filter(Task.status == "in_progress")
                .label("in_progress"),
                func.count(Task.id).filter(Task.status == "done").label("done"),
                func.coalesce(
                    func.sum(Task.story_points).filter(Task.status.in_(_OPEN_STATUSES)),
                    0,
                ).label("open_points"),
                func.coalesce(
                    func.sum(Task.estimated_hours).filter(
                        Task.status.in_(_OPEN_STATUSES)
                    ),
                    0,
                ).label("assigned_hours"),
                func.count(Task.id)
                .filter(
                    and_(
                        Task.status == "in_progress",
                        _last_change() < stale_cutoff,
                    )
                )
                .label("stale"),
            )
            .outerjoin(Task, join_on)
            .where(TeamMember.is_available.is_(True))
            .group_by(
                TeamMember.id,
                TeamMember.name,
                TeamMember.role,
                TeamMember.capacity_hours_per_sprint,
            )
            .order_by(TeamMember.name)
        )
        return [
            TeamLoadRow(
                id=r.id,
                name=r.name,
                role=r.role,
                capacity_hours=r.capacity_hours_per_sprint or 0,
                assigned_hours=int(r.assigned_hours or 0),
                open_points=int(r.open_points or 0),
                in_progress=r.in_progress or 0,
                done=r.done or 0,
                stale=r.stale or 0,
            )
            for r in result.all()
        ]

    # ------------------------------------------------------------------
    # velocity
    # ------------------------------------------------------------------

    async def velocity_history(
        self, stream_id: str | None = None, limit: int = 8
    ) -> VelocityResponse:
        """Committed vs completed points per sprint, newest last.

        Sourced from ``sprints`` plus a live task aggregate — NOT from
        confidence snapshots, which only exist for active sprints with tasks
        and would therefore miss most of the history.
        """
        stmt = (
            select(
                Sprint.id,
                Sprint.name,
                Sprint.start_date,
                Sprint.status,
                Sprint.velocity,
                func.coalesce(func.sum(Task.story_points), 0).label("committed"),
                func.coalesce(
                    func.sum(Task.story_points).filter(Task.status == "done"), 0
                ).label("completed"),
                func.count(Task.id).label("items"),
                func.count(Task.id).filter(Task.status == "done").label("items_done"),
            )
            .outerjoin(Task, Task.sprint_id == Sprint.id)
            .where(Sprint.status.in_(["active", "completed"]))
            .group_by(Sprint.id, Sprint.name, Sprint.start_date, Sprint.status, Sprint.velocity)
            .order_by(Sprint.start_date.desc())
            .limit(limit)
        )
        if stream_id:
            stmt = stmt.where(Sprint.stream_id == stream_id)

        rows = list((await self.db.execute(stmt)).all())
        rows.reverse()  # chronological for plotting

        # Teams that never estimate in points get item counts instead of a
        # chart of zeros (same rule as the burndown).
        use_items = not any(int(r.committed or 0) for r in rows)
        points = [
            VelocityPoint(
                sprint_id=r.id,
                name=r.name,
                start_date=r.start_date,
                status=r.status,
                committed_points=int((r.items if use_items else r.committed) or 0),
                completed_points=int((r.items_done if use_items else r.completed) or 0),
                velocity=r.velocity,
                in_flight=r.status == "active",
            )
            for r in rows
        ]

        # The forecast band uses completed sprints only — an in-flight sprint's
        # partial total would drag the mean down and read as a slowdown.
        done = [p.completed_points for p in points if not p.in_flight]
        return VelocityResponse(
            points=points,
            rolling_mean=round(mean(done), 1) if done else None,
            sigma=round(pstdev(done), 1) if len(done) > 1 else None,
            sprints_counted=len(done),
            unit="items" if use_items else "pts",
        )

    # ------------------------------------------------------------------
    # throughput
    # ------------------------------------------------------------------

    async def delivery_summary(
        self, days: int = 14, stream_id: str | None = None
    ) -> DeliverySummary:
        """Completions per day over a window.

        Approximate: ``updated_at`` is a proxy for "finished on". Replaced by
        the event log in ``flow_metrics``.
        """
        since = _utcnow() - timedelta(days=days)
        day = func.date(Task.updated_at).label("day")

        stmt = (
            select(
                day,
                func.count(Task.id).label("completed"),
                func.coalesce(func.sum(Task.story_points), 0).label("points"),
            )
            .where(Task.status == "done", Task.updated_at >= since)
            .group_by(day)
            .order_by(day)
        )
        if stream_id:
            stmt = stmt.join(Sprint, Sprint.id == Task.sprint_id).where(
                Sprint.stream_id == stream_id
            )

        rows = (await self.db.execute(stmt)).all()
        daily = [
            ThroughputPoint(
                date=r.day if isinstance(r.day, date) else date.fromisoformat(str(r.day)),
                completed=r.completed or 0,
                points=int(r.points or 0),
            )
            for r in rows
        ]
        return DeliverySummary(
            days=days,
            completed=sum(d.completed for d in daily),
            points=sum(d.points for d in daily),
            daily=daily,
        )

    # ------------------------------------------------------------------
    # integration health
    # ------------------------------------------------------------------

    async def integration_health(self) -> IntegrationHealth:
        """Freshness of every input the dashboards depend on.

        ``sync_state`` is written by the TFS sync task and has never had a
        route. Without this, a stale dashboard looks identical to a healthy one.
        """
        sync = (
            await self.db.execute(
                select(SyncState).order_by(SyncState.last_sync_at.desc().nullslast()).limit(1)
            )
        ).scalar_one_or_none()

        last_brief = (
            await self.db.execute(select(func.max(Brief.brief_date)))
        ).scalar_one_or_none()
        last_snapshot = (
            await self.db.execute(select(func.max(ConfidenceSnapshot.snapshot_date)))
        ).scalar_one_or_none()

        lag = (date.today() - last_snapshot).days if last_snapshot else None

        return IntegrationHealth(
            provider=sync.provider if sync else None,
            last_sync_at=sync.last_sync_at if sync else None,
            last_status=sync.last_status if sync else None,
            last_error=sync.last_error if sync else None,
            inserted_count=sync.inserted_count if sync else None,
            updated_count=sync.updated_count if sync else None,
            last_brief_date=last_brief,
            last_snapshot_date=last_snapshot,
            snapshot_lag_days=lag,
        )

    # ------------------------------------------------------------------
    # AI workforce
    # ------------------------------------------------------------------

    async def cost_by_stream(self, days: int | None = None) -> CostByStreamResponse:
        """Agent spend attributed through task → sprint → stream.

        Runs whose task has no sprint (or whose sprint has no stream) cannot be
        attributed; their spend is reported separately rather than silently
        dropped or folded into an arbitrary stream.
        """
        stmt = (
            select(
                Stream.id.label("stream_id"),
                Stream.name.label("stream_name"),
                func.coalesce(func.sum(AgentRun.cost_estimate), 0.0).label("cost"),
                func.count(AgentRun.id).label("runs"),
                func.count(AgentRun.id).filter(AgentRun.status == "merged").label("merged"),
            )
            .select_from(AgentRun)
            .join(Task, Task.id == AgentRun.task_id)
            .join(Sprint, Sprint.id == Task.sprint_id)
            .join(Stream, Stream.id == Sprint.stream_id)
            .group_by(Stream.id, Stream.name)
            .order_by(func.sum(AgentRun.cost_estimate).desc())
        )
        if days:
            stmt = stmt.where(AgentRun.created_at >= _utcnow() - timedelta(days=days))

        rows = (await self.db.execute(stmt)).all()
        streams = [
            CostByStream(
                stream_id=r.stream_id,
                stream_name=r.stream_name,
                cost=round(float(r.cost or 0), 2),
                runs=r.runs or 0,
                merged=r.merged or 0,
            )
            for r in rows
        ]

        total_stmt = select(func.coalesce(func.sum(AgentRun.cost_estimate), 0.0))
        if days:
            total_stmt = total_stmt.where(
                AgentRun.created_at >= _utcnow() - timedelta(days=days)
            )
        total = float((await self.db.execute(total_stmt)).scalar_one() or 0)
        attributed = sum(s.cost for s in streams)

        return CostByStreamResponse(
            streams=streams,
            total_cost=round(total, 2),
            unattributed_cost=round(max(0.0, total - attributed), 2),
        )

    async def investment_profile(
        self, sprint_id: str | None = None
    ) -> InvestmentProfileResponse:
        """Story points by work item type (done vs open) for one sprint.

        Defaults to the active sprint; empty rows when there is none — same
        convention as ``sprint_dashboard``.
        """
        sprint = await self._resolve_sprint(sprint_id)
        raw: list[tuple[str, float | None, float | None, int]] = []
        if sprint is not None:
            work_type = func.coalesce(Task.work_item_type, "Unclassified")
            raw = (
                await self.db.execute(
                    select(
                        work_type.label("work_item_type"),
                        func.sum(Task.story_points).label("total"),
                        func.sum(Task.story_points)
                        .filter(Task.status == "done")
                        .label("done"),
                        func.count(Task.id).label("items"),
                    )
                    .where(Task.sprint_id == sprint.id)
                    .group_by(work_type)
                )
            ).all()
        return _investment_response(sprint, raw)

    async def workforce_trend(self, days: int = 84, bucket_days: int = 7) -> WorkforceTrend:
        """Agent output bucketed over time, for the executive ROI charts.

        Cost and merged counts are returned together but must be plotted on
        separate charts — two measures of different scale on one axis is the
        chart mistake this whole surface is meant to avoid.
        """
        since = _utcnow() - timedelta(days=days)
        rows = (
            await self.db.execute(
                select(
                    func.date(AgentRun.created_at).label("day"),
                    func.count(AgentRun.id).label("dispatched"),
                    func.count(AgentRun.id).filter(AgentRun.status == "merged").label("merged"),
                    func.count(AgentRun.id)
                    .filter(AgentRun.status.in_(["failed", "ci_failed"]))
                    .label("failed"),
                    func.coalesce(func.sum(AgentRun.cost_estimate), 0.0).label("cost"),
                )
                .where(AgentRun.created_at >= since)
                .group_by(func.date(AgentRun.created_at))
                .order_by(func.date(AgentRun.created_at))
            )
        ).all()

        # Bucket in Python: the row count here is bounded by `days`, so this is
        # cheap, and it keeps the date arithmetic out of dialect-specific SQL.
        buckets: dict[date, dict[str, Any]] = {}
        start = (since).date()
        for r in rows:
            d = r.day if isinstance(r.day, date) else date.fromisoformat(str(r.day))
            idx = (d - start).days // bucket_days
            key = start + timedelta(days=idx * bucket_days)
            b = buckets.setdefault(
                key, {"dispatched": 0, "merged": 0, "failed": 0, "cost": 0.0}
            )
            b["dispatched"] += r.dispatched or 0
            b["merged"] += r.merged or 0
            b["failed"] += r.failed or 0
            b["cost"] += float(r.cost or 0)

        points = [
            WorkforcePoint(
                date=k,
                dispatched=v["dispatched"],
                merged=v["merged"],
                failed=v["failed"],
                cost=round(v["cost"], 2),
            )
            for k, v in sorted(buckets.items())
        ]
        return WorkforceTrend(points=points, bucket_days=bucket_days)

    # ------------------------------------------------------------------
    # flow metrics (from the task status event log)
    # ------------------------------------------------------------------

    async def flow_metrics(
        self, days: int = 30, stream_id: str | None = None
    ) -> FlowMetrics:
        """Throughput, cycle time, lead time, aging WIP and a CFD.

        Derived from ``task_status_events``. Snapshots cannot answer these —
        they record a daily position, not a transition.

        ``history_starts`` deliberately ignores rows written by the migration's
        synthetic backfill, so the UI can say how far genuinely-observed history
        goes rather than implying the log covers all time.
        """
        since = _utcnow() - timedelta(days=days)

        real_start = (
            await self.db.execute(
                select(func.min(TaskStatusEvent.changed_at)).where(
                    TaskStatusEvent.source != "backfill"
                )
            )
        ).scalar_one_or_none()
        any_real = (
            await self.db.execute(
                select(func.count(TaskStatusEvent.id)).where(
                    TaskStatusEvent.source != "backfill"
                )
            )
        ).scalar_one() or 0

        stream_task_ids: list[str] | None = None
        if stream_id:
            rows = await self.db.execute(
                select(Task.id)
                .join(Sprint, Sprint.id == Task.sprint_id)
                .where(Sprint.stream_id == stream_id)
            )
            stream_task_ids = [r[0] for r in rows.all()]
            if not stream_task_ids:
                return FlowMetrics(
                    days=days, history_starts=None, backfilled_only=any_real == 0,
                    throughput=[], items=[], cfd=[], aging_wip=[],
                )

        # --- throughput: transitions INTO done, per day ---
        done_day = func.date(TaskStatusEvent.changed_at).label("day")
        thr_stmt = (
            select(done_day, func.count(TaskStatusEvent.id))
            .where(
                TaskStatusEvent.to_status == "done",
                TaskStatusEvent.changed_at >= since,
                # Backfill rows all carry the task's updated_at, so counting
                # them would report a fake burst of completions on whatever day
                # the seed data happened to be written.
                TaskStatusEvent.source != "backfill",
            )
            .group_by(done_day)
            .order_by(done_day)
        )
        if stream_task_ids is not None:
            thr_stmt = thr_stmt.where(TaskStatusEvent.task_id.in_(stream_task_ids))
        throughput = [
            FlowBucket(
                date=r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])),
                count=r[1] or 0,
            )
            for r in (await self.db.execute(thr_stmt)).all()
        ]

        # --- cycle & lead time: replay each task's events once ---
        ev_stmt = (
            select(
                TaskStatusEvent.task_id,
                TaskStatusEvent.to_status,
                TaskStatusEvent.changed_at,
                TaskStatusEvent.source,
                Task.title,
                Task.created_at,
            )
            .join(Task, Task.id == TaskStatusEvent.task_id)
            .order_by(TaskStatusEvent.task_id, TaskStatusEvent.changed_at)
        )
        if stream_task_ids is not None:
            ev_stmt = ev_stmt.where(TaskStatusEvent.task_id.in_(stream_task_ids))
        rows = (await self.db.execute(ev_stmt)).all()

        started: dict[str, datetime] = {}
        # Earliest event per task. Synced rows are created at sync time, so
        # Task.created_at alone can postdate the work and give negative lead
        # times; the first recorded event is the better "created" signal.
        first_seen: dict[str, datetime] = {}
        titles: dict[str, str] = {}
        created: dict[str, datetime | None] = {}
        items: list[CycleTimeItem] = []
        cycles: list[float] = []
        leads: list[float] = []

        for task_id, to_status, changed_at, source, title, task_created in rows:
            first_seen.setdefault(task_id, changed_at)
            titles[task_id] = title
            created[task_id] = task_created
            if to_status == "in_progress" and task_id not in started:
                started[task_id] = changed_at
            elif to_status == "done":
                if changed_at < since:
                    continue
                # A backfilled "done" has no observed start, so it yields no
                # cycle time — counting it as zero would fake a fast delivery.
                if source == "backfill":
                    continue
                start = started.get(task_id)
                if start is None:
                    continue
                cycle_days = round((changed_at - start).total_seconds() / 86400, 2)
                cycles.append(cycle_days)
                lead_start = first_seen[task_id]
                if task_created:
                    ct = task_created if task_created.tzinfo else task_created.replace(tzinfo=timezone.utc)
                    lead_start = min(lead_start, ct)
                lead_days = (changed_at - lead_start).total_seconds() / 86400
                if lead_days >= 0:  # a negative span is a data error, not a fast delivery
                    leads.append(round(lead_days, 2))
                items.append(
                    CycleTimeItem(
                        task_id=task_id,
                        title=title,
                        days=cycle_days,
                        completed_on=changed_at.date(),
                    )
                )

        # --- aging WIP: in progress now, ordered by how long ---
        aging_stmt = (
            select(Task.id, Task.title, Task.status, TeamMember.name)
            .outerjoin(TeamMember, TeamMember.id == Task.assignee_id)
            .where(Task.status.in_(["in_progress", "review"]))
        )
        if stream_task_ids is not None:
            aging_stmt = aging_stmt.where(Task.id.in_(stream_task_ids))
        aging: list[AgingItem] = []
        for tid, title, status_, assignee in (await self.db.execute(aging_stmt)).all():
            entered = started.get(tid)
            if entered is None:
                continue
            aging.append(
                AgingItem(
                    task_id=tid,
                    title=title,
                    status=status_,
                    days_in_status=round(
                        (_utcnow() - entered).total_seconds() / 86400, 1
                    ),
                    assignee_name=assignee,
                )
            )
        aging.sort(key=lambda a: a.days_in_status, reverse=True)

        cfd = self._cumulative_flow(rows, since.date(), date.today())

        return FlowMetrics(
            days=days,
            history_starts=real_start.date() if real_start else None,
            backfilled_only=any_real == 0,
            throughput=throughput,
            cycle_time_p50=_percentile(cycles, 0.50),
            cycle_time_p85=_percentile(cycles, 0.85),
            lead_time_p50=_percentile(leads, 0.50),
            lead_time_p85=_percentile(leads, 0.85),
            items=sorted(items, key=lambda i: i.days, reverse=True)[:40],
            cfd=cfd,
            aging_wip=aging[:20],
        )

    @staticmethod
    def _cumulative_flow(
        rows: list, start: date, end: date
    ) -> list[StatusCount]:
        """Status distribution per day, replayed from the event stream.

        Each task's status on a given day is its last event on or before that
        day; tasks with no event yet are simply absent, so the bands grow as
        work enters the system rather than starting at a fabricated total.

        Replayed in Python because the event rows are already loaded for cycle
        time. That is O(days x tasks) and fine at this scale; if the event log
        grows large this wants a window function instead.
        """
        if not rows or end < start:
            return []

        # (task_id -> [(day, status)]), chronological.
        history: dict[str, list[tuple[date, str]]] = {}
        for task_id, to_status, changed_at, _source, _title, _created in rows:
            history.setdefault(task_id, []).append((changed_at.date(), to_status))

        out: list[StatusCount] = []
        cursor = start
        while cursor <= end:
            counts = {"backlog": 0, "todo": 0, "in_progress": 0, "review": 0, "done": 0}
            for events in history.values():
                status_on_day = None
                for day, status_ in events:
                    if day <= cursor:
                        status_on_day = status_
                    else:
                        break
                if status_on_day in counts:
                    counts[status_on_day] += 1
            out.append(StatusCount(date=cursor, **counts))
            cursor += timedelta(days=1)
        return out

    # ------------------------------------------------------------------
    # roadmap
    # ------------------------------------------------------------------

    async def roadmap(self, stream_id: str | None = None) -> RoadmapResponse:
        """Feature spans derived from the sprint dates of their tasks.

        ``features`` has no date column, so a feature whose tasks are not yet
        in a sprint has no span. That is reported as null dates — never an
        invented window.
        """
        stmt = (
            select(
                Feature.id,
                Feature.name,
                Feature.stream_id,
                Stream.name.label("stream_name"),
                func.min(Sprint.start_date).label("start_date"),
                func.max(Sprint.end_date).label("end_date"),
                func.count(Task.id).label("total_tasks"),
                func.count(Task.id).filter(Task.status == "done").label("completed_tasks"),
                func.coalesce(func.sum(Task.story_points), 0).label("total_points"),
                func.coalesce(
                    func.sum(Task.story_points).filter(Task.status == "done"), 0
                ).label("completed_points"),
            )
            .select_from(Feature)
            .outerjoin(Stream, Stream.id == Feature.stream_id)
            .outerjoin(Task, Task.feature_id == Feature.id)
            .outerjoin(Sprint, Sprint.id == Task.sprint_id)
            .group_by(Feature.id, Feature.name, Feature.stream_id, Stream.name)
            .order_by(func.min(Sprint.start_date).nullslast(), Feature.name)
        )
        if stream_id:
            stmt = stmt.where(Feature.stream_id == stream_id)

        rows = (await self.db.execute(stmt)).all()

        # One rollup per stream, reused for every feature in it, so a feature's
        # colour matches the stream health shown on the portfolio page.
        rollups: dict[str, str] = {}
        items: list[RoadmapItem] = []
        for r in rows:
            status = "No data"
            if r.stream_id:
                if r.stream_id not in rollups:
                    rollup = await self._confidence.get_stream_rollup(r.stream_id)
                    rollups[r.stream_id] = rollup["status"]
                status = rollups[r.stream_id]

            total_pts = int(r.total_points or 0)
            done_pts = int(r.completed_points or 0)
            items.append(
                RoadmapItem(
                    id=r.id,
                    name=r.name,
                    stream_id=r.stream_id,
                    stream_name=r.stream_name,
                    start_date=r.start_date,
                    end_date=r.end_date,
                    status=status,
                    progress=round(done_pts / total_pts, 3) if total_pts else 0.0,
                    total_tasks=r.total_tasks or 0,
                    completed_tasks=r.completed_tasks or 0,
                    total_points=total_pts,
                    completed_points=done_pts,
                )
            )

        starts = [i.start_date for i in items if i.start_date]
        ends = [i.end_date for i in items if i.end_date]
        return RoadmapResponse(
            items=items,
            window_start=min(starts) if starts else None,
            window_end=max(ends) if ends else None,
            today=date.today(),
        )
