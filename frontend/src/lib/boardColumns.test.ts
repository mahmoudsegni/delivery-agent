import { describe, it, expect } from 'vitest';
import type { TaskStatus } from '../types';
import { COLUMNS, PERSON_WIP_LIMIT, WIP_LIMITS, wipBreached } from './boardColumns';

describe('WIP limits', () => {
  it('breaches only when count exceeds the cap', () => {
    expect(wipBreached(3, 4)).toBe(true);
    expect(wipBreached(3, 3)).toBe(false);
  });

  it('per-person cap is a positive integer and breaches one past it', () => {
    // Drift to 0 would silently turn the feature off (every member flagged).
    expect(Number.isInteger(PERSON_WIP_LIMIT)).toBe(true);
    expect(PERSON_WIP_LIMIT).toBeGreaterThan(0);
    expect(wipBreached(PERSON_WIP_LIMIT, PERSON_WIP_LIMIT)).toBe(false);
    expect(wipBreached(PERSON_WIP_LIMIT, PERSON_WIP_LIMIT + 1)).toBe(true);
  });

  it('uncapped columns (todo, done) never breach', () => {
    expect(wipBreached(undefined, 99)).toBe(false);
  });

  it('caps exactly In Progress and Review, both real board columns', () => {
    expect(Object.keys(WIP_LIMITS).sort()).toEqual(['in_progress', 'review']);
    for (const id of Object.keys(WIP_LIMITS)) {
      expect(COLUMNS.filter((c) => c.id === id)).toHaveLength(1);
    }
  });
});

describe('COLUMNS', () => {
  it('ids and order are stable — the board layout is this array', () => {
    expect(COLUMNS.map((c) => c.id)).toEqual(['todo', 'in_progress', 'review', 'done']);
  });

  it('every board status lands in exactly one column; backlog and unknown statuses in none', () => {
    // KanbanBoard maps tasks via `cards.filter((c) => c.status === column.id)`,
    // so "the column for a status" is membership in COLUMNS.
    for (const status of ['todo', 'in_progress', 'review', 'done'] as TaskStatus[]) {
      expect(COLUMNS.filter((c) => c.id === status)).toHaveLength(1);
    }
    for (const status of ['backlog', 'blocked', ''] as string[]) {
      expect(COLUMNS.filter((c) => c.id === status)).toHaveLength(0);
    }
  });
});
