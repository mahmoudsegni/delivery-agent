import type { MemberHit, TaskHit } from '../types';

/** A keyboard-runnable verb shown in the Ctrl/Cmd+K palette. */
export interface PaletteAction {
  id: string;
  label: string;
  run: () => void;
}

export type PaletteItem =
  | { kind: 'action'; action: PaletteAction }
  | { kind: 'task'; hit: TaskHit }
  | { kind: 'member'; hit: MemberHit };

/** Actions match the query by label, case-insensitive substring. */
export function filterActions(actions: PaletteAction[], q: string): PaletteAction[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return actions;
  return actions.filter((a) => a.label.toLowerCase().includes(needle));
}

/** Combined list the palette walks: actions first, then task and member hits. */
export function buildPaletteItems(
  actions: PaletteAction[],
  tasks: TaskHit[],
  members: MemberHit[],
): PaletteItem[] {
  return [
    ...actions.map((action) => ({ kind: 'action' as const, action })),
    ...tasks.map((hit) => ({ kind: 'task' as const, hit })),
    ...members.map((hit) => ({ kind: 'member' as const, hit })),
  ];
}

/** Keyboard index over the combined list, clamped at both ends. */
export function moveIndex(index: number, delta: number, length: number): number {
  if (length <= 0) return 0;
  return Math.min(length - 1, Math.max(0, index + delta));
}
