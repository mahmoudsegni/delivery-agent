/**
 * Where LLM tokens go, for the Command Center usage strip.
 * Pure helpers over the by_feature ledger the /usage endpoint returns.
 */

export interface FeatureTokens {
  feature: string;
  tokens: number;
}

/** Top-n features by total tokens, ties broken by feature name asc. */
export function topFeatures(
  byFeature: Record<string, { calls: number; total_tokens: number }>,
  n = 3,
): FeatureTokens[] {
  return Object.entries(byFeature)
    .map(([feature, v]) => ({ feature, tokens: v.total_tokens }))
    .sort((a, b) => b.tokens - a.tokens || a.feature.localeCompare(b.feature))
    .slice(0, n);
}

/** 0/999 plain, then one decimal k/M with trailing ".0" dropped (8k, 8.2k, 1.5M). */
export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  const size = n >= 1_000_000 ? 1_000_000 : 1000;
  const suffix = size === 1_000_000 ? 'M' : 'k';
  return `${(n / size).toFixed(1).replace(/\.0$/, '')}${suffix}`;
}
