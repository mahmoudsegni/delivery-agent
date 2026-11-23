"""Database-connected tools for Deep Agent.

These tools query the actual database to provide real data to the agent.
They are designed to be injected into the Deep Agent at runtime.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sprint import Sprint
from app.models.task import Task
from app.models.team import TeamMember

# When an item last really changed state. Synced items carry ADO's
# StateChangeDate; ``updated_at`` is rewritten by every sync, so on its own it
# made every synced item look "changed today".
LAST_CHANGE = func.coalesce(Task.state_changed_at, Task.updated_at)


def _last_change(task: Task) -> datetime | None:
    return task.state_changed_at or task.updated_at

logger = logging.getLogger(__name__)


class DatabaseTools:
    """
    Container for database-connected agent tools.

    These tools are instantiated with a database session and provide
    real data to the Deep Agent during processing.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_team_capacity(self, sprint_id: str | None = None) -> dict[str, Any]:
        """
        Get team capacity from database.

        Args:
            sprint_id: Optional sprint ID for sprint-specific capacity.

        Returns:
            Dictionary with team member availability and capacity hours.
        """
        # Get all available team members
        result = await self.db.execute(
            select(TeamMember).where(TeamMember.is_available.is_(True))
        )
        members = list(result.scalars().all())

        team_data = []
        total_capacity = 0

        for member in members:
            capacity = member.capacity_hours_per_sprint
            total_capacity += capacity
            team_data.append({
                "id": str(member.id),
                "name": member.name,
                "email": member.email,
                "role": member.role,
                "available_hours": capacity,
            })

        # If sprint_id provided, get sprint-specific capacity breakdown
        capacity_breakdown = None
        if sprint_id:
            try:
                result = await self.db.execute(
                    select(Sprint).where(Sprint.id == sprint_id)
                )
                sprint = result.scalar_one_or_none()
                if sprint and sprint.capacity_breakdown:
                    capacity_breakdown = sprint.capacity_breakdown
                    total_capacity = sprint.capacity_hours
            except Exception as e:
                logger.warning(f"Could not load sprint capacity: {e}")

        return {
            "sprint_id": sprint_id,
            "total_capacity_hours": total_capacity,
            "capacity_breakdown": capacity_breakdown,
            "team_members": team_data,
        }

    async def get_backlog_items(
        self, priority: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """
        Get backlog items from database.

        Args:
            priority: Filter by priority (high, medium, low, critical).
            limit: Maximum items to return.

        Returns:
            List of backlog items.
        """
        query = select(Task).where(Task.sprint_id.is_(None))

        if priority:
            query = query.where(Task.priority == priority)

        query = query.order_by(Task.priority_order).limit(limit)

        result = await self.db.execute(query)
        tasks = list(result.scalars().all())

        return [
            {
                "id": str(task.id),
                "title": task.title,
                "description": task.description,
                "priority": task.priority,
                "status": task.status,
                "story_points": task.story_points,
                "assignee_id": str(task.assignee_id) if task.assignee_id else None,
            }
            for task in tasks
        ]

    async def get_sprint_status(self, sprint_id: str | None = None) -> dict[str, Any]:
        """
        Get sprint status from database.

        Args:
            sprint_id: Sprint ID. None returns active sprint.

        Returns:
            Sprint status with task breakdown.
        """
        if sprint_id:
            result = await self.db.execute(
                select(Sprint).where(Sprint.id == sprint_id)
            )
        else:
            result = await self.db.execute(
                select(Sprint).where(Sprint.status == "active").limit(1)
            )

        sprint = result.scalar_one_or_none()

        if not sprint:
            return {"error": "No active sprint found", "sprint_id": sprint_id}

        # Get task counts by status
        task_counts = await self.db.execute(
            select(Task.status, func.count(Task.id))
            .where(Task.sprint_id == str(sprint.id))
            .group_by(Task.status)
        )
        counts = {row[0]: row[1] for row in task_counts.all()}

        # Get blocked tasks (in_progress for too long or explicitly marked)
        blocked_threshold = datetime.now(timezone.utc) - timedelta(days=3)
        blocked_result = await self.db.execute(
            select(Task).where(
                Task.sprint_id == str(sprint.id),
                Task.status == "in_progress",
                LAST_CHANGE < blocked_threshold,
            )
        )
        blocked_tasks = list(blocked_result.scalars().all())

        return {
            "sprint_id": str(sprint.id),
            "name": sprint.name,
            "goal": sprint.goal,
            "start_date": str(sprint.start_date),
            "end_date": str(sprint.end_date),
            "status": sprint.status,
            "tasks": {
                "total": sum(counts.values()),
                "backlog": counts.get("backlog", 0),
                "todo": counts.get("todo", 0),
                "in_progress": counts.get("in_progress", 0),
                "review": counts.get("review", 0),
                "done": counts.get("done", 0),
            },
            "potentially_blocked": [
                {
                    "id": str(t.id),
                    "title": t.title,
                    "days_in_progress": (datetime.now(timezone.utc) - _last_change(t)).days
                    if _last_change(t)
                    else 0,
                }
                for t in blocked_tasks
            ],
        }

    async def get_developer_status(
        self, developer_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Get developer status for standup.

        Args:
            developer_id: Specific developer ID. None returns all.

        Returns:
            List of developer statuses with current tasks.
        """
        query = select(TeamMember)
        if developer_id:
            query = query.where(TeamMember.id == developer_id)

        result = await self.db.execute(query)
        members = list(result.scalars().all())

        statuses = []
        for member in members:
            # Get current in-progress tasks
            task_result = await self.db.execute(
                select(Task).where(
                    Task.assignee_id == str(member.id),
                    Task.status == "in_progress",
                )
            )
            current_tasks = list(task_result.scalars().all())

            # Check for potential blockers (tasks in progress > 2 days)
            blockers = []
            for task in current_tasks:
                if _last_change(task):
                    days = (datetime.now(timezone.utc) - _last_change(task)).days
                    if days > 2:
                        blockers.append(f"{task.title} (in progress for {days} days)")

            statuses.append({
                "id": str(member.id),
                "name": member.name,
                "email": member.email,
                "is_available": member.is_available,
                "current_tasks": [
                    {"id": str(t.id), "title": t.title, "status": t.status}
                    for t in current_tasks
                ],
                "potential_blockers": blockers,
                "status": "blocked" if blockers else "on_track",
            })

        return statuses

    async def get_completed_tasks(
        self, sprint_id: str | None = None, since_days: int = 7
    ) -> list[dict[str, Any]]:
        """
        Get recently completed tasks for changelog generation.

        Args:
            sprint_id: Filter by sprint. None returns recent completions.
            since_days: Look back period in days.

        Returns:
            List of completed tasks.
        """
        since = datetime.now(timezone.utc) - timedelta(days=since_days)

        query = select(Task).where(
            Task.status == "done",
            LAST_CHANGE >= since,
        )

        if sprint_id:
            query = query.where(Task.sprint_id == sprint_id)

        query = query.order_by(LAST_CHANGE.desc())

        result = await self.db.execute(query)
        tasks = list(result.scalars().all())

        return [
            {
                "id": str(task.id),
                "title": task.title,
                "description": task.description,
                "priority": task.priority,
                "story_points": task.story_points,
                "completed_at": str(_last_change(task)) if _last_change(task) else None,
            }
            for task in tasks
        ]

    async def get_release_readiness(
        self, sprint_id: str | None = None
    ) -> dict[str, Any]:
        """
        Assess release readiness.

        Args:
            sprint_id: Sprint to assess. None uses active sprint.

        Returns:
            Release readiness assessment.
        """
        sprint_status = await self.get_sprint_status(sprint_id)

        if "error" in sprint_status:
            return sprint_status

        tasks = sprint_status.get("tasks", {})
        total = tasks.get("total", 0)
        done = tasks.get("done", 0)
        in_progress = tasks.get("in_progress", 0)
        blocked = len(sprint_status.get("potentially_blocked", []))

        # Calculate readiness
        completion_rate = (done / total * 100) if total > 0 else 0
        risk_level = "low"

        if blocked > 0 or completion_rate < 50:
            risk_level = "high"
        elif in_progress > done or completion_rate < 80:
            risk_level = "medium"

        return {
            "sprint_id": sprint_status.get("sprint_id"),
            "sprint_name": sprint_status.get("name"),
            "completion_rate": round(completion_rate, 1),
            "tasks_done": done,
            "tasks_remaining": total - done,
            "tasks_blocked": blocked,
            "risk_level": risk_level,
            "recommendation": (
                "Ready to release"
                if risk_level == "low"
                else "Resolve blockers before release"
                if risk_level == "high"
                else "Review remaining work before release"
            ),
            "blockers": sprint_status.get("potentially_blocked", []),
        }


def create_tool_functions(db_tools: DatabaseTools) -> list:
    """
    Create standalone tool functions that wrap DatabaseTools methods.

    Deep Agents expect regular functions, so we create closures that
    call the async database methods.
    """
    import asyncio

    def get_team_capacity(sprint_id: str | None = None) -> dict[str, Any]:
        """Get team capacity for sprint planning."""
        return asyncio.get_event_loop().run_until_complete(
            db_tools.get_team_capacity(sprint_id)
        )

    def get_backlog_items(priority: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Get backlog items for sprint planning."""
        return asyncio.get_event_loop().run_until_complete(
            db_tools.get_backlog_items(priority, limit)
        )

    def get_sprint_status(sprint_id: str | None = None) -> dict[str, Any]:
        """Get current sprint status with task breakdown."""
        return asyncio.get_event_loop().run_until_complete(
            db_tools.get_sprint_status(sprint_id)
        )

    def get_developer_status(developer_id: str | None = None) -> list[dict[str, Any]]:
        """Get developer status for standup."""
        return asyncio.get_event_loop().run_until_complete(
            db_tools.get_developer_status(developer_id)
        )

    def get_completed_tasks(sprint_id: str | None = None, since_days: int = 7) -> list[dict[str, Any]]:
        """Get recently completed tasks for changelog."""
        return asyncio.get_event_loop().run_until_complete(
            db_tools.get_completed_tasks(sprint_id, since_days)
        )

    def get_release_readiness(sprint_id: str | None = None) -> dict[str, Any]:
        """Assess release readiness."""
        return asyncio.get_event_loop().run_until_complete(
            db_tools.get_release_readiness(sprint_id)
        )

    return [
        get_team_capacity,
        get_backlog_items,
        get_sprint_status,
        get_developer_status,
        get_completed_tasks,
        get_release_readiness,
    ]
