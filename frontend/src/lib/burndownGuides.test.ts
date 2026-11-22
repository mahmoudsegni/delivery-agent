import { describe, it, expect } from 'vitest';
import { addDays, withProjection, type BurndownDay } from './burndownGuides';

// Sprint Mon 2026-11-16 → Fri 2026-11-20, 40 pts committed, days 0–1 measured.
const sprint = (): BurndownDay[] => [
  { date: '2026-11-16', ideal: 40, remaining: 40 },
  { date: '2026-11-17', ideal: 30, remaining: 35 },
  { date: '2026-11-18', ideal: 20, remaining: null },
  { date: '2026-11-19', ideal: 10, remaining: null },
  { date: '2026-11-20', ideal: 0, remaining: null },
];

describe('addDays', () => {
  it('crosses month and year boundaries', () => {
    expect(addDays('2026-11-28', 3)).toBe('2026-12-01');
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01');
  });
});

describe('withProjection', () => {
  it('passes the measured grid through untouched when there is no forecast', () => {
    const g = withProjection(sprint(), '2026-11-17', null);
    expect(g.dates).toHaveLength(5);
    expect(g.projection.every((v) => v === null)).toBe(true);
    expect(g.ideal[0]).toBe(40);
    expect(g.ideal[4]).toBe(0);
    expect(g.actual).toEqual([40, 35, null, null, null]);
  });

  it('draws from the last measured point to a zero crossing at today + p50', () => {
    // today 11-17 + 4d → crossing 11-21, one day past the end date.
    const g = withProjection(sprint(), '2026-11-17', 4);
    expect(g.dates).toHaveLength(6);
    expect(g.dates[5]).toBe('2026-11-21');
    expect(g.projection[1]).toBe(35); // last measured point, verbatim
    expect(g.projection[5]).toBe(0); // the crossing
    // Straight segment: midpoint of the span is half the start value.
    expect(g.projection[3]).toBeCloseTo(17.5);
    // Nothing drawn before the last measured day.
    expect(g.projection[0]).toBeNull();
    // Extended day is padded, never invented, on the other series.
    expect(g.ideal[5]).toBeNull();
    expect(g.actual[5]).toBeNull();
  });

  it('keeps the segment monotone from remaining down to zero', () => {
    const g = withProjection(sprint(), '2026-11-17', 3);
    const drawn = g.projection.filter((v): v is number => v != null);
    expect(drawn[0]).toBe(35);
    expect(drawn[drawn.length - 1]).toBe(0);
    for (let i = 1; i < drawn.length; i++) {
      expect(drawn[i]).toBeLessThan(drawn[i - 1]);
    }
  });

  it('rounds fractional p50 days half-up', () => {
    const g = withProjection(sprint(), '2026-11-17', 2.5);
    expect(g.dates[g.dates.length - 1]).toBe('2026-11-20'); // 17th + 3d
  });

  it('draws nothing when no day was ever measured', () => {
    const days = sprint().map((d) => ({ ...d, remaining: null }));
    const g = withProjection(days, '2026-11-17', 4);
    expect(g.dates).toHaveLength(5); // grid not extended either
    expect(g.projection.every((v) => v === null)).toBe(true);
  });

  it('draws nothing when the crossing is not after the last measured day', () => {
    // p50 of 0 with work remaining: contradictory, refuse to draw.
    const g = withProjection(sprint(), '2026-11-17', 0);
    expect(g.projection.every((v) => v === null)).toBe(true);
  });
});
