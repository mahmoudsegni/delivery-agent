export type Column = { id: string; name: string; color: string };

/** Board columns, mapped to task status. Real app config, not sample data. */
export const COLUMNS: Column[] = [
  { id: 'todo', name: 'To Do', color: '#9CA3AF' },
  { id: 'in_progress', name: 'In Progress', color: '#3B82F6' },
  { id: 'review', name: 'Review', color: '#F59E0B' },
  { id: 'done', name: 'Done', color: '#10B981' },
];

/** Per-column WIP caps. Columns absent (todo, done) are uncapped. */
export const WIP_LIMITS: Record<string, number> = { in_progress: 4, review: 3 };

/** Per-person WIP cap. Constant until someone asks. */
export const PERSON_WIP_LIMIT = 3;

export function wipBreached(limit: number | undefined, count: number): boolean {
  return limit !== undefined && count > limit;
}
