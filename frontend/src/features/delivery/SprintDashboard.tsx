import { useMemo, useState } from 'react';
import { CalendarClock, RefreshCw } from 'lucide-react';
import {
  useBurndown, useForecast, useSnapshotSprint, useSprintDashboard, useSprints, useVelocity,
} from '../../hooks/useApi';
import { BarChart, Legend, LineChart, Meter, HBars, shortDate } from '../../ui/charts';
import { Badge } from '../../ui/Badge';
import { Button } from '../../ui/Button';
import { EmptyState } from '../../ui/EmptyState';
import { Formula, Kpi } from '../../ui/Kpi';

/** Same bands as CommandCenter and StreamView — one health scale everywhere. */
const scoreColor = (v: number) =>
  v >= 75 ? 'var(--success)' : v >= 60 ? 'var(--orange)' : 'var(--danger)';

const RISK_TONE = { low: 'success', medium: 'orange', high: 'danger' } as const;

const FACTOR_META: Record<string, { label: string; note: string }> = {
  scope_remaining: { label: 'Scope remaining', note: 'completion by item count' },
  velocity_vs_plan: { label: 'Velocity vs plan', note: 'actual against the ideal line today' },
  open_blockers: { label: 'Items without updates', note: '100 − stale × 25' },
  untested_surface: {
    label: 'Untested surface',
    note: 'points parked in review ÷ coded work — a proxy, not test coverage',
  },
};

interface Props {
  sprintId: string | null;
  onSelectSprint: (id: string | null) => void;
}

/**
 * The PM sprint view: burndown, velocity, readiness, confidence factors, load.
 *
 * These are the classic delivery instruments and none of them existed — the
 * burndown component was deleted and velocity was stored but never surfaced.
 */
