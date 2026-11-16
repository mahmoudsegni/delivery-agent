"""Monte Carlo completion forecast per sprint.

Bootstraps the portfolio's historical weekly throughput (last 6 ISO weeks),
samples weeks with replacement until the sprint's remaining work is covered,
and reports p50/p85 days-to-done plus the probability of finishing by the
sprint's end date. Pure stdlib ``random.Random(seed)`` — deterministic, no
new deps, no LLM.
"""

import math
import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task
from app.services.sprint_service import SprintService

# When an item last really changed state — same rule as agents/tools.py:
# synced items carry state_changed_at; updated_at is rewritten by every sync.
LAST_CHANGE = func.coalesce(Task.state_changed_at, Task.updated_at)


def _simulate(
    weekly: list[int], remaining: int, iterations: int = 1000, seed: int = 42
) -> list[int]:
    """Days-to-done per run, sampling weeks with replacement.

    A partial week counts as a full week — coarse by design; the forecast
    feeds a one-line UI caption, not a schedule.
    """
    if not any(weekly):
        raise ValueError("no throughput history to sample from")
    rng = random.Random(seed)
    n = len(weekly)
    days: list[int] = []
    for _ in range(iterations):
        acc = weeks = 0
        while acc < remaining:
            acc += weekly[rng.randrange(n)]
            weeks += 1
        days.append(weeks * 7)
    return days


def _pct(sorted_days: list[int], p: int) -> int:
    i = min(len(sorted_days) - 1, max(0, math.ceil(p / 100 * len(sorted_days)) - 1))
    return sorted_days[i]


def _summarize(days: list[int], budget_days: int) -> dict:
    s = sorted(days)
    return {
        "p50_days": _pct(s, 50),
        "p85_days": _pct(s, 85),
        "on_time_probability": sum(1 for d in days if d <= budget_days) / len(days),
    }


def _forecast_payload(
    weekly: list[int], remaining: int, unit: str, budget_days: int, iterations: int, seed: int
) -> dict:
    if remaining <= 0:
        return {"done": True}
    if sum(1 for w in weekly if w > 0) < 2:
        # One active week is an anecdote — refuse to project from it.
        return {"sufficient_history": False, "weekly_throughput": weekly}
    days = _simulate(weekly, remaining, iterations, seed)
    return {
        **_summarize(days, budget_days),
        "iterations": iterations,
        "unit": unit,
        "remaining": remaining,
        "weekly_throughput": weekly,
    }


async def weekly_throughput(db: AsyncSession, weeks: int = 6) -> list[int]:
    """Per-week done throughput across ALL sprints, oldest first.

    Story points when the portfolio uses them, item counts otherwise —
    same rule as ``SprintService.work_units``.
    """
    today = datetime.now(timezone.utc).date()
    start = datetime.combine(
        today - timedelta(days=today.weekday() + 7 * (weeks - 1)),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    idx = func.floor(
        (func.extract("epoch", LAST_CHANGE) - start.timestamp()) / 604800
    ).label("w")
    rows = await db.execute(
        select(idx, func.coalesce(func.sum(Task.story_points), 0), func.count(Task.id))
        .where(Task.status == "done", LAST_CHANGE >= start)
        .group_by(idx)
    )
    use_pts = any(int(pts or 0) > 0 for _, pts, _ in rows)
    out = [0] * weeks
    for w, pts, items in rows:
        if 0 <= int(w) < weeks:
            out[int(w)] = int(pts if use_pts else items)
    return out


async def forecast(
    db: AsyncSession, sprint_id, iterations: int = 1000, seed: int = 42
) -> dict:
    svc = SprintService(db)
    sprint = await svc.get_sprint(sprint_id)  # raises SprintNotFoundError
    total, done, unit = await svc.work_units(sprint_id)
    weekly = await weekly_throughput(db)
    budget = (sprint.end_date - date.today()).days
    return _forecast_payload(weekly, total - done, unit, budget, iterations, seed)
