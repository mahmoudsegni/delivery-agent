import { describe, it, expect } from 'vitest';
import { syncStale } from './syncHealth';

const NOW = new Date('2026-11-26T12:00:00Z');
const ts = (hoursAgo: number) => new Date(NOW.getTime() - hoursAgo * 3600_000).toISOString();

describe('syncStale', () => {
  it('never-synced (demo mode) is not stale', () => {
    expect(syncStale(null, 'ok', NOW)).toBe(false);
  });

  it('errored last sync is stale regardless of age', () => {
    expect(syncStale(ts(0.1), 'error', NOW)).toBe(true);
  });

  it('7h-old ok sync is stale', () => {
    expect(syncStale(ts(7), 'ok', NOW)).toBe(true);
  });

  it('5h-old ok sync is fresh', () => {
    expect(syncStale(ts(5), 'ok', NOW)).toBe(false);
  });

  it('exactly 6h is fresh (boundary is strictly greater)', () => {
    expect(syncStale(ts(6), 'ok', NOW)).toBe(false);
  });
});
