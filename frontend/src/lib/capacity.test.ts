import { describe, it, expect } from 'vitest';
import { DEFAULT_CAPACITY_HOURS, currentSprintId } from './capacity';

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
