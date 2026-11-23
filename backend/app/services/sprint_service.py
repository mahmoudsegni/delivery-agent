"""Sprint service for business logic."""

import json
import logging
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.proposal import Proposal
from app.models.sprint import Sprint
from app.models.task import Task
from app.schemas.sprint import (
    SprintBurndown,
    SprintCreate,
    SprintUpdate,
    SprintWithStats,
)
from app.services.groq_client import groq_chat

logger = logging.getLogger(__name__)


class SprintNotFoundError(Exception):
    """Raised when sprint is not found."""

    pass


class PendingReleaseNotesError(Exception):
    """Raised when a release-notes draft for this sprint awaits review."""

    pass


class SprintService:
    """Service for sprint business logic."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_sprints(
        self,
        skip: int = 0,
        limit: int = 50,
        status: str | None = None,
    ) -> tuple[list[Sprint], int]:
        """
        List sprints with optional filtering.

        Args:
            skip: Number of records to skip
            limit: Maximum records to return
            status: Filter by sprint status

        Returns:
            Tuple of (sprints, total_count)
        """
        query = select(Sprint)

        if status:
            query = query.where(Sprint.status == status)

        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total = await self.db.scalar(count_query) or 0

        # Get paginated results
        query = query.order_by(Sprint.start_date.desc()).offset(skip).limit(limit)
        result = await self.db.execute(query)
        sprints = list(result.scalars().all())

        return sprints, total

    async def get_sprint(self, sprint_id: UUID) -> Sprint:
        """
        Get sprint by ID.

        Raises:
            SprintNotFoundError: If sprint not found
        """
        result = await self.db.execute(
            select(Sprint).where(Sprint.id == str(sprint_id))
        )
        sprint = result.scalar_one_or_none()

        if sprint is None:
            raise SprintNotFoundError(f"Sprint not found: {sprint_id}")

        return sprint

    async def get_sprint_with_stats(self, sprint_id: UUID) -> SprintWithStats:
        """Get sprint with computed statistics."""
        sprint = await self.get_sprint(sprint_id)

        # Get task statistics
        result = await self.db.execute(
            select(
                func.count(Task.id).label("total"),
                func.count(Task.id).filter(Task.status == "done").label("completed"),
                func.coalesce(func.sum(Task.story_points), 0).label("total_points"),
                func.coalesce(
                    func.sum(Task.story_points).filter(Task.status == "done"), 0
                ).label("completed_points"),
            ).where(Task.sprint_id == str(sprint_id))
        )
        stats = result.one()

        total_tasks = stats.total or 0
        completed_tasks = stats.completed or 0
        total_points = stats.total_points or 0
        completed_points = stats.completed_points or 0

        progress = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0.0

        return SprintWithStats(
            id=sprint.id,
            name=sprint.name,
            goal=sprint.goal,
            start_date=sprint.start_date,
            end_date=sprint.end_date,
            status=sprint.status,
            capacity_hours=sprint.capacity_hours,
            capacity_breakdown=sprint.capacity_breakdown,
            velocity=sprint.velocity,
            retrospective_notes=sprint.retrospective_notes,
            created_at=sprint.created_at,
            updated_at=sprint.updated_at,
            total_tasks=total_tasks,
            completed_tasks=completed_tasks,
            total_story_points=total_points,
            completed_story_points=completed_points,
            progress_percentage=round(progress, 1),
        )

    async def create_sprint(self, data: SprintCreate) -> Sprint:
        """Create a new sprint. Without an explicit end date, sprints are two
        weeks (start + 13 days, so the window spans exactly 14 calendar days)."""
        sprint = Sprint(
            name=data.name,
            goal=data.goal,
            start_date=data.start_date,
            end_date=data.end_date or data.start_date + timedelta(days=13),
            capacity_hours=data.capacity_hours,
            capacity_breakdown=data.capacity_breakdown,
        )
        self.db.add(sprint)
        await self.db.flush()
        await self.db.refresh(sprint)
        return sprint

    async def update_sprint(self, sprint_id: UUID, data: SprintUpdate) -> Sprint:
        """Update an existing sprint."""
        sprint = await self.get_sprint(sprint_id)

        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(sprint, field, value)

        await self.db.flush()
        await self.db.refresh(sprint)
        return sprint

    async def delete_sprint(self, sprint_id: UUID) -> None:
        """Delete a sprint (unassigns all tasks first)."""
        sprint = await self.get_sprint(sprint_id)

        # Unassign all tasks from this sprint
        await self.db.execute(
            Task.__table__.update()
            .where(Task.sprint_id == str(sprint_id))
            .values(sprint_id=None)
        )

        await self.db.delete(sprint)
        await self.db.flush()

    async def add_tasks_to_sprint(
        self, sprint_id: UUID, task_ids: list[UUID]
    ) -> list[Task]:
        """Add tasks to a sprint."""
        await self.get_sprint(sprint_id)  # Verify sprint exists

        result = await self.db.execute(
            select(Task).where(Task.id.in_([str(tid) for tid in task_ids]))
        )
        tasks = list(result.scalars().all())

        for task in tasks:
            task.sprint_id = str(sprint_id)

        await self.db.flush()
        return tasks

    async def remove_tasks_from_sprint(
        self, sprint_id: UUID, task_ids: list[UUID]
    ) -> list[Task]:
        """Remove tasks from a sprint (move back to backlog)."""
        await self.get_sprint(sprint_id)  # Verify sprint exists

        result = await self.db.execute(
            select(Task).where(
                Task.id.in_([str(tid) for tid in task_ids]),
                Task.sprint_id == str(sprint_id),
            )
        )
        tasks = list(result.scalars().all())

        for task in tasks:
            task.sprint_id = None

        await self.db.flush()
        return tasks

    async def get_sprint_tasks(
        self, sprint_id: UUID, status: str | None = None
    ) -> list[Task]:
        """Get all tasks in a sprint."""
        await self.get_sprint(sprint_id)  # Verify sprint exists

        query = (
            select(Task)
            .where(Task.sprint_id == str(sprint_id))
            .options(selectinload(Task.assignee))
        )

        if status:
            query = query.where(Task.status == status)

        query = query.order_by(Task.priority_order)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_today_burndown_point(self, sprint_id: UUID) -> tuple[float, float]:
        """Return ``(ideal_remaining, actual_remaining)`` for TODAY, live.

        Computed entirely from current task state — it reads no snapshot. That
        matters: ``ConfidenceService`` calls this to derive ``velocity_vs_plan``,
        and if it were fed replayed snapshot history instead, confidence would
        become a function of yesterday's confidence and drift with no error and
        no failing test.

        The historical series lives in ``BurndownService.get_series``.
        """
        sprint = await self.get_sprint(sprint_id)

        total, done, _ = await self.work_units(sprint_id)
        total_points = float(total)
        actual_remaining = total_points - float(done)

        ideal_remaining = self.ideal_remaining_on(
            date.today(), sprint.start_date, sprint.end_date, total_points
        )
        return ideal_remaining, actual_remaining

    async def work_units(self, sprint_id: UUID | str) -> tuple[int, int, str]:
        """``(total, done, unit)`` of work in a sprint.

        Story points when the team estimates in points; otherwise item counts.
        Many Scrum teams (the on-prem ADO instance among them) never fill
        points, and a points-only burndown is then a flat line at zero.
        """
        result = await self.db.execute(
            select(
                func.coalesce(func.sum(Task.story_points), 0).label("pts"),
                func.coalesce(
                    func.sum(Task.story_points).filter(Task.status == "done"), 0
                ).label("pts_done"),
                func.count(Task.id).label("items"),
                func.count(Task.id).filter(Task.status == "done").label("items_done"),
            ).where(Task.sprint_id == str(sprint_id))
        )
        row = result.one()
        if int(row.pts or 0) > 0:
            return int(row.pts), int(row.pts_done or 0), "pts"
        return int(row.items or 0), int(row.items_done or 0), "items"

    @staticmethod
    def ideal_remaining_on(
        on: date, start: date, end: date, total_points: float
    ) -> float:
        """Straight-line plan value for a date, clamped to the sprint window.

        Deterministic, so the ideal line stays straight even across days that
        have no actual reading.
        """
        span = (end - start).days
        if span <= 0:
            return 0.0 if on >= end else total_points
        elapsed = min(max((on - start).days, 0), span)
        return round(total_points - (total_points / span) * elapsed, 1)

    async def get_burndown(self, sprint_id: UUID) -> SprintBurndown:
        """Burndown series for a sprint.

        Delegates to ``BurndownService``, which replays the daily confidence
        snapshots for history and appends today's live point.
        """
        from app.services.burndown_service import BurndownService

        return await BurndownService(self.db).get_series(sprint_id)

    async def activate_sprint(self, sprint_id: UUID) -> Sprint:
        """Activate a sprint (change status to active)."""
        sprint = await self.get_sprint(sprint_id)

        if sprint.status != "planning":
            raise ValueError(f"Can only activate sprints in 'planning' status, got: {sprint.status}")

        sprint.status = "active"
        await self.db.flush()
        await self.db.refresh(sprint)
        return sprint

    async def complete_sprint(self, sprint_id: UUID) -> Sprint:
        """Complete a sprint and calculate velocity."""
        sprint = await self.get_sprint(sprint_id)

        if sprint.status != "active":
            raise ValueError(f"Can only complete sprints in 'active' status, got: {sprint.status}")

        # Calculate velocity (story points completed)
        result = await self.db.execute(
            select(func.coalesce(func.sum(Task.story_points), 0)).where(
                Task.sprint_id == str(sprint_id),
                Task.status == "done",
            )
        )
        velocity = result.scalar() or 0

        sprint.status = "completed"
        sprint.velocity = velocity
        await self.db.flush()
        await self.db.refresh(sprint)
        return sprint

    async def get_active_sprint(self) -> Sprint | None:
        """Get the currently active sprint."""
        result = await self.db.execute(
            select(Sprint).where(Sprint.status == "active").limit(1)
        )
        return result.scalar_one_or_none()

    async def draft_release_notes(self, sprint_id: UUID) -> Proposal:
        """Draft markdown release notes from a sprint's done work.

        The facts (done tasks) come from the DB; the LLM only words them into
        a grouped changelog. Returns a ``release_notes`` proposal in
        ``pending_review`` — nothing is pushed anywhere until a human approves.
        """
        sprint = await self.get_sprint(sprint_id)

        pending = (
            await self.db.execute(
                select(Proposal).where(
                    Proposal.type == "release_notes",
                    Proposal.state == "pending_review",
                )
            )
        ).scalars()
        for p in pending:
            if (p.content or {}).get("sprint_id") == str(sprint_id):
                raise PendingReleaseNotesError(
                    f"A release-notes draft for {sprint.name} is already pending review."
                )

        done = await self.get_sprint_tasks(sprint_id, status="done")
        facts = [
            {
                "item": f"#{t.external_id}" if t.external_id else f"#{t.id[:8]}",
                "title": t.title,
                "type": t.work_item_type,
                "area": t.project_name,
            }
            for t in done
        ]
        markdown = await groq_chat(
            [
                {
                    "role": "system",
                    "content": "You are a technical writer drafting release notes. "
                    "Return markdown only.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Draft release notes for {sprint.name} (ended {sprint.end_date}). "
                        f"Done items (facts — use only these, one bullet each, grouped under "
                        f"Features / Bug fixes / Other, every bullet references its item id):\n"
                        f"{json.dumps(facts, ensure_ascii=False)}\n"
                        "Start with a one-line summary and a `## <sprint name>` heading."
                    ),
                },
            ]
        )

        proposal = Proposal(
            type="release_notes",
            state="pending_review",
            content={"sprint_id": str(sprint_id), "markdown": markdown},
            reasoning=f"Release notes draft for {sprint.name} from {len(done)} done item(s).",
            confidence="medium",
            created_by_agent="release-notes",
        )
        self.db.add(proposal)
        await self.db.flush()
        await self.db.refresh(proposal)
        return proposal
