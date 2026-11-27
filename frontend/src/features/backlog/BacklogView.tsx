import { useState } from 'react';
import { Plus, X } from 'lucide-react';
import { subAreaColor, isDefect, typeChip } from '../../lib/subAreas';
import { useTasks, useTeamMembers, useCodingRuns, useFeatures, useGrooming } from '../../hooks/useApi';
import { Button } from '../../ui/Button';
import { Spinner } from '../../ui/Spinner';
import { EmptyState } from '../../ui/EmptyState';
import { AgentBadge } from '../../ui/AgentBadge';
import { RunDetailDrawer } from '../coding/RunDetailDrawer';
import { TaskDetailDrawer } from '../tasks/TaskDetailDrawer';
import { TaskFormModal } from '../board/TaskFormModal';

interface BacklogViewProps {
  teamFilter: string | null;
  onClearTeam: () => void;
  /** null = show every sprint. */
  sprintId: string | null;
}

const taskHours = (t: { estimated_hours: number | null; story_points: number | null }) =>
  t.estimated_hours ?? t.story_points ?? 0;

/** Backlog hygiene chips (issue #56, drill-down #58): counts from /agent/grooming, click to see items. */
function HygieneChips() {
  const { data } = useGrooming();
  const [open, setOpen] = useState<string | null>(null);
  const s = data?.summary;
  if (!s || !data) return null;
  const entries = [
    ['stale', '🧹', s.stale],
    ['unestimated', '📐', s.unestimated],
    ['unassigned', '👤', s.unassigned],
  ] as const;
  if (entries.every(([, , n]) => n === 0)) {
    return <span className="filter-chip hygiene-chip">Backlog is clean ✨</span>;
  }
  const items = open ? data.items.filter((i) => i.category === open) : [];
  return (
    <div className="hygiene">
      {entries.map(([key, icon, n]) => (
        <button
          key={key}
          type="button"
          className={`filter-chip hygiene-chip${n > 0 ? ' warn' : ''}${open === key ? ' is-active' : ''}`}
          aria-pressed={open === key}
          onClick={() => setOpen(open === key ? null : key)}
        >
          {icon} {n} {key}
        </button>
      ))}
      {open && (
        <div className="hygiene-panel">
          {items.length === 0 ? (
            <div className="hygiene-item">No {open} items — nice ✨</div>
          ) : (
            items.map((i) => (
              <div key={i.ref} className="hygiene-item">
                <span className="hygiene-ref">{i.ref}</span>
                <span className="hygiene-title">{i.title}</span>
                {i.days !== null && <span className="hygiene-days">{i.days}d untouched</span>}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export function BacklogView({ teamFilter, onClearTeam, sprintId }: BacklogViewProps) {
  const { data: allTasks = [], isLoading } = useTasks();
  const tasks = sprintId ? allTasks.filter((t) => t.sprint_id === sprintId) : allTasks;
  const { data: members = [] } = useTeamMembers();
  const { data: agentRuns = [] } = useCodingRuns();
  const { data: features = [] } = useFeatures();
  const [showForm, setShowForm] = useState(false);
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);

  const membersById = new Map(members.map((m) => [m.id, m.name]));
  const runByTask = new Map(agentRuns.map((r) => [r.task_id, r]));
  const featureById = new Map(features.map((f) => [f.id, f.name]));

  // Resolve the open run/task from the polled lists each render so drawers stay live.
  const openRun = openRunId ? agentRuns.find((r) => r.id === openRunId) : undefined;
  const openTask = openTaskId ? tasks.find((t) => t.id === openTaskId) : undefined;

  const rows = tasks
    .filter((t) => t.status === 'todo' || t.status === 'backlog')
    .map((t) => {
      const assignee = (t.assignee_id && membersById.get(t.assignee_id)) || '—';
      // Real Azure DevOps sub-area; null for items on the stream root.
      const team = t.project_name || t.technology || null;
      const feature = (t.feature_id && featureById.get(t.feature_id)) || null;
      return { t, assignee, team, feature, run: runByTask.get(t.id) };
    });

  const visible = teamFilter ? rows.filter((r) => r.team === teamFilter) : rows;
  const totalHours = visible.reduce((sum, r) => sum + taskHours(r.t), 0);

  return (
    <>
      <header className="header">
        <h1 className="header-title">Backlog</h1>
        {teamFilter && (
          <button className="filter-chip" onClick={onClearTeam} title="Clear team filter">
            <span className="sidebar-dot" style={{ background: subAreaColor(teamFilter), width: 8, height: 8, marginLeft: 0 }} />
            {teamFilter}
            <X size={13} strokeWidth={2.5} />
          </button>
        )}
        <HygieneChips />
        <div className="header-actions">
          <span className="mono" style={{ color: 'var(--ink-soft)', marginRight: 8, fontSize: 12, fontWeight: 600 }}>
            {visible.length} items · {totalHours}h total
          </span>
          <Button variant="primary" onClick={() => setShowForm(true)} icon={<Plus size={15} strokeWidth={2.5} />}>
            New Work Item
          </Button>
        </div>
      </header>

      {showForm && <TaskFormModal defaultStatus="todo" onClose={() => setShowForm(false)} />}
      <div className="content">
        {isLoading ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}><Spinner /></div>
        ) : visible.length === 0 ? (
          <EmptyState>{teamFilter ? `No backlog items for ${teamFilter}.` : 'No backlog items yet — create one.'}</EmptyState>
        ) : (
          <div className="backlog-list">
            {visible.map(({ t, assignee, team, feature, run }, i) => (
              <div
                key={t.id}
                className="backlog-row reveal"
                style={{ ['--i' as any]: i, cursor: 'pointer' }}
                onClick={() => setOpenTaskId(t.id)}
              >
                <span className="backlog-id">#{t.external_id || t.id.slice(0, 8)}</span>
                {typeChip(t.work_item_type) && (
                  <span className={`wit-chip ${isDefect(t.work_item_type) ? 'defect' : ''}`}>
                    {typeChip(t.work_item_type)}
                  </span>
                )}
                <span className="backlog-title">{t.title}</span>
                {feature && <span className="backlog-feature" title={`Feature: ${feature}`}>◆ {feature}</span>}
                {team && <span className="backlog-team" style={{ background: subAreaColor(team) }}>{team}</span>}
                {assignee !== '—' && <span className="backlog-assignee">{assignee}</span>}
                <span className="backlog-hours">{taskHours(t)}h</span>
                {run && <AgentBadge status={run.status} prUrl={run.pr_url} onClick={() => setOpenRunId(run.id)} />}
              </div>
            ))}
          </div>
        )}
      </div>

      {openRun && (
        <RunDetailDrawer
          run={openRun}
          taskTitle={tasks.find((t) => t.id === openRun.task_id)?.title}
          onClose={() => setOpenRunId(null)}
        />
      )}
      {openTask && !openRun && (
        <TaskDetailDrawer
          task={openTask}
          run={runByTask.get(openTask.id)}
          onViewRun={(runId) => { setOpenTaskId(null); setOpenRunId(runId); }}
          onClose={() => setOpenTaskId(null)}
        />
      )}
    </>
  );
}
