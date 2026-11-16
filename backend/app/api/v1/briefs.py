"""Morning-brief endpoints: list, latest, generate-now.

``POST /briefs/generate`` runs the Groq generation synchronously (the UI shows
a pending state) and only the Webex push goes through Celery — so the user
sees the brief the moment the call returns.
"""

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.brief import Brief
from app.services.brief_service import BriefService
from app.services.groq_client import GroqError
from app.tasks.brief_tasks import push_brief_to_chat

router = APIRouter(prefix="/briefs", tags=["briefs"])
logger = logging.getLogger(__name__)


class BriefResponse(BaseModel):
    id: str
    brief_date: date
    headline: str | None
    content: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def of(cls, brief: Brief) -> "BriefResponse":
        return cls(
            id=brief.id,
            brief_date=brief.brief_date,
            headline=brief.headline,
            content=brief.content,
            created_at=brief.created_at,
            updated_at=brief.updated_at,
        )


@router.get("", response_model=list[BriefResponse])
async def list_briefs(
    limit: int = 30, db: AsyncSession = Depends(get_db)
) -> list[BriefResponse]:
    """Recent briefs, newest first (the Briefs page rail)."""
    result = await db.execute(
        select(Brief).order_by(Brief.brief_date.desc()).limit(limit)
    )
    return [BriefResponse.of(b) for b in result.scalars().all()]


@router.get("/latest", response_model=BriefResponse | None)
async def get_latest_brief(
    db: AsyncSession = Depends(get_db),
) -> BriefResponse | None:
    """The newest brief, or null when none exists yet (notification poller)."""
    brief = await BriefService(db).latest()
    return BriefResponse.of(brief) if brief else None


@router.get("/{brief_id}/markdown", response_class=PlainTextResponse)
async def get_brief_markdown(
    brief_id: str, db: AsyncSession = Depends(get_db)
) -> PlainTextResponse:
    """A brief as plain markdown — for pasting into wikis and status docs."""
    result = await db.execute(select(Brief).where(Brief.id == brief_id))
    brief = result.scalar_one_or_none()
    if brief is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brief not found"
        )
    return PlainTextResponse(
        BriefService.to_markdown(brief), media_type="text/markdown"
    )


@router.post("/generate", response_model=BriefResponse)
async def generate_brief(db: AsyncSession = Depends(get_db)) -> BriefResponse:
    """Generate (or regenerate) today's brief now, then push it to Webex.

    Synchronous by design: the caller gets the finished brief back. The
    Webex push is queued so a slow/unconfigured bot never blocks the UI.
    """
    if not settings.groq_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Groq API key not configured. Set GROQ_API_KEY environment variable.",
        )
    try:
        brief = await BriefService(db).generate(force=True)
    except (GroqError, ValueError) as exc:
        logger.error("Brief generation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Brief generation failed: {exc}",
        )
    await db.commit()

    push_brief_to_chat.delay(brief.id)
    return BriefResponse.of(brief)
