import { describe, it, expect } from 'vitest';
import { topFeatures, formatTokens } from './llmUsage';

const ledger = (entries: Record<string, number>) =>
  Object.fromEntries(
    Object.entries(entries).map(([feature, tokens]) => [feature, { calls: 1, total_tokens: tokens }]),
  );

describe('topFeatures', () => {
  it('returns [] for an empty record', () => {
    expect(topFeatures({})).toEqual([]);
  });

  it('sorts by total_tokens desc and caps at 3', () => {
    const by = ledger({ briefs: 100, standup: 5000, retro: 1000, backlog: 50 });
    expect(topFeatures(by).map((f) => f.feature)).toEqual(['standup', 'retro', 'briefs']);
  });

  it('breaks ties by feature name asc (deterministic order)', () => {
    const by = ledger({ sync: 100, brief: 100, agent: 100 });
    expect(topFeatures(by).map((f) => f.feature)).toEqual(['agent', 'brief', 'sync']);
  });

  it('honours n', () => {
    const by = ledger({ a: 30, b: 20, c: 10 });
    expect(topFeatures(by, 2).map((f) => f.feature)).toEqual(['a', 'b']);
  });
});

describe('formatTokens', () => {
  it('formats the edges the strip prints', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(999)).toBe('999');
    expect(formatTokens(1000)).toBe('1k');
    expect(formatTokens(8000)).toBe('8k');
    expect(formatTokens(8200)).toBe('8.2k');
    expect(formatTokens(12345)).toBe('12.3k');
    expect(formatTokens(1_500_000)).toBe('1.5M');
  });
});
