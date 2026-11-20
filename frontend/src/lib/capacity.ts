/**
 * Hours one person is assumed to have in a sprint when the server has no
 * per-person figure.
 *
 * The on-prem instance leaves the sprint Capacity tab empty, so every synced
 * member falls back to this. Kept in one place because the board rail and the
 * capacity page both compare assigned hours against it and must not disagree.
 */
export const DEFAULT_CAPACITY_HOURS = 60;

/**
 * Hours of capacity one absent workday removes. Must match
 * WORKDAY_HOURS in backend/app/services/team_service.py — the board rail
 * prorates locally and must not disagree with the server's math.
 */
export const WORKDAY_HOURS = 8;

/**
 * Mon–Fri days in [start, end], inclusive (ISO date strings).
 * Same rule the backend prorates capacity by.
 */
export function countWorkdays(start: string, end: string): number {
  let count = 0;
  const d = new Date(`${start}T00:00:00Z`);
  const last = new Date(`${end}T00:00:00Z`);
  while (d <= last) {
    const day = d.getUTCDay();
    if (day !== 0 && day !== 6) count++;
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return count;
}

/**
 * True when an absence window overlaps [start, end] (inclusive ISO dates).
 * One definition so the rail's proration and the time-off list never disagree.
 */
export function overlapsWindow(
  absence: { starts_on: string; ends_on: string },
  start: string,
  end: string,
): boolean {
  return absence.starts_on <= end && absence.ends_on >= start;
}

/**
 * The sprint containing today, latest first when several overlap.
 *
 * Not `status === 'active'`: more than one sprint can carry that flag, so the
 * flag alone picks the wrong one. Dates are unambiguous.
 *
 * Capacity is a per-sprint quantity, so every view that compares assigned
 * hours against it has to pin itself to exactly one sprint — summing several
 * and dividing by one sprint's hours is what produced "357h / 60h".
 */
export function currentSprintId(
  sprints: { id: string; start_date: string; end_date: string }[],
): string | null {
  const today = new Date().toISOString().slice(0, 10);
  return (
    sprints
      .filter((s) => s.start_date <= today && today <= s.end_date)
      .sort((a, b) => b.start_date.localeCompare(a.start_date))[0]?.id ?? null
  );
}
