"""Global search endpoint (issue #35): tasks and teammates by substring."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.sprint import Sprint
from app.models.task import Task
from app.models.team import TeamMember
from app.schemas.search import MemberHit, SearchResponse, TaskHit

router = APIRouter(prefix="/search", tags=["search"])

LIMIT = 10


def escape_like(q: str) -> str:
    """Escape ILIKE wildcards (and the escape char itself) in user input.

    ``q`` is attacker-controlled; without this, a literal ``%`` or ``_`` in it
    would match everything / any char. Order matters: backslash first.
    """
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(min_length=2),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    like = f"%{escape_like(q)}%"
    match = lambda col: col.ilike(like, escape="\\")  # noqa: E731
    task_rows = (
        await db.execute(
            select(
                Task.id,
                Task.title,
                Task.status,
                Task.sprint_id,
                Sprint.name.label("sprint_name"),
                TeamMember.name.label("assignee_name"),
            )
            .outerjoin(Sprint, Sprint.id == Task.sprint_id)
            .outerjoin(TeamMember, TeamMember.id == Task.assignee_id)
            .where(match(Task.title) | match(Task.external_id))
            .order_by(Task.created_at.desc())
            .limit(LIMIT)
        )
    ).all()
    member_rows = (
        await db.execute(
            select(TeamMember.id, TeamMember.name, TeamMember.role)
            .where(match(TeamMember.name))
            .order_by(TeamMember.created_at.desc())
            .limit(LIMIT)
        )
    ).all()
    return SearchResponse(
        tasks=[
            TaskHit(
                id=r.id,
                title=r.title,
                status=r.status,
                sprint_id=r.sprint_id,
                sprint_name=r.sprint_name,
                assignee_name=r.assignee_name,
            )
            for r in task_rows
        ],
        members=[
            MemberHit(id=r.id, name=r.name, role=r.role) for r in member_rows
        ],
    )
