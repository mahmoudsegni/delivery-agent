import { describe, it, expect } from 'vitest';
import { filterActions, buildPaletteItems, moveIndex, type PaletteAction } from './paletteItems';
import type { MemberHit, TaskHit } from '../types';

const action = (id: string, label: string): PaletteAction => ({ id, label, run: () => {} });
const ACTIONS = [
  action('snapshot', 'Snapshot current sprint'),
  action('export', 'Export sprint CSV'),
  action('board', 'Go to Board'),
];
const task = (id: string): TaskHit => ({ id, title: `Task ${id}`, sprint_id: 's1' } as TaskHit);
const member = (id: string): MemberHit => ({ id, name: `Member ${id}` } as MemberHit);

describe('filterActions', () => {
  it('matches by label substring, case-insensitive', () => {
    expect(filterActions(ACTIONS, 'SNAP')).toEqual([ACTIONS[0]]);
  });

  it('excludes non-matching actions', () => {
    expect(filterActions(ACTIONS, 'csv')).toEqual([ACTIONS[1]]);
    expect(filterActions(ACTIONS, 'retro')).toEqual([]);
  });

  it('returns all actions for a blank query', () => {
    expect(filterActions(ACTIONS, '  ')).toEqual(ACTIONS);
  });
});

describe('buildPaletteItems', () => {
  it('orders matching actions before task and member hits', () => {
    const items = buildPaletteItems(
      filterActions(ACTIONS, 'board'),
      [task('t1'), task('t2')],
      [member('m1')],
    );
    expect(items.map((i) => i.kind)).toEqual(['action', 'task', 'task', 'member']);
    expect(items[0].kind === 'action' && items[0].action.id).toBe('board');
  });
});

describe('moveIndex', () => {
  it('moves within the combined list and clamps at both ends', () => {
    expect(moveIndex(0, 1, 4)).toBe(1);
    expect(moveIndex(3, 1, 4)).toBe(3);
    expect(moveIndex(0, -1, 4)).toBe(0);
    expect(moveIndex(0, 1, 0)).toBe(0);
  });
});
