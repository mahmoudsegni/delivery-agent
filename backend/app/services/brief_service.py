"""Morning Brief service: live-data snapshot → Groq-written executive brief.

Assembles a snapshot from the SAME sources the executive endpoints use (stream
rollups, portfolio trend, AI-workforce stats, yesterday's completions, latest
standup, pending decisions), has Groq write the brief as strict JSON, and
upserts one :class:`Brief` per day. ``generate`` is idempotent per date so the
beat job and the "generate now" button never duplicate a day's brief.
"""

import json
import logging
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tools import DatabaseTools
from app.models.brief import Brief
from app.models.proposal import Proposal
from app.models.standup import StandupRound
from app.services.confidence_service import ConfidenceService
from app.services.groq_client import groq_json
from app.services.stream_service import StreamService
from app.services.workforce_stats_service import WorkforceStatsService

logger = logging.getLogger(__name__)

# Telegram sendMessage caps at 4096 chars; stay comfortably under it.
_TELEGRAM_MAX_CHARS = 3500
# Webex caps a message at 7439 *bytes*; this slice counts characters, so leave
# headroom for multi-byte content (emoji, accents).
_WEBEX_MAX_CHARS = 7000

_BRIEF_SYSTEM_PROMPT = (
    "You are an AI delivery manager writing the daily morning brief for top "
    "management. Return ONLY a single valid JSON object. No markdown, no code "
    "fences, no prose outside the JSON. All strings must be plain text."
)


