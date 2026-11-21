"""Ask-the-Delivery-Manager: Q&A over a server-assembled live-data snapshot.

The snapshot is rebuilt per question from the SAME async sources the executive
endpoints use (never the broken sync tool bridge), embedded in the system
prompt, and answered in one ``groq_chat`` call with the last few conversation
turns for continuity. No tool loop yet — documented future work.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tools import DatabaseTools
from app.models.proposal import Proposal
from app.models.standup import StandupRound
from app.services.confidence_service import ConfidenceService
from app.services.groq_client import groq_chat
from app.services.stream_service import StreamService
from app.services.workforce_stats_service import WorkforceStatsService

logger = logging.getLogger(__name__)

# Only the trailing turns ride along — the snapshot dominates the budget.
_HISTORY_WINDOW = 6


class AskService:
    """Assembles the live snapshot and answers executive questions over it."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Snapshot assembly (async DB reads only)
    # ------------------------------------------------------------------
    async def _latest_standup_summary(self) -> dict[str, Any] | None:
        result = await self.db.execute(
            select(StandupRound)
            .where(StandupRound.status == "analyzed")
            .order_by(StandupRound.created_at.desc())
            .limit(1)
        )
        round_ = result.scalar_one_or_none()
        return round_.summary if round_ else None

    async def _pending_proposal_counts(self) -> dict[str, int]:
        result = await self.db.execute(
            select(Proposal.type, func.count(Proposal.id))
            .where(Proposal.state == "pending_review")
            .group_by(Proposal.type)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def assemble_snapshot(self) -> dict[str, Any]:
        """Everything the delivery manager is allowed to answer from."""
        tools = DatabaseTools(self.db)
        confidence = ConfidenceService(self.db)
        stream_service = StreamService(self.db)

        streams = await stream_service.list_streams()
        stream_rollups: list[dict[str, Any]] = []
        for stream in streams:
            rollup = await confidence.get_stream_rollup(stream.id)
            stream_rollups.append({"id": stream.id, "name": stream.name, **rollup})

        return {
            "streams": stream_rollups,
            "portfolio_confidence": await confidence.get_portfolio_confidence(),
            "confidence_trend_14d": await confidence.get_portfolio_trend(14),
            "active_sprint": await tools.get_sprint_status(),
            "team_capacity": await tools.get_team_capacity(),
            "completed_last_7_days": await tools.get_completed_tasks(since_days=7),
            "backlog_top": await tools.get_backlog_items(limit=15),
            "ai_workforce": await WorkforceStatsService(self.db).get_stats(),
            "latest_standup": await self._latest_standup_summary(),
            "pending_proposals": await self._pending_proposal_counts(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Answering
    # ------------------------------------------------------------------
    @staticmethod
    def _system_prompt(snapshot: dict[str, Any]) -> str:
        return (
            "You are the delivery manager for this organization, answering a "
            "busy executive. Answer ONLY from the LIVE DATA below — if the data "
            "does not contain the answer, say so plainly instead of guessing. "
            "Be concise and use an executive tone: lead with the answer, then "
            "the evidence. Format with simple markdown only — **bold** for "
            "emphasis and '-' bullet lists; no tables, no links, no code blocks.\n\n"
            f"LIVE DATA:\n{json.dumps(snapshot, default=str)}"
        )

    async def answer(
        self, question: str, history: list[dict[str, Any]] | None = None
    ) -> str:
        """One-shot answer over a fresh snapshot + the last few turns."""
        snapshot = await self.assemble_snapshot()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt(snapshot)}
        ]
        for msg in (history or [])[-_HISTORY_WINDOW:]:
            role = msg.get("role")
            content = (msg.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})
        return await groq_chat(messages, feature="ask")
