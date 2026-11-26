import { useState, type CSSProperties } from 'react';
import {
  ChevronRight, Inbox, MessageCircleQuestion, Newspaper, TrendingUp, X,
} from 'lucide-react';
import {
  useCodingStats, useDecisions, useIntegrationHealth, useLatestBrief, useLlmUsage, usePortfolio,
} from '../../hooks/useApi';
import { formatTokens, topFeatures } from '../../lib/llmUsage';
import { syncStale } from '../../lib/syncHealth';
import { Badge } from '../../ui/Badge';
import { Button } from '../../ui/Button';
import { EmptyState } from '../../ui/EmptyState';
import { TrendChart } from '../../ui/TrendChart';
import { STATUS_TONE, DirectionChip } from '../portfolio/PortfolioView';
import { AutopilotCard } from './AutopilotCard';
import { DecisionQueue } from './DecisionQueue';

// Score colour: confidence health bands (higher = healthier) — same bands as
// the stream drill-in.
const scoreColor = (v: number) => (v >= 75 ? 'var(--success)' : v >= 60 ? 'var(--orange)' : 'var(--danger)');

const SECTION_TITLE: CSSProperties = {
  fontSize: 13, fontWeight: 600, color: 'var(--ink-mute)',
  textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 12,
};

function BriefTeaser({ onOpenBriefs, onOpenAsk }: { onOpenBriefs: () => void; onOpenAsk: () => void }) {
  const { data: brief } = useLatestBrief();
  return (
    <div className="command-card">
      <div className="command-card-title">
        <Newspaper size={16} strokeWidth={2.25} /> Morning brief
      </div>
      {brief ? (
        <>
          <div className="brief-teaser-headline">{brief.headline}</div>
          <div className="brief-teaser-date">{brief.brief_date}</div>
        </>
      ) : (
        <div className="brief-teaser-date" style={{ marginTop: 0 }}>
          No brief yet — generate the first one from the Briefs page.
        </div>
      )}
      <div className="brief-teaser-actions">
        <Button variant="secondary" onClick={onOpenBriefs}>Read the brief →</Button>
        <Button
          variant="ghost"
          onClick={onOpenAsk}
          icon={<MessageCircleQuestion size={14} strokeWidth={2.5} />}
        >
          Ask about this
        </Button>
      </div>
    </div>
  );
}

/** Amber, dismissible warning that the board is stale — the same stall signal
 *  #50 ships to Telegram, rendered where the team actually looks. */
function SyncStaleBanner() {
  const [dismissed, setDismissed] = useState(false);
  const { data, dataUpdatedAt } = useIntegrationHealth();
  if (dismissed || !syncStale(data?.last_sync_at, data?.last_status)) return null;
  const hours = data?.last_sync_at
    ? (dataUpdatedAt - new Date(data.last_sync_at).getTime()) / 3600_000
    : null;
  const age = hours == null ? '' : ` — last ok sync ${hours >= 48 ? `${Math.round(hours / 24)}d` : `${Math.round(hours)}h`} ago`;
  const err = data?.last_error ? ` · ${data.last_error.slice(0, 80)}` : '';
  return (
    <div className="command-stale-banner" role="alert">
      <span style={{ flex: 1 }}>⚠️ Backlog sync stalled{age}{err}</span>
      <button aria-label="Dismiss sync warning" onClick={() => setDismissed(true)}>
        <X size={14} strokeWidth={2.5} />
      </button>
    </div>
  );
}

interface CommandCenterProps {
  onOpenStream: (id: string) => void;
  onOpenBriefs: () => void;
  onOpenAsk: () => void;
}

/** The executive landing page: portfolio health at a glance, the unified
 *  decision queue, AI-workforce stats, the autopilot dial, and the latest
 *  morning brief — one screen that answers "what needs me today?". */
