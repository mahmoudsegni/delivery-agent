"""Pure proration tests for absence-based capacity.

No network, no DB — the math here is what ``team_load``, the board rail and
the agent's sprint-draft capacity rows consume; the SQL around it follows the
one-query patterns already covered elsewhere.
"""

from datetime import date

import pytest

from app.schemas.team import AbsenceCreate
from app.services.team_service import (
    WORKDAY_HOURS,
    absent_workdays,
    effective_capacity_hours,
)


class FakeAbsence:
    def __init__(self, starts_on: date, ends_on: date) -> None:
        self.starts_on = starts_on
        self.ends_on = ends_on


class FakeMember:
    def __init__(self, capacity_hours_per_sprint: int = 60) -> None:
        self.capacity_hours_per_sprint = capacity_hours_per_sprint


class FakeSprint:
    def __init__(self, start_date: date, end_date: date) -> None:
        self.start_date = start_date
        self.end_date = end_date


# A standard two-week sprint: Mon 2026-11-02 → Fri 2026-11-13 (10 workdays).
SPRINT = FakeSprint(date(2026, 11, 2), date(2026, 11, 13))


def test_absence_straddling_boundary_counts_only_overlap():
    # Week before the sprint through Wed of its first week → Mon–Wed = 3 days.
    a = FakeAbsence(date(2026, 10, 26), date(2026, 11, 4))
    assert absent_workdays([a], SPRINT.start_date, SPRINT.end_date) == 3


def test_weekends_excluded():
    # Two full calendar weeks clipped to the sprint = 10 Mon–Fri days.
    a = FakeAbsence(date(2026, 11, 2), date(2026, 11, 15))
    assert absent_workdays([a], SPRINT.start_date, SPRINT.end_date) == 10


def test_absence_outside_sprint_counts_zero():
    a = FakeAbsence(date(2026, 10, 5), date(2026, 10, 9))
    assert absent_workdays([a], SPRINT.start_date, SPRINT.end_date) == 0


def test_capacity_clamps_at_zero_when_away_whole_sprint():
    a = FakeAbsence(date(2026, 11, 2), date(2026, 11, 13))
    assert effective_capacity_hours(FakeMember(), SPRINT, [a]) == 0


def test_no_absence_keeps_full_capacity():
    assert effective_capacity_hours(FakeMember(), SPRINT, []) == 60


def test_proration_flips_a_row_over_allocated():
    # 45h assigned is fine against 60h, over capacity once 2 workdays off.
    a = FakeAbsence(date(2026, 11, 2), date(2026, 11, 3))
    cap = effective_capacity_hours(FakeMember(), SPRINT, [a])
    assert cap == 60 - 2 * WORKDAY_HOURS
    assert 45 > cap  # team_load bar turns red / agent flags overload


def test_multiple_absences_sum():
    a1 = FakeAbsence(date(2026, 11, 2), date(2026, 11, 3))
    a2 = FakeAbsence(date(2026, 11, 12), date(2026, 11, 13))
    assert absent_workdays([a1, a2], SPRINT.start_date, SPRINT.end_date) == 4


def test_no_sprint_means_no_proration():
    a = FakeAbsence(date(2026, 11, 2), date(2026, 11, 13))
    assert effective_capacity_hours(FakeMember(), None, [a]) == 60


def test_absence_create_rejects_reversed_dates():
    with pytest.raises(ValueError):
        AbsenceCreate(starts_on=date(2026, 11, 10), ends_on=date(2026, 11, 2))
