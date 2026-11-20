import { describe, it, expect } from 'vitest';
import type { TaskStatus } from '../types';
import { COLUMNS } from './boardColumns';

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
