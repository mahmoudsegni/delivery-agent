import { useState } from 'react';
import {
  TrendingUp, TrendingDown, Minus, Check, X, Pencil, ChevronRight, ArrowUpCircle,
} from 'lucide-react';
import { Badge, type Tone } from '../../ui/Badge';
import { Button } from '../../ui/Button';
import { Modal } from '../../ui/Modal';
import { EmptyState } from '../../ui/EmptyState';
import {
  usePortfolio, useApproveProposal, useRejectProposal, useInvestmentProfile, useInvestmentTrend,
} from '../../hooks/useApi';
import type { StreamStatus, StreamDirection, EscalationItem } from '../../types';

// Exported for reuse by the Command Center stream tiles.
export const STATUS_TONE: Record<StreamStatus, Tone> = {
  'On track': 'success',
  Ahead: 'blue',
  'At risk': 'orange',
  Behind: 'danger',
  'No data': 'neutral',
};

const DIRECTION = {
  improving: { Icon: TrendingUp, color: 'var(--success)' },
  declining: { Icon: TrendingDown, color: 'var(--danger)' },
  steady: { Icon: Minus, color: 'var(--ink-mute)' },
} as const;

export function DirectionChip({ direction }: { direction: StreamDirection }) {
  const { Icon, color } = DIRECTION[direction];
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12, fontWeight: 600, color }}>
      <Icon size={15} strokeWidth={2.5} /> {direction}
    </span>
  );
}

interface PortfolioViewProps {
  onOpenStream: (id: string) => void;
}

