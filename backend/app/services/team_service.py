"""Team management service layer."""

from datetime import date, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.member_absence import MemberAbsence
from app.models.team import TeamMember
from app.schemas.team import AbsenceCreate, TeamMemberCreate, TeamMemberUpdate

# Hours of capacity one absent workday removes. Weekends are not workdays.
WORKDAY_HOURS = 8


def count_workdays(start: date, end: date) -> int:
    """Workdays (Mon-Fri) in [start, end], inclusive."""
    days, cursor = 0, start
    while cursor <= end:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def absent_workdays(absences, sprint_start: date | None, sprint_end: date | None) -> int:
    """Workdays lost to absences inside the sprint window (clipped to it)."""
    if sprint_start is None or sprint_end is None:
        return 0
    total = 0
    for a in absences:
        start, end = max(a.starts_on, sprint_start), min(a.ends_on, sprint_end)
        # ponytail: overlapping absences double-count; merge ranges if people
        # start entering them that way.
        if start <= end:
            total += count_workdays(start, end)
    return total


def effective_capacity_hours(member: TeamMember, sprint, absences) -> int:
    """Flat capacity minus 8h per absent workday in the sprint, clamped >= 0."""
    absent = absent_workdays(
        absences, getattr(sprint, "start_date", None), getattr(sprint, "end_date", None)
    )
    return max(0, (member.capacity_hours_per_sprint or 0) - WORKDAY_HOURS * absent)


async def sprint_absences(
    db: AsyncSession, sprint_start: date, sprint_end: date
) -> list[MemberAbsence]:
    """All absences overlapping the sprint window — one query for the whole team."""
    result = await db.execute(
        select(MemberAbsence).where(
            MemberAbsence.starts_on <= sprint_end,
            MemberAbsence.ends_on >= sprint_start,
        )
    )
    return list(result.scalars().all())


class TeamService:
    """Service class for team member operations."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_members(
        self, skip: int = 0, limit: int = 50
    ) -> tuple[list[TeamMember], int]:
        """
        List team members with pagination.

        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            Tuple of (list of team members, total count)
        """
        # Get total count
        count_query = select(func.count()).select_from(TeamMember)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # Get paginated members
        query = select(TeamMember).offset(skip).limit(limit).order_by(TeamMember.name)
        result = await self.db.execute(query)
        members = list(result.scalars().all())

        return members, total

    async def get_member(self, member_id: UUID) -> TeamMember:
        """
        Get a team member by ID.

        Args:
            member_id: UUID of the team member

        Returns:
            TeamMember instance

        Raises:
            HTTPException: 404 if member not found
        """
        query = select(TeamMember).where(TeamMember.id == member_id)
        result = await self.db.execute(query)
        member = result.scalar_one_or_none()

        if member is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Team member with id {member_id} not found",
            )

        return member

    async def create_member(self, data: TeamMemberCreate) -> TeamMember:
        """
        Create a new team member.

        Args:
            data: Team member creation data

        Returns:
            Created TeamMember instance

        Raises:
            HTTPException: 409 if email already exists
        """
        member = TeamMember(**data.model_dump())
        self.db.add(member)

        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Team member with email {data.email} already exists",
            )

        return member

    async def update_member(
        self, member_id: UUID, data: TeamMemberUpdate
    ) -> TeamMember:
        """
        Update a team member with partial data.

        Args:
            member_id: UUID of the team member
            data: Partial update data

        Returns:
            Updated TeamMember instance

        Raises:
            HTTPException: 404 if member not found, 409 if email conflict
        """
        member = await self.get_member(member_id)

        # Apply only provided fields
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(member, field, value)

        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Team member with email {data.email} already exists",
            )

        return member

    async def delete_member(self, member_id: UUID) -> None:
        """
        Delete a team member.

        Args:
            member_id: UUID of the team member

        Raises:
            HTTPException: 404 if member not found
        """
        member = await self.get_member(member_id)
        await self.db.delete(member)
        await self.db.flush()

    async def toggle_availability(self, member_id: UUID) -> TeamMember:
        """
        Toggle a team member's availability status.

        Args:
            member_id: UUID of the team member

        Returns:
            Updated TeamMember instance

        Raises:
            HTTPException: 404 if member not found
        """
        member = await self.get_member(member_id)
        member.is_available = not member.is_available
        await self.db.flush()
        return member

    async def list_absences(
        self,
        member_id: UUID | None = None,
        from_on: date | None = None,
        to_on: date | None = None,
    ) -> list[MemberAbsence]:
        """List absences, optionally narrowed to one member and/or date window."""
        stmt = select(MemberAbsence).order_by(MemberAbsence.starts_on)
        if member_id is not None:
            stmt = stmt.where(MemberAbsence.member_id == member_id)
        if from_on is not None:
            stmt = stmt.where(MemberAbsence.ends_on >= from_on)
        if to_on is not None:
            stmt = stmt.where(MemberAbsence.starts_on <= to_on)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def create_absence(self, member_id: UUID, data: AbsenceCreate) -> MemberAbsence:
        """Record an absence for a member (dates validated by the schema)."""
        await self.get_member(member_id)  # 404 when the member does not exist
        absence = MemberAbsence(member_id=member_id, **data.model_dump())
        self.db.add(absence)
        await self.db.flush()
        return absence

    async def delete_absence(self, absence_id: UUID) -> None:
        """Delete an absence.

        Raises:
            HTTPException: 404 if absence not found
        """
        result = await self.db.execute(
            select(MemberAbsence).where(MemberAbsence.id == absence_id)
        )
        absence = result.scalar_one_or_none()
        if absence is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Absence with id {absence_id} not found",
            )
        await self.db.delete(absence)
        await self.db.flush()
