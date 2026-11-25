"""The agent's three analyses — sprint planning, blocker detection, release
assessment — computed over the LIVE delivery data synced from Azure DevOps.

Design rule: the facts are computed here, deterministically, from the DB
(who is overloaded, what is on hold, how long something has sat in a state,
which bugs are open). The LLM only adds judgement on top — ranking, suggested
actions, a short narrative — and every id it returns is validated against the
real items. If the LLM is unavailable the report still ships with the facts
and says so; it never falls back to invented data.

Staleness is measured against the data's own freshness (the newest ADO
``ChangedDate``), not the wall clock, so an older TFS dump doesn't make every
item look abandoned.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.proposal import Proposal
from app.models.sprint import Sprint
from app.models.task import Task
from app.models.team import TeamMember
from app.services.groq_client import GroqError, groq_json
from app.services.team_service import effective_capacity_hours, sprint_absences

logger = logging.getLogger(__name__)

BUG_TYPES = {"Bug", "Issue", "Impediment"}
FEATURE_TYPES = {"Product Backlog Item", "User Story", "Requirement"}
# Long-lived containers: being "in progress" for weeks is normal for them.
CONTAINER_TYPES = {"Feature", "Epic", "Release"}
# Plannable = what a sprint plan picks; ADO Tasks are children of these.
PLANNABLE_TYPES = FEATURE_TYPES | BUG_TYPES
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def work_item_url(task: Task) -> str | None:
    """Deep link to the work item in Azure DevOps (None for local tasks)."""
    if not (task.external_id and settings.tfs_url and settings.tfs_project):
        return None
    return (
        f"{settings.tfs_url.rstrip('/')}/{quote(settings.tfs_project)}"
        f"/_workitems/edit/{task.external_id}"
    )


def item_ref(task: Task) -> str:
    return f"#{task.external_id}" if task.external_id else f"#{task.id[:8]}"


def _is_on_hold(task: Task) -> bool:
    return (task.external_state or "").strip().lower() == "on hold"


def _is_tagged_blocked(task: Task) -> bool:
    return "block" in (task.tags or "").lower()


def _is_plannable(task: Task) -> bool:
    # Local tasks carry no type; treat them as plannable work.
    return task.work_item_type is None or task.work_item_type in PLANNABLE_TYPES


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AgentAnalysisService:
    """Runs one analysis, persists it as a pending_review proposal."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.stale_days = max(1, int(settings.autopilot_stale_task_days))

    # ------------------------------------------------------------------
    # Shared context
    # ------------------------------------------------------------------
    async def _data_as_of(self) -> datetime:
        """Newest ADO change we hold — 'now' for staleness purposes."""
        latest = (await self.db.execute(select(func.max(Task.external_changed_at)))).scalar()
        now = datetime.now(timezone.utc)
        latest = _aware(latest)
        return min(now, latest) if latest else now

    async def _sprints(self) -> list[Sprint]:
        return list(
            (await self.db.execute(select(Sprint).order_by(Sprint.start_date.desc()))).scalars()
        )

    @staticmethod
    def _current_sprint(sprints: list[Sprint], today: date) -> Sprint | None:
        for s in sprints:
            if s.status == "active":
                return s
        started = [s for s in sprints if s.start_date <= today]
        return started[0] if started else None

    @staticmethod
    def _release_candidate(sprints: list[Sprint], today: date) -> Sprint | None:
        """The sprint whose release readiness matters right now: the running
        one once it is at least half done, otherwise the one that just ended
        (a sprint on day one is always '0% — not ready', which says nothing)."""
        started = [s for s in sprints if s.start_date <= today]
        if not started:
            return None
        current = started[0]
        length = max(1, (current.end_date - current.start_date).days)
        if (today - current.start_date).days * 2 >= length or len(started) == 1:
            return current
        return started[1]

    @staticmethod
    def _next_sprint(sprints: list[Sprint], today: date) -> Sprint | None:
        upcoming = [s for s in sprints if s.start_date > today]
        return min(upcoming, key=lambda s: s.start_date) if upcoming else None

    async def _members(self) -> dict[str, TeamMember]:
        rows = (await self.db.execute(select(TeamMember))).scalars().all()
        return {m.id: m for m in rows}

    async def _team_pool(self, sprints: list[Sprint], today: date) -> set[str]:
        """People who actually had work in the last three sprints."""
        recent = [s.id for s in sprints if s.start_date <= today][:3]
        if not recent:
            return set()
        rows = await self.db.execute(
            select(Task.assignee_id)
            .where(Task.sprint_id.in_(recent), Task.assignee_id.is_not(None))
            .distinct()
        )
        return {r[0] for r in rows.all()}

    async def _open_load(self, sprint_id: str | None) -> dict[str, dict[str, int]]:
        """Per member: open (not done) items + estimated hours in a sprint."""
        load: dict[str, dict[str, int]] = defaultdict(lambda: {"items": 0, "hours": 0})
        if not sprint_id:
            return load
        rows = (
            await self.db.execute(
                select(Task).where(
                    Task.sprint_id == sprint_id,
                    Task.status != "done",
                    Task.assignee_id.is_not(None),
                )
            )
        ).scalars()
        for t in rows:
            load[t.assignee_id]["items"] += 1
            load[t.assignee_id]["hours"] += t.estimated_hours or 0
        return load

    async def _save(
        self, kind: str, summary: str, report: dict[str, Any], confidence: str
    ) -> str:
        proposal = Proposal(
            type=kind,
            state="pending_review",
            content={"content": summary, "report": report},
            reasoning=summary,
            confidence=confidence,
            created_by_agent="delivery-agent",
        )
        self.db.add(proposal)
        await self.db.commit()
        await self.db.refresh(proposal)
        return str(proposal.id)

    @staticmethod
    async def _ai(prompt: str, system: str) -> tuple[dict[str, Any], str | None]:
        """LLM judgement on top of the facts. Returns (json, error_note)."""
        try:
            return await groq_json(prompt, system), None
        except GroqError as exc:
            logger.warning("Agent analysis: AI step unavailable: %s", exc)
            return {}, f"AI suggestions unavailable ({exc}). Facts below are from live data."

    # ------------------------------------------------------------------
    # Blocker detection
    # ------------------------------------------------------------------
    async def detect_blockers_data(self) -> dict[str, Any]:
        """Pure blocker/stuck facts: heuristic detection only — no LLM, no persistence."""
        as_of = await self._data_as_of()
        members = await self._members()
        sprints = await self._sprints()
        current = self._current_sprint(sprints, as_of.date())
        load = await self._open_load(current.id if current else None)

        # Prorate overload flags by dated absences in the current sprint.
        absences_by_member: dict[str, list] = defaultdict(list)
        if current:
            for a in await sprint_absences(self.db, current.start_date, current.end_date):
                absences_by_member[a.member_id].append(a)

        def effective_cap(member: TeamMember) -> int:
            return effective_capacity_hours(
                member, current, absences_by_member.get(member.id, [])
            )

        candidates = (
            await self.db.execute(
                select(Task).where(
                    Task.status.in_(("in_progress", "review")),
                )
            )
        ).scalars().all()

        # Who has finished work in each sub-area — the natural people to help.
        helper_rows = await self.db.execute(
            select(Task.project_name, Task.assignee_id, func.count(Task.id))
            .where(Task.status == "done", Task.assignee_id.is_not(None))
            .group_by(Task.project_name, Task.assignee_id)
        )
        experts: dict[str | None, list[tuple[str, int]]] = defaultdict(list)
        for project, member_id, n in helper_rows.all():
            experts[project].append((member_id, int(n)))

        def helper_for(task: Task) -> str | None:
            options = [
                (mid, n) for mid, n in experts.get(task.project_name, [])
                if mid != task.assignee_id and mid in members
            ]
            if not options:
                return None
            # Most experienced in the area, then least loaded right now.
            options.sort(key=lambda o: (-o[1], load[o[0]]["hours"]))
            return members[options[0][0]].name

        blockers: list[dict[str, Any]] = []
        for t in candidates:
            if t.work_item_type in CONTAINER_TYPES:
                continue
            since = _aware(t.state_changed_at) or _aware(t.external_changed_at) or _aware(t.updated_at)
            days = max(0, (as_of - since).days) if since else 0
            threshold = self.stale_days * (2 if t.work_item_type in FEATURE_TYPES else 1)
            owner = members.get(t.assignee_id) if t.assignee_id else None

            kind = severity = None
            description = ""
            if _is_on_hold(t):
                kind, severity = "On Hold", "High"
                description = t.hold_reason or "Marked On Hold in Azure DevOps (no reason given)."
            elif _is_tagged_blocked(t):
                kind, severity = "Tagged blocked", "High"
                description = f"Tagged '{t.tags}' in Azure DevOps."
            elif t.status == "in_progress" and owner is None:
                kind, severity = "No owner", "Medium"
                description = "In progress but nobody is assigned."
            elif t.status == "in_progress" and days >= threshold:
                kind = "Stuck in progress"
                severity = "High" if days >= threshold * 2 else "Medium"
                description = f"In '{t.external_state or 'In Progress'}' for {days} days with no state change."
            elif t.status == "review" and days >= threshold:
                kind = "Waiting"
                severity = "Medium" if days >= threshold * 2 else "Low"
                description = f"Sitting in '{t.external_state or 'review'}' for {days} days."
            if not kind:
                continue

            blockers.append({
                "id": item_ref(t),
                "type": kind,
                "member": owner.name if owner else "Unassigned",
                "email": owner.email if owner else None,
                "item": item_ref(t),
                "title": t.title,
                "workItemType": t.work_item_type,
                "area": t.project_name,
                "description": description,
                "severity": severity,
                "daysBlocked": days,
                "helper": helper_for(t),
                "inCurrentSprint": bool(current and t.sprint_id == current.id),
                "url": work_item_url(t),
                "suggestedAction": "",
            })

        sev_rank = {"High": 0, "Medium": 1, "Low": 2}
        blockers.sort(key=lambda b: (sev_rank[b["severity"]], not b["inCurrentSprint"], -b["daysBlocked"]))
        blockers = blockers[:40]

        overloaded = [
            {"member": members[mid].name, "hours": v["hours"], "items": v["items"],
             "capacity": effective_cap(members[mid])}
            for mid, v in load.items()
            if mid in members and v["hours"] > effective_cap(members[mid])
        ]

        high = sum(b["severity"] == "High" for b in blockers)
        med = sum(b["severity"] == "Medium" for b in blockers)
        low = sum(b["severity"] == "Low" for b in blockers)
        return {
            "sprint": current.name if current else None,
            "asOf": as_of,
            "staleThresholdDays": self.stale_days,
            "summary": {"totalBlockers": len(blockers), "critical": high, "medium": med, "low": low},
            "blockers": blockers,
            "overloaded": overloaded,
        }

    # ------------------------------------------------------------------
    # Backlog grooming (no LLM)
    # ------------------------------------------------------------------
    async def grooming_data(self) -> dict[str, Any]:
        """Pure backlog-hygiene facts: stale / unestimated / unassigned. No LLM, no persistence."""
        as_of = await self._data_as_of()
        cutoff = as_of - timedelta(days=max(1, int(settings.groom_stale_days)))
        # Same active-sprint pick as /forecast.
        sprint = (
            await self.db.execute(
                select(Sprint)
                .where(Sprint.status == "active")
                .order_by(Sprint.end_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        non_done = Task.status != "done"
        last_touch = func.coalesce(Task.state_changed_at, Task.updated_at, Task.created_at)
        stale = (
            await self.db.execute(
                select(Task).where(non_done, last_touch < cutoff).order_by(last_touch)
            )
        ).scalars().all()

        # One fetch for both sprint-scoped signals (same rows, two filters).
        if sprint:
            sprint_tasks = (
                (
                    await self.db.execute(
                        select(Task).where(non_done, Task.sprint_id == sprint.id)
                    )
                )
                .scalars()
                .all()
            )
        else:
            sprint_tasks = []
        unestimated = [t for t in sprint_tasks if t.story_points is None]
        unassigned = [t for t in sprint_tasks if t.assignee_id is None]

        def _item(category: str, t: Task) -> dict[str, Any]:
            since = _aware(t.state_changed_at or t.updated_at or t.created_at)
            return {
                "category": category,
                "ref": item_ref(t),
                "title": t.title,
                "days": max(0, (as_of - since).days) if since else 0,
            }

        return {
            "summary": {
                "stale": len(stale),
                "unestimated": len(unestimated),
                "unassigned": len(unassigned),
            },
            "items": (
                [_item("stale", t) for t in stale[:10]]
                + [_item("unestimated", t) for t in unestimated]
                + [_item("unassigned", t) for t in unassigned]
            ),
        }

    async def detect_blockers(self) -> dict[str, Any]:
        data = await self.detect_blockers_data()
        blockers, overloaded, counts = data["blockers"], data["overloaded"], data["summary"]
        ai, ai_note = ({}, None)
        if blockers:
            facts = [
                {k: b[k] for k in ("item", "title", "type", "member", "description", "daysBlocked", "helper", "area")}
                for b in blockers[:25]
            ]
            ai, ai_note = await self._ai(
                "Blocked or stuck work items from Azure DevOps (facts, do not change them):\n"
                f"{json.dumps(facts, ensure_ascii=False)}\n\n"
                f"Over-capacity people this sprint: {json.dumps(overloaded, ensure_ascii=False)}\n\n"
                "For each item give ONE concrete next action a delivery manager can take today "
                "(who to talk to, what to ask; use the 'helper' when it makes sense). Then give up to "
                "4 team-level recommendations and a 2-sentence summary.\n"
                'Return JSON: {"actions": {"<item>": "action"}, "recommendations": [""], "summary": ""}',
                "You are a pragmatic delivery manager. Be specific and short. Only reference the "
                "items and people given. Return valid JSON only.",
            )
        actions = ai.get("actions") if isinstance(ai.get("actions"), dict) else {}
        for b in blockers:
            fallback = (
                f"Ask {b['member']} what is needed to lift the hold and chase the dependency."
                if b["type"] in ("On Hold", "Tagged blocked")
                else f"Check in with {b['member']}" + (f"; consider pairing with {b['helper']}." if b["helper"] else ".")
            )
            b["suggestedAction"] = str(actions.get(b["item"]) or fallback)

        recommendations = [str(r) for r in ai.get("recommendations", []) if r] if isinstance(ai.get("recommendations"), list) else []
        for o in overloaded:
            recommendations.append(
                f"{o['member']} has {o['hours']}h open against {o['capacity']}h capacity — rebalance."
            )

        report = {
            "id": "BLOCKERS-" + data["asOf"].strftime("%Y%m%d"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataAsOf": data["asOf"].isoformat(),
            "sprint": data["sprint"],
            "staleThresholdDays": data["staleThresholdDays"],
            "summary": counts,
            "blockers": blockers,
            "overloaded": overloaded,
            "recommendations": recommendations,
            "aiSummary": str(ai.get("summary") or ""),
            "aiNote": ai_note,
        }
        summary = (
            f"{counts['totalBlockers']} blocked/stuck item(s): "
            f"{counts['critical']} high, {counts['medium']} medium, {counts['low']} low"
            + (f" — {data['sprint']}" if data["sprint"] else "")
        )
        report_id = await self._save("blocker_detection", summary, report, "high" if not ai_note else "medium")
        return {"proposal_id": report_id, "report": report}

    # ------------------------------------------------------------------
    # Sprint planning
    # ------------------------------------------------------------------
    async def plan_sprint(self) -> dict[str, Any]:
        as_of = await self._data_as_of()
        # Sprint boundaries are judged at the data's date, not the wall clock,
        # so an older dump doesn't count a not-yet-synced sprint as "0 done".
        today = as_of.date()
        members = await self._members()
        sprints = await self._sprints()
        current = self._current_sprint(sprints, today)
        target = self._next_sprint(sprints, today) or current
        pool = await self._team_pool(sprints, today) or {m.id for m in members.values() if m.is_available}
        committed = await self._open_load(target.id if target else None)

        # Capacity rows the sprint-draft prompt sees: prorated by absences in
        # the sprint being planned, so the agent stops drafting work for
        # people who are partly away.
        absences_by_member: dict[str, list] = defaultdict(list)
        if target:
            for a in await sprint_absences(self.db, target.start_date, target.end_date):
                absences_by_member[a.member_id].append(a)

        # Velocity: plannable items finished per recent completed sprint.
        done_sprints = [s for s in sprints if s.end_date < today][:3]
        velocity_items: list[int] = []
        for s in done_sprints:
            n = (
                await self.db.execute(
                    select(func.count(Task.id)).where(
                        Task.sprint_id == s.id, Task.status == "done",
                        (Task.work_item_type.in_(PLANNABLE_TYPES)) | (Task.work_item_type.is_(None)),
                    )
                )
            ).scalar() or 0
            velocity_items.append(int(n))
        avg_velocity = round(sum(velocity_items) / len(velocity_items)) if velocity_items else None

        past_ids = {s.id for s in sprints if s.end_date < today}
        rows = (
            await self.db.execute(
                select(Task).where(Task.status.in_(("backlog", "todo", "in_progress")))
            )
        ).scalars().all()
        candidates: list[Task] = []
        carry_over: list[Task] = []
        for t in rows:
            if not _is_plannable(t):
                continue
            if t.status == "in_progress":
                # Unfinished work in the current sprint rolls into the next one.
                if current and target and target.id != current.id and t.sprint_id == current.id:
                    carry_over.append(t)
                continue
            if t.sprint_id is None or t.sprint_id in past_ids or (target and t.sprint_id == target.id) or (
                current and t.sprint_id == current.id
            ):
                candidates.append(t)
        candidates.sort(key=lambda t: (
            0 if (target and t.sprint_id == target.id) else 1,
            PRIORITY_RANK.get(t.priority, 2),
            0 if t.status == "todo" else 1,
            t.priority_order,
        ))
        candidates = candidates[:40]
        by_ref = {item_ref(t): t for t in candidates}

        capacity_rows = []
        for mid in pool:
            m = members.get(mid)
            if not m:
                continue
            used = committed[mid]["hours"]
            cap = effective_capacity_hours(m, target, absences_by_member.get(mid, []))
            capacity_rows.append({
                "member": m.name, "capacity": cap,
                "committed": used, "available": cap - used,
            })
        total_cap = sum(r["capacity"] for r in capacity_rows)
        total_committed = sum(r["committed"] for r in capacity_rows)
        target_size = avg_velocity or 8

        def assignee_name(t: Task) -> str | None:
            return members[t.assignee_id].name if t.assignee_id in members else None

        ai, ai_note = ({}, None)
        if candidates:
            facts = [
                {"item": item_ref(t), "title": t.title, "type": t.work_item_type, "priority": t.priority,
                 "state": t.external_state or t.status, "area": t.project_name, "assignee": assignee_name(t),
                 "points": t.story_points, "hours": t.estimated_hours,
                 "alreadyInTarget": bool(target and t.sprint_id == target.id)}
                for t in candidates
            ]
            ai, ai_note = await self._ai(
                f"Plan {target.name if target else 'the next sprint'}.\n"
                f"Team finishes about {avg_velocity if avg_velocity is not None else 'unknown'} backlog items per sprint "
                f"(last sprints: {velocity_items}). Carry-over from the current sprint: {len(carry_over)} item(s).\n"
                f"Capacity per person (hours): {json.dumps(capacity_rows, ensure_ascii=False)}\n\n"
                f"Candidate backlog items (pick only from these, by 'item'):\n{json.dumps(facts, ensure_ascii=False)}\n\n"
                f"Pick about {max(1, target_size - len(carry_over))} items. Prefer high priority, bugs that "
                "block users, items already in the target sprint, and a balanced load across areas.\n"
                'Return JSON: {"sprintGoal": "", "proposedItems": [{"item": "#123", "priority": "High|Medium|Low", '
                '"reason": ""}], "recommendations": [""]}',
                "You are a sprint-planning assistant. Only use item ids from the list. Return valid JSON only.",
            )

        picked: list[dict[str, Any]] = []
        for p in ai.get("proposedItems", []) if isinstance(ai.get("proposedItems"), list) else []:
            t = by_ref.get(str(p.get("item", "")).strip()) if isinstance(p, dict) else None
            if t is None or any(x["id"] == item_ref(t) for x in picked):
                continue  # ignore hallucinated / duplicate ids
            picked.append(self._plan_row(t, str(p.get("priority") or "Medium").title(), str(p.get("reason") or ""), assignee_name(t)))
        if not picked:
            for t in candidates[: max(1, target_size - len(carry_over))]:
                prio = {"critical": "High", "high": "High", "medium": "Medium", "low": "Low"}.get(t.priority, "Medium")
                picked.append(self._plan_row(t, prio, "Top of the backlog by priority", assignee_name(t)))

        warnings = [
            {"type": "Over Capacity", "member": r["member"], "current": r["committed"], "capacity": r["capacity"],
             "action": "Already over capacity before new work — move items off or reassign."}
            for r in capacity_rows if r["committed"] > r["capacity"]
        ]
        if carry_over:
            warnings.append({
                "type": "Carry-over", "member": f"{len(carry_over)} item(s)", "current": len(carry_over),
                "capacity": target_size,
                "action": "Unfinished items from the current sprint: " + ", ".join(item_ref(t) for t in carry_over[:8]),
            })

        areas: dict[str, dict[str, int]] = defaultdict(lambda: {"backlogItems": 0, "totalHours": 0})
        area_people: dict[str, set[str]] = defaultdict(set)
        for t in candidates:
            name = t.project_name or "General"
            areas[name]["backlogItems"] += 1
            areas[name]["totalHours"] += t.estimated_hours or 0
            if t.assignee_id:
                area_people[name].add(t.assignee_id)

        recommendations = [str(r) for r in ai.get("recommendations", []) if r] if isinstance(ai.get("recommendations"), list) else []
        if target is None:
            recommendations.insert(0, "No upcoming sprint exists yet — create the iteration in Azure DevOps first.")

        report = {
            "id": f"PLAN-{(target.name if target else 'next').replace(' ', '-')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataAsOf": as_of.isoformat(),
            "targetSprint": target.name if target else None,
            "sprintGoal": str(ai.get("sprintGoal") or f"Deliver the top {len(picked)} priority backlog items"),
            "velocity": {"recentSprints": velocity_items, "average": avg_velocity},
            "capacity": {
                "total": total_cap,
                "available": total_cap - total_committed,
                "utilization": round(total_committed / total_cap * 100) if total_cap else 0,
            },
            "teamBreakdown": [
                {"team": name, "members": len(area_people[name]), **vals} for name, vals in sorted(areas.items(), key=lambda x: -x[1]["backlogItems"])
            ],
            "proposedItems": picked,
            "warnings": warnings,
            "recommendations": recommendations,
            "aiNote": ai_note,
        }
        summary = f"{target.name if target else 'Next sprint'}: {len(picked)} item(s) proposed — {report['sprintGoal']}"
        report_id = await self._save("sprint_planning", summary, report, "medium")
        return {"proposal_id": report_id, "report": report}

    @staticmethod
    def _plan_row(t: Task, priority: str, reason: str, assignee: str | None) -> dict[str, Any]:
        return {
            "id": item_ref(t), "title": t.title, "type": t.work_item_type, "priority": priority,
            "reason": reason, "hours": t.estimated_hours or 0, "points": t.story_points,
            "assignee": assignee, "area": t.project_name, "url": work_item_url(t),
        }

    # ------------------------------------------------------------------
    # Release assessment
    # ------------------------------------------------------------------
    async def assess_release(self, sprint_id: str | None = None) -> dict[str, Any]:
        as_of = await self._data_as_of()
        members = await self._members()
        sprints = await self._sprints()
        if sprint_id:
            sprint = next((s for s in sprints if s.id == sprint_id), None)
        else:
            sprint = self._release_candidate(sprints, as_of.date())
        if sprint is None:
            raise ValueError("No sprint found to assess. Sync sprints from Azure DevOps first.")

        items = [
            t for t in (await self.db.execute(select(Task).where(Task.sprint_id == sprint.id))).scalars()
            if _is_plannable(t)
        ]
        done = [t for t in items if t.status == "done"]
        open_bugs = [t for t in items if t.work_item_type in BUG_TYPES and t.status != "done"]
        high_bugs = [t for t in open_bugs if t.priority in ("critical", "high")]
        on_hold = [t for t in items if _is_on_hold(t)]
        waiting = [t for t in items if t.status == "review"]
        unowned = [t for t in items if t.status != "done" and not t.assignee_id]
        completion = round(len(done) / len(items) * 100) if items else 0
        days_left = (sprint.end_date - as_of.date()).days

        if high_bugs or on_hold or completion < 80:
            status = "NOT_READY"
        elif completion < 100 or waiting or open_bugs:
            status = "READY_WITH_CAVEATS"
        else:
            status = "READY"

        def owner(t: Task) -> str:
            return members[t.assignee_id].name if t.assignee_id in members else "Unassigned"

        checklist = [
            {"item": "Sprint scope complete (≥80%)", "status": completion >= 80, "note": f"{len(done)}/{len(items)} done ({completion}%)"},
            {"item": "No high-priority bugs open", "status": not high_bugs, "note": f"{len(high_bugs)} open"},
            {"item": "Nothing on hold", "status": not on_hold, "note": f"{len(on_hold)} on hold"},
            {"item": "Nothing waiting for deployment/verification", "status": not waiting, "note": f"{len(waiting)} waiting"},
            {"item": "Every open item has an owner", "status": not unowned, "note": f"{len(unowned)} unassigned"},
        ]
        blockers = [
            {"issue": f"{item_ref(t)} {t.title}", "owner": owner(t), "eta": "—",
             "impact": "High-priority bug open", "url": work_item_url(t)}
            for t in high_bugs
        ] + [
            {"issue": f"{item_ref(t)} {t.title}", "owner": owner(t), "eta": "—",
             "impact": "On hold" + (f": {t.hold_reason}" if t.hold_reason else ""), "url": work_item_url(t)}
            for t in on_hold
        ]
        changelog = {
            "features": [f"{item_ref(t)} {t.title}" for t in done if t.work_item_type in FEATURE_TYPES or t.work_item_type is None],
            "improvements": [],
            "fixes": [f"{item_ref(t)} {t.title}" for t in done if t.work_item_type in BUG_TYPES],
        }

        facts = {
            "sprint": sprint.name, "ends": sprint.end_date.isoformat(), "daysLeft": days_left,
            "status": status, "completionPct": completion, "done": len(done), "total": len(items),
            "openBugs": len(open_bugs), "highBugs": [f"{item_ref(t)} {t.title}" for t in high_bugs[:10]],
            "onHold": [f"{item_ref(t)} {t.title} ({t.hold_reason or 'no reason'})" for t in on_hold[:10]],
            "waiting": [f"{item_ref(t)} {t.title}" for t in waiting[:10]],
        }
        ai, ai_note = await self._ai(
            f"Release readiness facts (computed, do not change the status): {json.dumps(facts, ensure_ascii=False)}\n"
            "Write a go/no-go recommendation of 2-4 sentences for the delivery manager: what must happen "
            'before release and who to chase. Return JSON: {"recommendation": ""}',
            "You are a release manager. Be direct and specific. Return valid JSON only.",
        )
        fallback = {
            "NOT_READY": "Not ready: resolve the blockers below before releasing.",
            "READY_WITH_CAVEATS": "Releasable with caveats: confirm the open items are acceptable to ship without.",
            "READY": "Ready to release.",
        }[status]

        report = {
            "id": f"RELEASE-{sprint.name.replace(' ', '-')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataAsOf": as_of.isoformat(),
            "version": sprint.name,
            "status": status,
            "summary": {"completed": len(done), "inReview": len(waiting), "blockers": len(blockers), "openBugs": len(open_bugs)},
            "checklist": checklist,
            "changelog": changelog,
            "blockers": blockers,
            "recommendation": str(ai.get("recommendation") or fallback),
            "aiNote": ai_note,
        }
        summary = f"{sprint.name}: {status.replace('_', ' ')} — {len(done)}/{len(items)} done, {len(open_bugs)} open bug(s)"
        report_id = await self._save("release_management", summary, report, "high")
        return {"proposal_id": report_id, "report": report}
