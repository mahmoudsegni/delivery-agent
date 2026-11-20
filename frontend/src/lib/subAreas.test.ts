import { describe, it, expect } from 'vitest';
import type { Task } from '../types';
import { subAreasOf, subAreaColor, isDefect, typeChip } from './subAreas';

const task = (project_name: string | null): Task =>
  ({ id: Math.random().toString(36).slice(2), title: 't', description: null, acceptance_criteria: null,
     status: 'todo', priority: 'medium', assignee_id: null, sprint_id: null, feature_id: null,
     story_points: null, estimated_hours: null, technology: null, project_name,
     work_item_type: null, priority_order: 0, external_id: null, created_at: '', updated_at: null }) as Task;

describe('subAreasOf', () => {
  it('returns [] for empty tasks', () => {
    expect(subAreasOf([])).toEqual([]);
  });

  it('skips tasks with no sub-area', () => {
    expect(subAreasOf([task(null), task(null)])).toEqual([]);
  });

  it('counts correctly, busiest first, ties alphabetical', () => {
    const areas = subAreasOf([
      task('Storefront'), task('Storefront'), task('Pulse'), task('Smart Assistant'), task('Pulse'),
    ]);
    expect(areas).toEqual([
      { id: 'Pulse', name: 'Pulse', count: 2 },
      { id: 'Storefront', name: 'Storefront', count: 2 },
      { id: 'Smart Assistant', name: 'Smart Assistant', count: 1 },
    ]);
  });
});

describe('subAreaColor', () => {
  it('is grey for no sub-area, stable otherwise', () => {
    expect(subAreaColor(null)).toBe('#9CA3AF');
    expect(subAreaColor(undefined)).toBe('#9CA3AF');
    expect(subAreaColor('Storefront')).toBe(subAreaColor('Storefront'));
  });
});

describe('isDefect', () => {
  it('matches defect types case-insensitively, not normal work, not null', () => {
    expect(isDefect('Bug')).toBe(true);
    expect(isDefect('IMPEDIMENT')).toBe(true);
    expect(isDefect('Task')).toBe(false);
    expect(isDefect(null)).toBe(false);
  });
});

describe('typeChip', () => {
  it('hides Task and null, abbreviates PBI, passes the rest through', () => {
    expect(typeChip(null)).toBeNull();
    expect(typeChip('Task')).toBeNull();
    expect(typeChip('task')).toBeNull();
    expect(typeChip('Product Backlog Item')).toBe('PBI');
    expect(typeChip('Bug')).toBe('Bug');
  });
});