class BriefService:
    """Assembles the snapshot, generates, and formats the morning brief."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Snapshot assembly
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
        """Live delivery data the brief is written from (and nothing else)."""
        tools = DatabaseTools(self.db)
        confidence = ConfidenceService(self.db)
        stream_service = StreamService(self.db)

        streams = await stream_service.list_streams()
        stream_rollups: list[dict[str, Any]] = []
        for stream in streams:
            rollup = await confidence.get_stream_rollup(stream.id)
            stream_rollups.append({"name": stream.name, **rollup})

        return {
            "streams": stream_rollups,
            "portfolio_confidence": await confidence.get_portfolio_confidence(),
            "confidence_trend_7d": await confidence.get_portfolio_trend(7),
            "active_sprint": await tools.get_sprint_status(),
            "completed_last_day": await tools.get_completed_tasks(since_days=1),
            "ai_workforce": await WorkforceStatsService(self.db).get_stats(),
            "latest_standup": await self._latest_standup_summary(),
            "pending_proposals": await self._pending_proposal_counts(),
        }

    # ------------------------------------------------------------------
    # Generation (idempotent per date)
    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(on_date: date, snapshot: dict[str, Any]) -> str:
        return (
            f"Write the morning brief for {on_date.isoformat()} from the live "
            "delivery data below.\n\n"
            f"LIVE DELIVERY DATA:\n{json.dumps(snapshot, default=str)}\n\n"
            "Return ONLY a JSON object with this exact shape:\n"
            "{\n"
            '  "headline": "one-sentence executive headline",\n'
            '  "shipped": ["work completed in the last day"],\n'
            '  "ai_delivered": ["what the AI workforce shipped or has in flight"],\n'
            '  "risks": ["risk or declining-health statement"],\n'
            '  "needs_decision": ["decision waiting on the executive"],\n'
            '  "numbers": {"short label": "value"},\n'
            '  "narrative": "3-5 sentence plain-text executive narrative"\n'
            "}\n"
            "Use [] for empty lists and {} for empty numbers. Base every "
            "statement strictly on the data above. If the data is sparse or "
            "shows little activity, say so plainly — do not invent activity."
        )

    async def get_for_date(self, on_date: date) -> Brief | None:
        result = await self.db.execute(
            select(Brief).where(Brief.brief_date == on_date)
        )
        return result.scalar_one_or_none()

    async def latest(self) -> Brief | None:
        """The newest brief, or None when none exists yet."""
        result = await self.db.execute(
            select(Brief).order_by(Brief.brief_date.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def generate(
        self, on_date: date | None = None, force: bool = False
    ) -> Brief:
        """Upsert the brief for ``on_date`` (today by default).

        Idempotent per date: an existing brief is returned unchanged unless
        ``force`` is set (the "generate now" button), which rewrites it in
        place — same row, same id, so notification dedupe keys stay stable.
        The caller owns the commit.
        """
        on_date = on_date or date.today()
        brief = await self.get_for_date(on_date)
        if brief is not None and not force:
            return brief

        snapshot = await self.assemble_snapshot()
        data = await groq_json(
            self._build_prompt(on_date, snapshot), _BRIEF_SYSTEM_PROMPT, feature="brief"
        )

        if brief is None:
            brief = Brief(brief_date=on_date)
            self.db.add(brief)
        brief.headline = data.get("headline")
        brief.content = {
            "shipped": data.get("shipped", []),
            "ai_delivered": data.get("ai_delivered", []),
            "risks": data.get("risks", []),
            "needs_decision": data.get("needs_decision", []),
            "numbers": data.get("numbers", {}),
            "narrative": data.get("narrative", ""),
        }
        await self.db.flush()
        logger.info("Generated morning brief for %s (%s)", on_date, brief.id)
        return brief

    # ------------------------------------------------------------------
    # Telegram formatting (plain text — TelegramClient sends no parse_mode)
    # ------------------------------------------------------------------
    @staticmethod
    def to_telegram_text(brief: Brief) -> str:
        content = brief.content or {}
        lines: list[str] = [f"☀️ Morning brief — {brief.brief_date}"]
        if brief.headline:
            lines.append(brief.headline)

        sections = [
            ("Shipped", content.get("shipped") or []),
            ("AI workforce", content.get("ai_delivered") or []),
            ("Risks", content.get("risks") or []),
            ("Needs your decision", content.get("needs_decision") or []),
        ]
        for title, items in sections:
            if not items:
                continue
            lines.append("")
            lines.append(f"{title}:")
            lines.extend(f"• {item}" for item in items)

        numbers = content.get("numbers") or {}
        if numbers:
            lines.append("")
            lines.append(
                "Numbers: " + " · ".join(f"{k}: {v}" for k, v in numbers.items())
            )

        narrative = content.get("narrative")
        if narrative:
            lines.append("")
            lines.append(narrative)

        return "\n".join(lines)[:_TELEGRAM_MAX_CHARS]

    # ------------------------------------------------------------------
    # Webex formatting (real markdown — WebexClient sends the `markdown` field)
    # ------------------------------------------------------------------
    @staticmethod
    def to_webex_markdown(brief: Brief) -> str:
        content = brief.content or {}
        lines: list[str] = [f"☀️ **Morning brief — {brief.brief_date}**"]
        if brief.headline:
            lines.append(brief.headline)

        sections = [
            ("Shipped", content.get("shipped") or []),
            ("AI workforce", content.get("ai_delivered") or []),
            ("Risks", content.get("risks") or []),
            ("Needs your decision", content.get("needs_decision") or []),
        ]
        for title, items in sections:
            if not items:
                continue
            lines.append("")
            lines.append(f"**{title}**")
            lines.extend(f"- {item}" for item in items)

        numbers = content.get("numbers") or {}
        if numbers:
            lines.append("")
            lines.append(
                "**Numbers:** " + " · ".join(f"{k}: {v}" for k, v in numbers.items())
            )

        narrative = content.get("narrative")
        if narrative:
            lines.append("")
            lines.append(narrative)

        return "\n".join(lines)[:_WEBEX_MAX_CHARS]

    # ------------------------------------------------------------------
    # Plain markdown export (wiki/status-doc pasting — no transport cap)
    # ------------------------------------------------------------------
    @staticmethod
    def to_markdown(brief: Brief) -> str:
        content = brief.content or {}
        lines: list[str] = [f"# Morning brief — {brief.brief_date}"]
        if brief.headline:
            lines.append(f"**{brief.headline}**")

        sections = [
            ("Shipped", content.get("shipped") or []),
            ("AI workforce", content.get("ai_delivered") or []),
            ("Risks", content.get("risks") or []),
            ("Needs your decision", content.get("needs_decision") or []),
        ]
        for title, items in sections:
            if not items:
                continue
            lines.append(f"\n## {title}")
            lines.extend(f"- {item}" for item in items)

        numbers = content.get("numbers") or {}
        if numbers:
            lines.append("\n## Numbers")
            lines.extend(f"- {k}: {v}" for k, v in numbers.items())

        narrative = content.get("narrative")
        if narrative:
            lines.append(f"\n## Narrative\n{narrative}")

        return "\n".join(lines)
