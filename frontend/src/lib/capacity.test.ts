import { describe, it, expect } from 'vitest';
import { DEFAULT_CAPACITY_HOURS, currentSprintId, overlapsWindow } from './capacity';

const iso = (offsetDays: number) =>
  new Date(Date.now() + offsetDays * 86400000).toISOString().slice(0, 10);

const sprint = (id: string, start: string, end: string) => ({ id, start_date: start, end_date: end });

describe('currentSprintId', () => {
  it('returns null when there are no sprints', () => {
    expect(currentSprintId([])).toBeNull();
  });

  it('picks the sprint whose date window contains today', () => {
    expect(currentSprintId([
      sprint('past', iso(-30), iso(-15)),
      sprint('now', iso(-5), iso(9)),
    ])).toBe('now');
  });

  it('prefers the latest start when windows overlap', () => {
    expect(currentSprintId([
      sprint('earlier-start', iso(-10), iso(20)),
      sprint('later-start', iso(-2), iso(12)),
    ])).toBe('later-start');
  });

  it('returns null when no sprint covers today', () => {
    expect(currentSprintId([sprint('past', iso(-30), iso(-1))])).toBeNull();
  });
});

describe('DEFAULT_CAPACITY_HOURS', () => {
  it('stays a positive number — the board rail and capacity page both fall back to it', () => {
    expect(DEFAULT_CAPACITY_HOURS).toBe(60);
  });
});

describe('overlapsWindow', () => {
  const absence = { starts_on: '2026-11-16', ends_on: '2026-11-18' };

  it('is true when the absence sits inside the window', () => {
    expect(overlapsWindow(absence, '2026-11-10', '2026-11-27')).toBe(true);
  });

  it('is inclusive on both boundary days', () => {
    expect(overlapsWindow(absence, '2026-11-18', '2026-11-27')).toBe(true);
    expect(overlapsWindow(absence, '2026-11-10', '2026-11-16')).toBe(true);
  });

  it('is true when the absence spans past the window', () => {
    expect(overlapsWindow(absence, '2026-11-17', '2026-11-17')).toBe(true);
  });

  it('is false when the absence is fully before or after the window', () => {
    expect(overlapsWindow(absence, '2026-11-19', '2026-11-27')).toBe(false);
    expect(overlapsWindow(absence, '2026-11-01', '2026-11-15')).toBe(false);
  });
});
