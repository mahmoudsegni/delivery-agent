"""Standup collection + analysis service.

Owns the conversation logic (used by the Telegram bot), round lifecycle, and
the analysis step that feeds collected answers to the standup Deep Agent.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import standup_state
from app.models.proposal import Proposal
from app.models.sprint import Sprint
from app.models.standup import StandupEntry, StandupRound
from app.models.task import Task
from app.models.team import TeamMember
from app.services.groq_client import extract_json, groq_chat
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

# Ordered standup questions. Step name -> question text.
STEPS = ["yesterday", "today", "blockers"]
QUESTIONS = {
    "yesterday": "📋 Standup time! What did you complete **yesterday**?",
    "today": "What are you working on **today**?",
    "blockers": "Any **blockers** or concerns? (reply 'none' if all good)",
}
_NO_BLOCKER_WORDS = {"none", "no", "nope", "n/a", "na", "nothing"}


async def _analyze_standup_json(prompt: str) -> dict:
    """Call the shared Groq client for a structured standup analysis.

    One LLM choke point for the whole app: ``groq_chat`` owns the key check,
    the gpt-oss ``reasoning_effort`` handling, and the DEMO_MODE replay of
    recorded standup digests.
    """
    content = await groq_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are an AI delivery manager. Analyze the standup and return ONLY a "
                    "single valid JSON object. No markdown, no code fences, no prose outside "
                    "the JSON. The 'summary' field must be plain text (2-3 sentences, no "
                    "markdown)."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
        feature="standup",
    )
    return extract_json(content)


@dataclass
class AnswerResult:
    reply: str | None
    entry_completed: bool = False
    round_id: str | None = None
    round_complete: bool = False


class StandupService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------
    async def members_to_prompt(self) -> list[TeamMember]:
        """Available members.

        Webex addresses people by email and ``TeamMember.email`` is NOT NULL, so
        there is no linking step to gate on — every available member is prompted.
        Members without a Webex account surface as a skipped DM (see
        ``standup_tasks._send_question``) rather than being filtered here.
        """
        result = await self.db.execute(
            select(TeamMember).where(TeamMember.is_available.is_(True))
        )
        available = list(result.scalars().all())

        # The sync imports everyone who ever touched a work item. Only ask the
        # people who actually had work in the last three sprints.
        today = datetime.now(timezone.utc).date()
        recent = (
            await self.db.execute(
                select(Sprint.id)
                .where(Sprint.start_date <= today)
                .order_by(Sprint.start_date.desc())
                .limit(3)
            )
        ).scalars().all()
        if not recent:
            return available
        active_ids = set(
            (
                await self.db.execute(
                    select(Task.assignee_id)
                    .where(Task.sprint_id.in_(recent), Task.assignee_id.is_not(None))
                    .distinct()
                )
            ).scalars().all()
        )
        team = [m for m in available if m.id in active_ids]
        return team or available

    async def submit_entry(
        self,
        round_id: str,
        member_id: str,
        yesterday: str,
        today: str,
        blockers: str | None,
    ) -> bool:
        """Record a standup answered through the web form.

        Returns True when every expected member has now answered.
        Raises ValueError when the round is unknown or already closed.
        """
        round_ = await self.db.get(StandupRound, round_id)
        if round_ is None:
            raise ValueError("Standup round not found")
        if round_.status != "collecting":
            raise ValueError("This standup is already closed")
        member = await self.db.get(TeamMember, member_id)
        if member is None:
            raise ValueError("Team member not found")

        entry = (
            await self.db.execute(
                select(StandupEntry).where(
                    StandupEntry.round_id == round_id, StandupEntry.member_id == member_id
                )
            )
        ).scalar_one_or_none()
        if entry is None:  # someone outside the prompted list chose to answer
            entry = StandupEntry(round_id=round_id, member_id=member_id, raw_text={"messages": []})
            self.db.add(entry)

        clean_blockers = (blockers or "").strip()
        entry.yesterday = yesterday.strip()
        entry.today = today.strip()
        entry.blockers = (
            None if not clean_blockers or clean_blockers.lower() in _NO_BLOCKER_WORDS else clean_blockers
        )
        entry.raw_text = {**(entry.raw_text or {}), "source": "web"}
        entry.completed = True
        entry.submitted_at = datetime.now(timezone.utc)
        await self.db.commit()
        return await self._is_round_complete(round_id)

    async def link_member(
        self, code: str, chat_id: str, username: str | None
    ) -> str:
        """Resolve a one-time link code and attach this chat to a member."""
        if not code:
            return (
                "👋 To link your account, generate a code in the app and send "
                "/start <code>."
            )
        r = standup_state.get_redis()
        try:
            member_id = await standup_state.resolve_link_code(r, code)
        finally:
            await r.aclose()

        if not member_id:
            return "⚠️ That link code is invalid or expired. Generate a new one."

        result = await self.db.execute(
            select(TeamMember).where(TeamMember.id == member_id)
        )
        member = result.scalar_one_or_none()
        if member is None:
            return "⚠️ Member not found."

        member.telegram_chat_id = chat_id
        member.telegram_username = username
        await self.db.commit()
        return f"✅ Linked as {member.name}. You'll get standup prompts here."

    async def member_for_chat(self, chat_id: str) -> TeamMember | None:
        """The member linked to a Telegram chat, if any."""
        result = await self.db.execute(
            select(TeamMember).where(TeamMember.telegram_chat_id == chat_id)
        )
        return result.scalar_one_or_none()

    async def create_link_code(self, member_id: UUID) -> str:
        """Create a one-time code a member uses to link Telegram."""
        r = standup_state.get_redis()
        try:
            return await standup_state.create_link_code(r, str(member_id))
        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    # Round lifecycle
    # ------------------------------------------------------------------
    async def start_round(
        self, trigger: str = "manual"
    ) -> tuple[StandupRound, list[TeamMember]]:
        """Create a round + an entry per linked member and arm conversations.

        Returns (round, members). The caller is responsible for enqueuing the
        first-question DMs (keeps Webex I/O off the request path).
        """
        members = await self.members_to_prompt()
        round_ = StandupRound(
            status="collecting",
            trigger=trigger,
            expected_count=len(members),
            scheduled_for=datetime.now(timezone.utc).date()
            if trigger == "scheduled"
            else None,
        )
        self.db.add(round_)
        await self.db.flush()  # get round_.id

        r = standup_state.get_redis()
        try:
            for member in members:
                entry = StandupEntry(
                    round_id=round_.id,
                    member_id=member.id,
                    raw_text={"messages": []},
                )
                self.db.add(entry)
                await self.db.flush()  # get entry.id
                # Keyed by email: that is what the Webex webhook reports as
                # ``personEmail`` when the member replies (inbound, phase 2).
                await standup_state.set_convo(
                    r,
                    member.email,
                    {"round_id": round_.id, "entry_id": entry.id, "step": STEPS[0]},
                )
        finally:
            await r.aclose()

        await self.db.commit()
        return round_, members

    async def record_answer(self, chat_id: str, text: str) -> AnswerResult:
        """Record an inbound message as the answer to the current question."""
        r = standup_state.get_redis()
        try:
            convo = await standup_state.get_convo(r, chat_id)
            if not convo:
                return AnswerResult(
                    reply="There's no active standup for you right now. 🙂"
                )

            step = convo["step"]
            entry = await self.db.get(StandupEntry, convo["entry_id"])
            if entry is None:
                await standup_state.clear_convo(r, chat_id)
                return AnswerResult(reply=None)

            # Append raw message (audit trail).
            raw = entry.raw_text or {"messages": []}
            raw.setdefault("messages", []).append({"step": step, "text": text})
            entry.raw_text = raw

            # Store the parsed answer.
            if step == "blockers" and text.strip().lower() in _NO_BLOCKER_WORDS:
                entry.blockers = None
            else:
                setattr(entry, step, text)

            # Advance.
            idx = STEPS.index(step)
            if idx + 1 < len(STEPS):
                next_step = STEPS[idx + 1]
                convo["step"] = next_step
                await standup_state.set_convo(r, chat_id, convo)
                await self.db.commit()
                return AnswerResult(reply=QUESTIONS[next_step], round_id=entry.round_id)

            # Last step -> complete this entry.
            entry.completed = True
            entry.submitted_at = datetime.now(timezone.utc)
            await standup_state.clear_convo(r, chat_id)
            await self.db.commit()

            round_complete = await self._is_round_complete(entry.round_id)
            return AnswerResult(
                reply="✅ Thanks, your standup is recorded!",
                entry_completed=True,
                round_id=entry.round_id,
                round_complete=round_complete,
            )
        finally:
            await r.aclose()

    async def mark_skipped(self, chat_id: str, reason: str) -> str:
        """Mark the linked member out for the open collecting round (issue #31).

        Idempotent via select-then-update (the ``submit_entry`` pattern):
        ``start_round`` pre-creates the (round, member) row, so a blind
        merge/insert would trip ``uq_standup_entry_round_member``.
        """
        member = await self.member_for_chat(chat_id)
        if member is None:
            return "This chat isn't linked yet — send /start <code>."
        round_ = (
            await self.db.execute(
                select(StandupRound)
                .where(StandupRound.status == "collecting")
                .order_by(StandupRound.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if round_ is None:
            return "No open standup round right now."
        entry = (
            await self.db.execute(
                select(StandupEntry).where(
                    StandupEntry.round_id == round_.id,
                    StandupEntry.member_id == member.id,
                )
            )
        ).scalar_one_or_none()
        if entry is None:  # member wasn't in the prompted list
            entry = StandupEntry(round_id=round_.id, member_id=member.id)
            self.db.add(entry)
        entry.raw_text = {
            **(entry.raw_text or {}),
            "skipped": True,
            "reason": reason.strip() or "out today",
        }
        await self.db.commit()
        return "You're marked out for today's standup — no more nudges 🏖"

    async def _is_round_complete(self, round_id: str) -> bool:
        result = await self.db.execute(
            select(StandupEntry.completed).where(StandupEntry.round_id == round_id)
        )
        flags = [row[0] for row in result.all()]
        return bool(flags) and all(flags)

    async def get_round(self, round_id: UUID) -> StandupRound | None:
        return await self.db.get(StandupRound, str(round_id))

    async def get_entries(self, round_id: str) -> list[StandupEntry]:
        result = await self.db.execute(
            select(StandupEntry).where(StandupEntry.round_id == round_id)
        )
        return list(result.scalars().all())

    async def list_rounds(self, limit: int = 20) -> list[StandupRound]:
        result = await self.db.execute(
            select(StandupRound).order_by(StandupRound.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    async def close_and_analyze(self, round_id: str) -> StandupRound:
        """Close collection and analyze the real answers into a structured report.

        Idempotent: a round already 'analyzed' is returned unchanged.
        """
        round_ = await self.db.get(StandupRound, round_id)
        if round_ is None:
            raise ValueError(f"Standup round not found: {round_id}")
        if round_.status == "analyzed":
            return round_

        entries = await self.get_entries(round_id)
        # Resolve member names.
        member_ids = [e.member_id for e in entries]
        names: dict[str, str] = {}
        if member_ids:
            res = await self.db.execute(
                select(TeamMember.id, TeamMember.name).where(
                    TeamMember.id.in_(member_ids)
                )
            )
            names = {mid: name for mid, name in res.all()}

        member_blocks = []
        member_summaries = []
        for e in entries:
            name = names.get(e.member_id, "Unknown")
            skipped = bool((e.raw_text or {}).get("skipped"))
            if skipped:  # /skip member (issue #31): out today, not missing
                member_blocks.append(
                    f"{name}: ⛰ out today ({(e.raw_text or {}).get('reason', 'out today')})"
                )
            else:
                member_blocks.append(
                    f"{name}:\n"
                    f"  Yesterday: {e.yesterday or '(no response)'}\n"
                    f"  Today: {e.today or '(no response)'}\n"
                    f"  Blockers: {e.blockers or 'None'}"
                )
            member_summaries.append(
                {
                    "member": name,
                    "yesterday": e.yesterday,
                    "today": e.today,
                    "blockers": e.blockers,
                    "responded": e.completed,
                    "skipped": skipped,
                }
            )

        task_summary = await TaskService(self.db).get_summary()

        prompt = (
            "Analyze today's standup. For each team member I provide "
            "yesterday/today/blockers.\n\n"
            f"TEAM UPDATES:\n{chr(10).join(member_blocks) or '(no responses)'}\n\n"
            f"WORK ITEMS SUMMARY: {task_summary}\n\n"
            "Return ONLY a JSON object with this exact shape:\n"
            "{\n"
            '  "blockers": [{"member": "", "issue": "", "severity": "High|Medium|Low", "suggestedAction": ""}],\n'
            '  "risks": ["short risk statement"],\n'
            '  "recommendations": ["actionable recommendation"],\n'
            '  "summary": "2-3 sentence plain-text overview of team status"\n'
            "}\n"
            "Use [] for empty lists. Base blockers only on what members actually reported."
        )

        # Create the proposal up-front (draft), then fill from the analysis.
        proposal = Proposal(
            type="standup",
            state="draft",
            content={"prompt_members": member_summaries},
        )
        self.db.add(proposal)
        await self.db.flush()

        try:
            data = await _analyze_standup_json(prompt)
            summary = {
                "blockers": data.get("blockers", []),
                "risks": data.get("risks", []),
                "recommendations": data.get("recommendations", []),
                "summary": data.get("summary", ""),
                "members": member_summaries,
            }
            proposal.state = "pending_review"
            proposal.content = summary
            proposal.reasoning = "Standup analysis over collected team updates"
            proposal.confidence = "high"
            proposal.created_by_agent = "StandupAgent"

            round_.proposal_id = proposal.id
            round_.summary = summary
            round_.status = "analyzed"
            round_.closed_at = datetime.now(timezone.utc)
            await self.db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Standup analysis failed for round %s", round_id)
            round_.status = "failed"
            round_.summary = {"error": str(exc)[:500], "members": member_summaries}
            round_.closed_at = datetime.now(timezone.utc)
            await self.db.commit()

        return round_