export function PortfolioView({ onOpenStream }: PortfolioViewProps) {
  const { data, isPending, error } = usePortfolio();
  const approve = useApproveProposal();
  const reject = useRejectProposal();

  // One modal drives both "Adjust" (edit note, then approve) and "Decline"
  // (reason, then reject). Both flow through the existing proposal workflow —
  // nothing bypasses approve/reject.
  const [modal, setModal] = useState<{ id: string; mode: 'adjust' | 'decline' } | null>(null);
  const [text, setText] = useState('');

  const closeModal = () => { setModal(null); setText(''); };

  const confirmModal = () => {
    if (!modal) return;
    if (modal.mode === 'adjust') {
      approve.mutate({ id: modal.id, reason: `Adjusted: ${text}` });
    } else {
      reject.mutate({ id: modal.id, reason: text });
    }
    closeModal();
  };

  const busy = approve.isPending || reject.isPending;

  return (
    <>
      <header className="header">
        <h1 className="header-title">Portfolio</h1>
      </header>

      <div className="content">
        {error && (
          <div className="alert alert-error" style={{ marginBottom: 16 }}>
            <X size={16} strokeWidth={2.5} />
            <span style={{ flex: 1 }}>{(error as Error).message}</span>
          </div>
        )}

        {/* Streams — qualitative status + direction only; no precise numbers. */}
        <section style={{ marginBottom: 28 }}>
          <h2 style={{ fontSize: 13, fontWeight: 600, color: 'var(--ink-mute)', textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 12 }}>
            Streams
          </h2>
          {isPending ? (
            <EmptyState>Loading portfolio…</EmptyState>
          ) : !data || data.streams.length === 0 ? (
            <EmptyState>No streams yet</EmptyState>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {data.streams.map((s) => (
                <button
                  key={s.id}
                  onClick={() => onOpenStream(s.id)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 16, textAlign: 'left',
                    background: 'var(--surface)', border: '1px solid var(--line)',
                    borderRadius: 'var(--radius-lg)', padding: '16px 18px', cursor: 'pointer',
                    boxShadow: 'var(--shadow-xs)',
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--ink)' }}>{s.name}</div>
                    <div style={{ fontSize: 12, color: 'var(--ink-mute)', marginTop: 3 }}>
                      {s.owner_name ? `PM: ${s.owner_name}` : 'Unassigned'} · {s.sprint_count} sprint{s.sprint_count === 1 ? '' : 's'}
                    </div>
                  </div>
                  <Badge tone={STATUS_TONE[s.status]}>{s.status}</Badge>
                  <DirectionChip direction={s.direction} />
                  <ChevronRight size={18} strokeWidth={2.25} color="var(--ink-mute)" />
                </button>
              ))}
            </div>
          )}
        </section>

        {/* Escalations — cross-stream / above-a-PM decisions, only surfaced here. */}
        <section>
          <h2 style={{ fontSize: 13, fontWeight: 600, color: 'var(--ink-mute)', textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 7 }}>
            <ArrowUpCircle size={15} strokeWidth={2.5} /> Escalations
          </h2>
          {isPending ? (
            <EmptyState>Loading…</EmptyState>
          ) : !data || data.escalations.length === 0 ? (
            <EmptyState>No open escalations</EmptyState>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {data.escalations.map((e: EscalationItem) => (
                <div
                  key={e.id}
                  style={{
                    background: 'var(--surface)', border: '1px solid var(--line)',
                    borderRadius: 'var(--radius-lg)', padding: 18, boxShadow: 'var(--shadow-xs)',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                    <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--ink)', textTransform: 'capitalize' }}>
                      {e.type.replace(/_/g, ' ')}
                    </span>
                    <Badge tone={e.scope === 'portfolio' ? 'purple' : 'orange'}>{e.scope}</Badge>
                    {e.escalated && <Badge tone="danger">escalated</Badge>}
                  </div>
                  {e.reasoning && (
                    <p style={{ fontSize: 13, color: 'var(--ink-soft)', lineHeight: 1.6, margin: '0 0 14px' }}>
                      {e.reasoning}
                    </p>
                  )}
                  <div style={{ display: 'flex', gap: 10 }}>
                    <Button variant="success" disabled={busy} onClick={() => approve.mutate({ id: e.id })} icon={<Check size={15} strokeWidth={3} />}>
                      Approve
                    </Button>
                    <Button variant="secondary" disabled={busy} onClick={() => setModal({ id: e.id, mode: 'adjust' })} icon={<Pencil size={14} strokeWidth={2.5} />}>
                      Adjust
                    </Button>
                    <Button variant="danger" disabled={busy} onClick={() => setModal({ id: e.id, mode: 'decline' })} icon={<X size={15} strokeWidth={3} />}>
                      Decline
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Investment — where the active sprint's story points go, by work item type. */}
        <InvestmentTile />

        {modal && (
          <Modal
            title={modal.mode === 'adjust' ? 'Adjust & Approve' : 'Decline Escalation'}
            onClose={closeModal}
            footer={
              <>
                <Button variant="secondary" onClick={closeModal}>Cancel</Button>
                <Button
                  variant={modal.mode === 'adjust' ? 'success' : 'danger'}
                  onClick={confirmModal}
                  disabled={busy || !text.trim()}
                >
                  {modal.mode === 'adjust' ? 'Approve with note' : 'Decline'}
                </Button>
              </>
            }
          >
            <p style={{ fontSize: 13, color: 'var(--ink-soft)', marginBottom: 14 }}>
              {modal.mode === 'adjust'
                ? 'Add a note describing your adjustment. The proposal is approved with this note attached.'
                : 'Provide a reason for declining this escalation.'}
            </p>
            <textarea
              className="form-textarea"
              value={text}
              onChange={(ev) => setText(ev.target.value)}
              placeholder={modal.mode === 'adjust' ? 'e.g. Approve, but only for one sprint…' : 'Enter reason…'}
              style={{ minHeight: 110, resize: 'vertical' }}
            />
          </Modal>
        )}
      </div>
    </>
  );
}

// Per-type hues for the investment bar; cycles after six types.
const TYPE_COLORS = ['var(--accent)', 'var(--purple)', 'var(--orange)', 'var(--success)', 'var(--pink)', 'var(--ink-mute)'];

// Per-sprint drift lines under the share bar: "Bug — 8 → 13 → 21 pts",
// oldest → newest (active sprint last). A type absent from a sprint reads 0.
function InvestmentTrendLines() {
  const { data } = useInvestmentTrend();
  const sprints = data?.sprints ?? [];
  if (sprints.length < 2) return null; // a trend needs at least two points

  // Union of types, newest sprint's order first so the freshest mix leads.
  const totals = new Map<string, number[]>();
  for (const s of [...sprints].reverse()) {
    for (const r of s.rows) {
      if (!totals.has(r.work_item_type)) totals.set(r.work_item_type, sprints.map(() => 0));
    }
  }
  sprints.forEach((s, i) => {
    for (const r of s.rows) {
      const series = totals.get(r.work_item_type);
      if (series) series[i] = r.total_points;
    }
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 12 }}>
      {[...totals].map(([type, series]) => (
        <span key={type} style={{ fontSize: 12, color: 'var(--ink-soft)' }}>
          {type} — {series.join(' → ')} pts
        </span>
      ))}
    </div>
  );
}

function InvestmentTile() {
  const { data, isPending } = useInvestmentProfile();
  const rows = data?.rows ?? [];
  const total = data?.total_points ?? 0;

  return (
    <section style={{ marginTop: 28 }}>
      <h2 style={{ fontSize: 13, fontWeight: 600, color: 'var(--ink-mute)', textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 12 }}>
        Investment{data?.sprint_name ? ` — ${data.sprint_name}` : ''}
      </h2>
      {isPending ? (
        <EmptyState>Loading…</EmptyState>
      ) : rows.length === 0 || total === 0 ? (
        <EmptyState>No story points in this sprint</EmptyState>
      ) : (
        <>
          {/* Stacked share bar: one segment per type, solid = done, faded = open. */}
          <div style={{ display: 'flex', height: 14, borderRadius: 'var(--radius-lg)', overflow: 'hidden', border: '1px solid var(--line)' }}>
            {rows.map((r, i) => {
              const color = TYPE_COLORS[i % TYPE_COLORS.length];
              const doneShare = r.total_points ? (r.done_points / r.total_points) * 100 : 0;
              return (
                <div key={r.work_item_type} title={`${r.work_item_type}: ${r.done_points}/${r.total_points} pts done`}
                  style={{ width: `${(r.total_points / total) * 100}%`, display: 'flex' }}>
                  <div style={{ width: `${doneShare}%`, background: color }} />
                  <div style={{ width: `${100 - doneShare}%`, background: color, opacity: 0.25 }} />
                </div>
              );
            })}
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 18px', marginTop: 10 }}>
            {rows.map((r, i) => (
              <span key={r.work_item_type} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--ink-soft)' }}>
                <span style={{ width: 9, height: 9, borderRadius: 2, background: TYPE_COLORS[i % TYPE_COLORS.length] }} />
                {r.work_item_type} — {r.done_points}/{r.total_points} pts · {r.item_count} item{r.item_count === 1 ? '' : 's'}
              </span>
            ))}
          </div>
          <InvestmentTrendLines />
        </>
      )}
    </section>
  );
}