export function SprintDashboard({ sprintId, onSelectSprint }: Props) {
  const { data: sprints } = useSprints();
  const { data: dash, isPending, error } = useSprintDashboard(sprintId);
  const { data: burndown } = useBurndown(dash?.sprint_id ?? null);
  const { data: forecast } = useForecast(dash?.sprint_id ?? null);
  const { data: velocity } = useVelocity(dash?.stream_id ?? null, 8);
  const snapshot = useSnapshotSprint();
  const [showAll, setShowAll] = useState(false);

  const burndownSeries = useMemo(() => {
    const pts = burndown?.data_points ?? [];
    return [
      {
        name: 'Ideal',
        color: 'var(--chart-ref)',
        dashed: true,
        points: pts.map((p) => ({ date: p.date, value: p.ideal_points })),
      },
      {
        name: 'Actual',
        color: 'var(--chart-1)',
        points: pts.map((p) => ({ date: p.date, value: p.remaining_points })),
      },
    ];
  }, [burndown]);

  // How much real history exists — an honest caption beats a confident-looking
  // line drawn over days we never measured.
  const measured = (burndown?.data_points ?? []).filter((p) => p.remaining_points != null);
  const historyStart = measured[0]?.date;

  if (isPending) {
    return (
      <>
        <header className="header"><h1 className="header-title">Sprint Dashboard</h1></header>
        <div className="content"><EmptyState>Loading sprint…</EmptyState></div>
      </>
    );
  }

  if (error || !dash) {
    return (
      <>
        <header className="header"><h1 className="header-title">Sprint Dashboard</h1></header>
        <div className="content">
          <EmptyState>
            {error ? (error as Error).message : 'No active sprint. Pick one below or start a sprint.'}
          </EmptyState>
          {sprints && sprints.length > 0 && (
            <div className="sprint-picker" style={{ marginTop: 16 }}>
              {sprints.map((s) => (
                <button key={s.id} className="chip-btn" onClick={() => onSelectSprint(s.id)}>
                  {s.name}
                </button>
              ))}
            </div>
          )}
        </div>
      </>
    );
  }

  // Teams that don't estimate in story points are measured in items.
  const byItems = dash.total_story_points === 0;
  const remaining = byItems
    ? dash.total_tasks - dash.completed_tasks
    : dash.total_story_points - dash.completed_story_points;
  const burnUnit = burndown?.unit ?? 'pts';
  const velUnit = velocity?.unit ?? 'pts';
  const stale = dash.stale_tasks;
  const load = showAll ? dash.team_load : dash.team_load.filter((m) => m.assigned_hours > 0 || m.in_progress > 0);
  const mean = velocity?.rolling_mean ?? null;

  // Negative days_remaining means the sprint end date has passed but it is
  // still marked active — say so rather than printing "-30 days left".
  const daysLabel =
    dash.days_remaining == null
      ? null
      : dash.days_remaining >= 0
        ? `${dash.days_remaining} day${dash.days_remaining === 1 ? '' : 's'} left`
        : `ended ${Math.abs(dash.days_remaining)} days ago`;

  return (
    <>
      <header className="header">
        <h1 className="header-title">{dash.name}</h1>
        <div className="header-actions">
          <Button
            variant="secondary"
            onClick={() => snapshot.mutate(dash.sprint_id)}
            disabled={snapshot.isPending}
            icon={<RefreshCw size={14} strokeWidth={2.5} />}
          >
            {snapshot.isPending ? 'Snapshotting…' : 'Snapshot now'}
          </Button>
        </div>
      </header>

      <div className="content">
        {dash.goal && <p className="dash-intro">{dash.goal}</p>}

        <div className="sprint-picker">
          <span className="chip">
            <CalendarClock size={13} strokeWidth={2.5} />
            {shortDate(dash.start_date)} → {shortDate(dash.end_date)}
          </span>
          {dash.stream_name && <span className="chip">{dash.stream_name}</span>}
          {daysLabel && <span className="chip">{daysLabel}</span>}
          {(sprints ?? []).length > 1 && (
            <select
              className="sprint-select"
              value={dash.sprint_id}
              onChange={(e) => onSelectSprint(e.target.value)}
              aria-label="Choose a sprint"
            >
              {(sprints ?? []).map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          )}
        </div>

        <div className="kpis">
          <Kpi
            label="Progress"
            value={`${dash.progress_percentage}%`}
            foot={`${dash.completed_tasks} of ${dash.total_tasks} items`}
          />
          <Kpi
            label={byItems ? 'Remaining' : 'Points'}
            value={
              byItems ? remaining : (
                <>
                  {dash.completed_story_points}
                  <span style={{ fontSize: 16, color: 'var(--ink-mute)' }}>/{dash.total_story_points}</span>
                </>
              )
            }
            foot={byItems ? 'items not done (no story points in use)' : `${remaining} remaining`}
          />
          <Kpi
            label="Confidence"
            value={dash.confidence == null ? null : Math.round(dash.confidence)}
            tone={dash.confidence == null ? undefined : scoreColor(dash.confidence)}
            foot={dash.confidence == null ? 'no snapshot yet' : 'latest daily snapshot'}
          />
          <Kpi
            label="No update 3d+"
            value={stale.length}
            tone={stale.length ? 'var(--orange)' : undefined}
            foot="in progress, unchanged"
          />
          <Kpi
            label="Velocity"
            value={dash.velocity}
            foot="set when the sprint completes"
          />
        </div>

        <div className="command-card">
          <div className="command-card-title">Burndown</div>
          <p className="card-sub">{burnUnit === 'items' ? 'Items' : 'Story points'} remaining against the straight-line plan.</p>
          <Legend
            entries={[
              { name: 'Ideal', color: 'var(--chart-ref)', dashed: true },
              { name: 'Actual', color: 'var(--chart-1)' },
            ]}
          />
          <LineChart
            series={burndownSeries}
            height={230}
            area
            valueFmt={(v) => `${Math.round(v)} ${burnUnit}`}
            axisFmt={(v) => String(Math.round(v))}
            emptyMessage="No burndown history yet — snapshots start the first day this sprint is active"
          />
          {historyStart && (
            <div className="chart-note">
              History starts {shortDate(historyStart)}. Days without a snapshot stay empty rather
              than reading as zero.
            </div>
          )}
          {forecast && (
            <div className="chart-note">
              {forecast.done
                ? 'Forecast: all work done.'
                : forecast.sufficient_history === false
                  ? 'Forecast: needs two non-zero delivery weeks before it can project.'
                  : forecast.p50_days != null && forecast.p85_days != null && forecast.on_time_probability != null
                    ? `Forecast: 50% in ${forecast.p50_days}d · 85% in ${forecast.p85_days}d · ${Math.round(forecast.on_time_probability * 100)}% by ${shortDate(dash.end_date)}`
                    : null}
            </div>
          )}
        </div>

        <div className="dash-grid-2">
          <div className="command-card">
            <div className="command-card-title">Velocity</div>
            <p className="card-sub">Committed against completed, per sprint. The last pair is in flight.</p>
            <Legend
              entries={[
                { name: 'Committed (plan)', color: 'var(--chart-1)', opacity: 0.32 },
                { name: 'Completed', color: 'var(--chart-1)' },
              ]}
            />
            <BarChart
              groups={(velocity?.points ?? []).map((p) => p.name)}
              series={[
                {
                  name: 'Committed',
                  values: (velocity?.points ?? []).map((p) => p.committed_points),
                  color: 'var(--chart-1)',
                  muted: true,
                },
                {
                  name: 'Completed',
                  values: (velocity?.points ?? []).map((p) => p.completed_points),
                  color: 'var(--chart-1)',
                },
              ]}
              valueFmt={(v) => `${v} ${velUnit}`}
              emptyMessage="No sprints with tasks yet"
            />
            {mean != null && (
              <Formula>
                <b>rolling mean</b> {mean} {velUnit}
                {velocity?.sigma != null && <> ± {velocity.sigma} (1σ)</>} over{' '}
                {velocity?.sprints_counted} completed sprint
                {velocity?.sprints_counted === 1 ? '' : 's'}
                {mean > 0 && (
                  <>
                    <br />
                    at that rate the {remaining} remaining points need{' '}
                    <b>{(remaining / mean).toFixed(1)} sprints</b>
                  </>
                )}
              </Formula>
            )}
          </div>

          <div className="command-card">
            <div className="command-card-title">
              Release readiness
              <Badge tone={RISK_TONE[dash.readiness.risk_level]}>
                {dash.readiness.risk_level} risk
              </Badge>
            </div>
            <p className="card-sub">Completion against the review gate.</p>
            <Meter
              value={dash.readiness.completion_rate}
              tone={
                dash.readiness.risk_level === 'low'
                  ? 'good'
                  : dash.readiness.risk_level === 'medium'
                    ? 'warn'
                    : 'bad'
              }
            />
            <div className="meter-scale">
              <span>0%</span>
              <span className="meter-value">{dash.readiness.completion_rate}% complete</span>
              <span>100%</span>
            </div>
            <table className="dash-table" style={{ marginTop: 14 }}>
              <tbody>
                <tr><td>Done</td><td className="num">{dash.readiness.tasks_done}</td></tr>
                <tr><td>Remaining</td><td className="num">{dash.readiness.tasks_remaining}</td></tr>
                <tr><td>No update 3d+</td><td className="num">{dash.readiness.tasks_stale}</td></tr>
              </tbody>
            </table>
            <div className="caveat">{dash.readiness.recommendation}</div>
          </div>
        </div>

        <div className="dash-grid-2">
          <div className="command-card">
            <div className="command-card-title">Confidence factors</div>
            {dash.factors ? (
              <>
                <p className="card-sub">
                  The four inputs behind this sprint’s score. Weights: scope 35%, velocity 30%,
                  blockers 20%, untested 15%.
                </p>
                {Object.entries(dash.factors).map(([key, v]) => {
                  const meta = FACTOR_META[key];
                  const value = v as number;
                  return (
                    <div className="factor" key={key}>
                      <div className="factor-head">
                        <span>{meta?.label ?? key}</span>
                        <span>{Math.round(value)}</span>
                      </div>
                      <div className="factor-track">
                        <div
                          className="factor-fill"
                          style={{ width: `${Math.max(0, Math.min(100, value))}%`, background: scoreColor(value) }}
                        />
                      </div>
                      {meta && <div className="factor-note">{meta.note}</div>}
                    </div>
                  );
                })}
              </>
            ) : (
              <EmptyState>
                No confidence snapshot for this sprint yet — use “Snapshot now” to take one.
              </EmptyState>
            )}
          </div>

          <div className="command-card">
            <div className="command-card-title">Team load</div>
            <p className="card-sub">
              Assigned hours against capacity this sprint.{' '}
              <button className="link-btn" onClick={() => setShowAll((v) => !v)}>
                {showAll ? 'hide unassigned' : `show all ${dash.team_load.length}`}
              </button>
            </p>
            {load.length ? (
              <HBars
                rows={load.map((m) => ({
                  name: m.name.split(' ')[0],
                  value: m.assigned_hours,
                  color: m.assigned_hours > m.capacity_hours ? 'var(--chart-bad)' : 'var(--chart-1)',
                  note: `${m.role ?? 'member'} · ${m.in_progress} in progress${m.stale ? ` · ${m.stale} without updates` : ''}`,
                }))}
                max={Math.max(...load.map((m) => Math.max(m.assigned_hours, m.capacity_hours)), 1)}
                refAt={load[0]?.capacity_hours}
                refLabel={`${load[0]?.capacity_hours}h capacity`}
                valueFmt={(v) => `${v}h`}
              />
            ) : (
              <EmptyState>Nobody has assigned hours on this sprint.</EmptyState>
            )}
          </div>
        </div>

        {stale.length > 0 && (
          <div className="command-card" style={{ marginTop: 16 }}>
            <div className="command-card-title">No update in over 3 days</div>
            {/* Deliberately not called "blocked": the signal is a stale
                updated_at, which the autopilot nudge and provider sync both
                write. Calling it blocked would overstate what we know. */}
            <p className="card-sub">
              Items sitting in progress without a change. Inferred from the task’s last-modified
              time, which is not the same as being blocked.
            </p>
            <table className="dash-table">
              <thead>
                <tr><th>Item</th><th>Assignee</th><th className="num">Points</th><th className="num">Days idle</th></tr>
              </thead>
              <tbody>
                {stale.map((t) => (
                  <tr key={t.id}>
                    <td>{t.title}</td>
                    <td className="muted">{t.assignee_name ?? 'Unassigned'}</td>
                    <td className="num">{t.story_points ?? '—'}</td>
                    <td className="num">
                      <Badge tone={t.days_idle > 10 ? 'danger' : 'orange'}>{t.days_idle}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