export function CommandCenter({ onOpenStream, onOpenBriefs, onOpenAsk }: CommandCenterProps) {
  const { data: portfolio, isPending, error } = usePortfolio();
  const { data: decisions = [] } = useDecisions();
  const { data: stats } = useCodingStats();
  const { data: usage } = useLlmUsage();

  const avgConfidence = portfolio?.avg_confidence ?? null;
  const trend = portfolio?.confidence_trend ?? [];
  const activeNow = stats?.active_now ?? 0;

  return (
    <>
      <SyncStaleBanner />
      <header className="header">
        <h1 className="header-title">Command Center</h1>
        <div className="header-actions">
          {activeNow > 0 && (
            <span className="live-indicator">
              <span className="live-dot" />
              {activeNow} agent run{activeNow === 1 ? '' : 's'} in flight
            </span>
          )}
        </div>
      </header>

      <div className="content">
        {error && (
          <div className="alert alert-error" style={{ marginBottom: 16 }}>
            <X size={16} strokeWidth={2.5} />
            <span style={{ flex: 1 }}>{(error as Error).message}</span>
          </div>
        )}

        {/* KPI row — the four numbers an executive checks first. */}
        <div className="command-kpis">
          <div className="report-stats">
            <div className="report-stat">
              <div
                className="report-stat-value"
                style={{ color: avgConfidence == null ? 'var(--ink-mute)' : scoreColor(avgConfidence) }}
              >
                {avgConfidence == null ? '—' : Math.round(avgConfidence)}
              </div>
              <div className="report-stat-label">Avg Confidence</div>
            </div>
            <div className="report-stat">
              <div
                className="report-stat-value"
                style={{ color: decisions.length > 0 ? 'var(--accent)' : 'var(--ink)' }}
              >
                {decisions.length}
              </div>
              <div className="report-stat-label">Pending Decisions</div>
            </div>
            <div className="report-stat">
              <div className="report-stat-value">{stats ? stats.active_now : '—'}</div>
              <div className="report-stat-label">Active Agent Runs</div>
            </div>
            <div className="report-stat">
              <div className="report-stat-value" style={{ color: 'var(--success)' }}>
                {stats ? `${Math.round(stats.hours_saved)}h` : '—'}
              </div>
              <div className="report-stat-label">Hours Saved</div>
            </div>
          </div>
        </div>

        {/* LLM spend, straight from the usage ledger. */}
        {usage && usage.calls > 0 && (
          <div
            style={{ fontSize: 12, color: 'var(--ink-mute)', marginTop: 10 }}
            aria-label="LLM usage"
          >
            LLM usage (7d): {usage.calls.toLocaleString()} calls ·{' '}
            {usage.total_tokens.toLocaleString()} tokens · avg {usage.avg_latency_ms}ms
            {Object.keys(usage.by_feature).length > 0 && (
              <>
                {' '}—{' '}
                {topFeatures(usage.by_feature)
                  .map((f) => `${f.feature} ${formatTokens(f.tokens)}`)
                  .join(' · ')}
              </>
            )}
          </div>
        )}

        {/* Stream health tiles — click drills into the PM altitude. */}
        <section style={{ marginBottom: 28 }}>
          <h2 style={SECTION_TITLE}>Streams</h2>
          {isPending ? (
            <EmptyState>Loading streams…</EmptyState>
          ) : !portfolio || portfolio.streams.length === 0 ? (
            <EmptyState>No streams yet</EmptyState>
          ) : (
            <div className="stats-grid">
              {portfolio.streams.map((s) => (
                <button key={s.id} className="stat-card command-tile" onClick={() => onOpenStream(s.id)}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div className="command-tile-name" style={{ flex: 1, minWidth: 0 }}>{s.name}</div>
                    <ChevronRight size={16} strokeWidth={2.25} color="var(--ink-mute)" />
                  </div>
                  <div className="command-tile-sub">
                    {s.owner_name ? `PM: ${s.owner_name}` : 'Unassigned'} · {s.sprint_count} sprint{s.sprint_count === 1 ? '' : 's'}
                  </div>
                  <div className="command-tile-status">
                    <Badge tone={STATUS_TONE[s.status]}>{s.status}</Badge>
                    <DirectionChip direction={s.direction} />
                  </div>
                </button>
              ))}
            </div>
          )}
        </section>

        {/* Portfolio confidence trend. */}
        <section style={{ marginBottom: 28 }}>
          <div className="command-card">
            <div className="command-card-title">
              <TrendingUp size={16} strokeWidth={2.25} /> Portfolio confidence — last {trend.length || 14} days
            </div>
            <div style={{ overflowX: 'auto' }}>
              <TrendChart points={trend} />
            </div>
          </div>
        </section>

        {/* The unified decision queue. */}
        <section style={{ marginBottom: 28 }}>
          <h2 style={{ ...SECTION_TITLE, display: 'flex', alignItems: 'center', gap: 7 }}>
            <Inbox size={15} strokeWidth={2.5} /> Decision queue
          </h2>
          <DecisionQueue />
        </section>

        {/* Autopilot dial + latest brief. */}
        <div className="command-cols">
          <AutopilotCard />
          <BriefTeaser onOpenBriefs={onOpenBriefs} onOpenAsk={onOpenAsk} />
        </div>
      </div>
    </>
  );
}
