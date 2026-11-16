"""Sprint API endpoints."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent import ProposalResponse
from app.database import get_db
from app.schemas.sprint import (
    SprintBurndown,
    SprintCreate,
    SprintListResponse,
    SprintResponse,
    SprintUpdate,
    SprintWithStats,
    TaskToSprintRequest,
)
from app.schemas.task import TaskListResponse, TaskResponse
from app.services.forecast_service import forecast
from app.services.sprint_service import (
    PendingReleaseNotesError,
    SprintNotFoundError,
    SprintService,
)

router = APIRouter(prefix="/sprints", tags=["sprints"])
logger = logging.getLogger(__name__)


@router.get("", response_model=SprintListResponse)
async def list_sprints(
    skip: int = 0,
    limit: int = 50,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> SprintListResponse:
    """List all sprints with optional filtering."""
    service = SprintService(db)
    sprints, total = await service.list_sprints(skip=skip, limit=limit, status=status)

    return SprintListResponse(
        sprints=[SprintResponse.model_validate(s) for s in sprints],
        total=total,
    )


@router.post("", response_model=SprintResponse, status_code=status.HTTP_201_CREATED)
async def create_sprint(
    data: SprintCreate,
    db: AsyncSession = Depends(get_db),
) -> SprintResponse:
    """Create a new sprint (end date defaults to a two-week window)."""
    if data.end_date is not None and data.end_date < data.start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    service = SprintService(db)
    sprint = await service.create_sprint(data)
    await db.commit()

    return SprintResponse.model_validate(sprint)


@router.get("/active", response_model=SprintWithStats | None)
async def get_active_sprint(
    db: AsyncSession = Depends(get_db),
) -> SprintWithStats | None:
    """Get the currently active sprint with statistics."""
    service = SprintService(db)
    sprint = await service.get_active_sprint()

    if sprint is None:
        return None

    return await service.get_sprint_with_stats(sprint.id)


@router.get("/{sprint_id}", response_model=SprintWithStats)
async def get_sprint(
    sprint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> SprintWithStats:
    """Get sprint by ID with statistics."""
    service = SprintService(db)
    try:
        return await service.get_sprint_with_stats(sprint_id)
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )


@router.patch("/{sprint_id}", response_model=SprintResponse)
async def update_sprint(
    sprint_id: UUID,
    data: SprintUpdate,
    db: AsyncSession = Depends(get_db),
) -> SprintResponse:
    """Update a sprint."""
    service = SprintService(db)
    try:
        sprint = await service.update_sprint(sprint_id, data)
        await db.commit()
        return SprintResponse.model_validate(sprint)
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )


@router.delete("/{sprint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sprint(
    sprint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a sprint."""
    service = SprintService(db)
    try:
        await service.delete_sprint(sprint_id)
        await db.commit()
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )


@router.post("/{sprint_id}/activate", response_model=SprintResponse)
async def activate_sprint(
    sprint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> SprintResponse:
    """Activate a sprint (change from planning to active)."""
    service = SprintService(db)
    try:
        sprint = await service.activate_sprint(sprint_id)
        await db.commit()
        return SprintResponse.model_validate(sprint)
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/{sprint_id}/complete", response_model=SprintResponse)
async def complete_sprint(
    sprint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> SprintResponse:
    """Complete a sprint (calculates velocity)."""
    service = SprintService(db)
    try:
        sprint = await service.complete_sprint(sprint_id)
        await db.commit()
        return SprintResponse.model_validate(sprint)
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/{sprint_id}/release-notes",
    response_model=ProposalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def draft_release_notes(
    sprint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> ProposalResponse:
    """Draft release notes from a sprint's done work → pending_review proposal."""
    service = SprintService(db)
    try:
        proposal = await service.draft_release_notes(sprint_id)
        await db.commit()
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )
    except PendingReleaseNotesError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    return ProposalResponse.model_validate(proposal, from_attributes=True)


@router.get("/{sprint_id}/tasks", response_model=TaskListResponse)
async def get_sprint_tasks(
    sprint_id: UUID,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> TaskListResponse:
    """Get all tasks in a sprint."""
    service = SprintService(db)
    try:
        tasks = await service.get_sprint_tasks(sprint_id, status=status)
        return TaskListResponse(
            tasks=[TaskResponse.model_validate(t) for t in tasks],
            total=len(tasks),
        )
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )


@router.post("/{sprint_id}/tasks", response_model=TaskListResponse)
async def add_tasks_to_sprint(
    sprint_id: UUID,
    data: TaskToSprintRequest,
    db: AsyncSession = Depends(get_db),
) -> TaskListResponse:
    """Add tasks to a sprint."""
    service = SprintService(db)
    try:
        tasks = await service.add_tasks_to_sprint(sprint_id, data.task_ids)
        await db.commit()
        return TaskListResponse(
            tasks=[TaskResponse.model_validate(t) for t in tasks],
            total=len(tasks),
        )
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )


@router.delete("/{sprint_id}/tasks", response_model=TaskListResponse)
async def remove_tasks_from_sprint(
    sprint_id: UUID,
    data: TaskToSprintRequest,
    db: AsyncSession = Depends(get_db),
) -> TaskListResponse:
    """Remove tasks from a sprint (move back to backlog)."""
    service = SprintService(db)
    try:
        tasks = await service.remove_tasks_from_sprint(sprint_id, data.task_ids)
        await db.commit()
        return TaskListResponse(
            tasks=[TaskResponse.model_validate(t) for t in tasks],
            total=len(tasks),
        )
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )


@router.get("/{sprint_id}/forecast")
async def get_sprint_forecast(
    sprint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Monte Carlo completion forecast: p50/p85 days-to-done + on-time odds."""
    try:
        return await forecast(db, sprint_id)
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )


@router.get("/{sprint_id}/burndown", response_model=SprintBurndown)
async def get_sprint_burndown(
    sprint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> SprintBurndown:
    """Get burndown chart data for a sprint."""
    service = SprintService(db)
    try:
        return await service.get_burndown(sprint_id)
    except SprintNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sprint not found: {sprint_id}",
        )
