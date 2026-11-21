"""Team management API endpoints."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.member_absence import MemberAbsence
from app.models.sprint import Sprint
from app.models.team import TeamMember
from app.schemas.team import (
    AbsenceCreate,
    AbsenceListResponse,
    AbsenceResponse,
    TeamMemberCreate,
    TeamMemberListResponse,
    TeamMemberResponse,
    TeamMemberUpdate,
)
from app.services.team_service import TeamService, build_ics

router = APIRouter(prefix="/team", tags=["team"])


@router.get("/calendar.ics")
async def team_calendar_ics(db: AsyncSession = Depends(get_db)) -> Response:
    """ICS feed of sprint windows + member time off for Outlook/Google.

    Calendar apps can't do the passcode cookie unlock; expose `ICS_TOKEN`
    and subscribe at `/api/v1/team/calendar.ics?token=...`.
    """
    sprints = (
        (await db.execute(select(Sprint).order_by(Sprint.start_date)))
        .scalars()
        .all()
    )
    absences = (
        await db.execute(
            select(
                MemberAbsence.id,
                MemberAbsence.starts_on,
                MemberAbsence.ends_on,
                MemberAbsence.note,
                TeamMember.name.label("member_name"),
            )
            .join(TeamMember, MemberAbsence.member_id == TeamMember.id)
            .order_by(MemberAbsence.starts_on)
        )
    ).all()
    return Response(
        content=build_ics(sprints, absences),
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="team.ics"'},
    )


@router.get("/members", response_model=TeamMemberListResponse)
async def list_team_members(
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> TeamMemberListResponse:
    """
    List all team members with pagination.

    Args:
        skip: Number of records to skip (default 0)
        limit: Maximum number of records to return (default 50)
        db: Database session

    Returns:
        List of team members with total count
    """
    service = TeamService(db)
    members, total = await service.list_members(skip=skip, limit=limit)
    return TeamMemberListResponse(
        members=[TeamMemberResponse.model_validate(m) for m in members],
        total=total,
    )


@router.post(
    "/members",
    response_model=TeamMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_team_member(
    data: TeamMemberCreate,
    db: AsyncSession = Depends(get_db),
) -> TeamMemberResponse:
    """
    Create a new team member.

    Args:
        data: Team member creation data
        db: Database session

    Returns:
        Created team member

    Raises:
        409: If email already exists
        422: If validation fails
    """
    service = TeamService(db)
    member = await service.create_member(data)
    return TeamMemberResponse.model_validate(member)


@router.get("/members/{member_id}", response_model=TeamMemberResponse)
async def get_team_member(
    member_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> TeamMemberResponse:
    """
    Get a team member by ID.

    Args:
        member_id: UUID of the team member
        db: Database session

    Returns:
        Team member details

    Raises:
        404: If member not found
    """
    service = TeamService(db)
    member = await service.get_member(member_id)
    return TeamMemberResponse.model_validate(member)


@router.patch("/members/{member_id}", response_model=TeamMemberResponse)
async def update_team_member(
    member_id: UUID,
    data: TeamMemberUpdate,
    db: AsyncSession = Depends(get_db),
) -> TeamMemberResponse:
    """
    Update a team member with partial data.

    Args:
        member_id: UUID of the team member
        data: Partial update data
        db: Database session

    Returns:
        Updated team member

    Raises:
        404: If member not found
        409: If email already exists
        422: If validation fails
    """
    service = TeamService(db)
    member = await service.update_member(member_id, data)
    return TeamMemberResponse.model_validate(member)


@router.delete(
    "/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_team_member(
    member_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Delete a team member.

    Args:
        member_id: UUID of the team member
        db: Database session

    Raises:
        404: If member not found
    """
    service = TeamService(db)
    await service.delete_member(member_id)


@router.post(
    "/members/{member_id}/toggle-availability",
    response_model=TeamMemberResponse,
)
async def toggle_team_member_availability(
    member_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> TeamMemberResponse:
    """
    Toggle a team member's availability status.

    Args:
        member_id: UUID of the team member
        db: Database session

    Returns:
        Updated team member with toggled availability

    Raises:
        404: If member not found
    """
    service = TeamService(db)
    member = await service.toggle_availability(member_id)
    return TeamMemberResponse.model_validate(member)


@router.get("/absences", response_model=AbsenceListResponse)
async def list_absences(
    member_id: UUID | None = None,
    from_on: date | None = Query(default=None, alias="from"),
    to_on: date | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
) -> AbsenceListResponse:
    """
    List absences, optionally narrowed to one member and/or a date window.

    Args:
        member_id: Optional UUID to filter by member
        from_on: Optional window start (alias "from"); keeps absences ending on/after it
        to_on: Optional window end (alias "to"); keeps absences starting on/before it
        db: Database session

    Returns:
        Absences matching the filters
    """
    service = TeamService(db)
    absences = await service.list_absences(member_id=member_id, from_on=from_on, to_on=to_on)
    return AbsenceListResponse(
        absences=[AbsenceResponse.model_validate(a) for a in absences]
    )


@router.post(
    "/members/{member_id}/absences",
    response_model=AbsenceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_absence(
    member_id: UUID,
    data: AbsenceCreate,
    db: AsyncSession = Depends(get_db),
) -> AbsenceResponse:
    """
    Record an absence for a team member.

    Args:
        member_id: UUID of the team member
        data: Inclusive date range (validated starts_on <= ends_on) + optional note
        db: Database session

    Returns:
        Created absence

    Raises:
        404: If member not found
        422: If dates are invalid
    """
    service = TeamService(db)
    absence = await service.create_absence(member_id, data)
    return AbsenceResponse.model_validate(absence)


@router.delete(
    "/absences/{absence_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_absence(
    absence_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Delete an absence.

    Args:
        absence_id: UUID of the absence
        db: Database session

    Raises:
        404: If absence not found
    """
    service = TeamService(db)
    await service.delete_absence(absence_id)
