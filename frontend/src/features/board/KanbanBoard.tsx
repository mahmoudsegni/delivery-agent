import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { RefreshCw, Plus, PanelRightClose, PanelRightOpen, X, CalendarRange } from 'lucide-react';
import { COLUMNS } from '../../lib/boardColumns';
import { subAreaColor, isDefect, typeChip } from '../../lib/subAreas';
import { useTasks, useTeamMembers, useUpdateTask, useCodingRuns, useFeatures, taskKeys } from '../../hooks/useApi';
import type { Task } from '../../types';
import { apiFetch } from '../../lib/api';
import { Button } from '../../ui/Button';
import { Spinner } from '../../ui/Spinner';
import { AgentBadge } from '../../ui/AgentBadge';
import { RunDetailDrawer } from '../coding/RunDetailDrawer';
import { TaskDetailDrawer } from '../tasks/TaskDetailDrawer';
import { CapacityPanel } from './CapacityPanel';
import { TaskFormModal } from './TaskFormModal';
import { SprintFormModal } from './SprintFormModal';

interface KanbanBoardProps {
  showCapacity: boolean;
  setShowCapacity: (s: boolean) => void;
  teamFilter: string | null;
  onClearTeam: () => void;
  /** null = show every sprint. */
  sprintId: string | null;
  /** Open task drawer is owned by App so the search palette can set it. */
  openTaskId: string | null;
  setOpenTaskId: (id: string | null) => void;
}

type Card = { id: string; displayId: string; title: string; status: string; assignee: string; team: string | null; hours: number; feature: string | null; type: string | null; defect: boolean };

const NEXT_STATUS: Record<string, { label: string; to: string }> = {
  todo: { label: 'Start', to: 'in_progress' },
  in_progress: { label: 'Review', to: 'review' },
  review: { label: 'Done', to: 'done' },
};

export function KanbanBoard({ showCapacity, setShowCapacity, teamFilter, onClearTeam, sprintId, openTaskId, setOpenTaskId }: KanbanBoardProps) {
  const qc = useQueryClient();
  const { data: tasks = [], isLoading } = useTasks();
  const { data: members = [] } = useTeamMembers();
  const { data: agentRuns = [] } = useCodingRuns();
  const { data: features = [] } = useFeatures();
  const updateTask = useUpdateTask();

  const runByTask = new Map(agentRuns.map((r) => [r.task_id, r]));
  const featureById = new Map(features.map((f) => [f.id, f.name]));

  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverCol, setDragOverCol] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [showSprintForm, setShowSprintForm] = useState(false);
  const [openRunId, setOpenRunId] = useState<string | null>(null);

  // Resolve the open run/task from the polled lists each render so drawers stay live.
  const openRun = openRunId ? agentRuns.find((r) => r.id === openRunId) : undefined;
  const openTask = openTaskId ? tasks.find((t) => t.id === openTaskId) : undefined;

  const membersById = new Map(members.map((m) => [m.id, m.name]));

  const toCard = (t: Task): Card => {
    const assignee = (t.assignee_id && membersById.get(t.assignee_id)) || '—';
    return {
      id: t.id,
      displayId: t.external_id || t.id.slice(0, 8),
      title: t.title,
      status: t.status === 'backlog' ? 'todo' : t.status,
      assignee,
      // The real Azure DevOps sub-area, or the locally set technology. Null
      // when the item sits on the stream root — no badge beats a made-up one.
      team: t.project_name || t.technology || null,
      hours: t.estimated_hours ?? t.story_points ?? 0,
      feature: (t.feature_id && featureById.get(t.feature_id)) || null,
      // Azure DevOps type. Bugs and Issues are what people hunt for on a
      // board, and they were indistinguishable from tasks until now.
      type: typeChip(t.work_item_type),
      defect: isDefect(t.work_item_type),
    };
  };

  const inSprint = sprintId ? tasks.filter((t) => t.sprint_id === sprintId) : tasks;
  const allCards = inSprint.map(toCard);
  const cards = teamFilter ? allCards.filter((c) => c.team === teamFilter) : allCards;

  const syncTfs = async () => {
    setSyncing(true);
    setSyncMsg(null);
    try {
      const r = await apiFetch('/api/v1/tasks/sync', { method: 'POST' });
      const d = await r.json();
      setSyncMsg(`${d.provider.toUpperCase()} sync: +${d.inserted} new, ${d.updated} updated`);
      qc.invalidateQueries({ queryKey: taskKeys.all });
    } catch (e: any) {
      setSyncMsg(`Sync failed: ${e.message}`);
    } finally {
      setSyncing(false);
    }
  };

  const move = (id: string, to: string) => updateTask.mutate({ id, body: { status: to as Task['status'] } });

  const onDrop = (colId: string) => {
    if (dragId) {
      const card = cards.find((c) => c.id === dragId);
      if (card && card.status !== colId) move(dragId, colId);
    }
    setDragId(null);
    setDragOverCol(null);
  };

  return (
    <>
      <header className="header">
        <h1 className="header-title">Taskboard</h1>
        {teamFilter && (
          <button className="filter-chip" onClick={onClearTeam} title="Clear team filter">
            <span className="sidebar-dot" style={{ background: subAreaColor(teamFilter), width: 8, height: 8, marginLeft: 0 }} />
            {teamFilter}
            <X size={13} strokeWidth={2.5} />
          </button>
        )}
        <div className="header-actions">
          {syncMsg && <span className="mono" style={{ fontSize: 12, color: 'var(--ink-soft)', marginRight: 4 }}>{syncMsg}</span>}
          <Button variant="ghost" onClick={syncTfs} disabled={syncing} icon={<RefreshCw size={15} strokeWidth={2.5} className={syncing ? 'spin-inline' : ''} />}>
            {syncing ? 'Syncing…' : 'Sync from TFS'}
          </Button>
          <Button variant="secondary" onClick={() => setShowCapacity(!showCapacity)} icon={showCapacity ? <PanelRightClose size={15} strokeWidth={2.5} /> : <PanelRightOpen size={15} strokeWidth={2.5} />}>
            {showCapacity ? 'Hide' : 'Show'} Capacity
          </Button>
          <Button variant="secondary" onClick={() => setShowSprintForm(true)} icon={<CalendarRange size={15} strokeWidth={2.5} />}>
            New Sprint
          </Button>
          <Button variant="primary" onClick={() => setShowForm(true)} icon={<Plus size={15} strokeWidth={2.5} />}>
            New Work Item
          </Button>
        </div>
      </header>

      {showForm && <TaskFormModal defaultStatus="todo" onClose={() => setShowForm(false)} />}
      {showSprintForm && <SprintFormModal onClose={() => setShowSprintForm(false)} />}

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        <div className="content" style={{ flex: 1, display: 'flex', gap: 16, padding: 16, overflowX: 'auto' }}>
          {isLoading ? (
            <div style={{ margin: 'auto' }}><Spinner /></div>
          ) : (
            COLUMNS.map((column) => {
              const columnCards = cards.filter((c) => c.status === column.id);
              const totalHours = columnCards.reduce((sum, c) => sum + c.hours, 0);
              return (
                <div
                  key={column.id}
                  className={`kanban-column ${dragOverCol === column.id ? 'drop-target' : ''}`}
                  onDragOver={(e) => { e.preventDefault(); if (dragOverCol !== column.id) setDragOverCol(column.id); }}
                  onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOverCol((c) => (c === column.id ? null : c)); }}
                  onDrop={() => onDrop(column.id)}
                >
                  <div className="kanban-column-header">
                    <div className="kanban-column-dot" style={{ background: column.color }} />
                    <span className="kanban-column-name">{column.name}</span>
                    <span className="kanban-column-count">{columnCards.length}</span>
                    <span className="kanban-column-hours">{totalHours}h</span>
                  </div>
                  <div className="kanban-cards">
                    {columnCards.map((c) => {
                      const next = NEXT_STATUS[c.status];
                      const run = runByTask.get(c.id);
                      return (
                        <div
                          key={c.id}
                          className={`kanban-card ${dragId === c.id ? 'dragging' : ''}`}
                          draggable
                          onDragStart={(e) => { setDragId(c.id); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', c.id); }}
                          onDragEnd={() => { setDragId(null); setDragOverCol(null); }}
                          // Every card opens the task detail drawer; the agent
                          // badge remains the direct shortcut to the live run.
                          onClick={() => setOpenTaskId(c.id)}
                          style={{ cursor: 'pointer' }}
                        >
                          <div className="kanban-card-id">
                            #{c.displayId}
                            {c.type && <span className={`wit-chip ${c.defect ? 'defect' : ''}`}>{c.type}</span>}
                          </div>
                          <div className="kanban-card-title">{c.title}</div>
                          {c.feature && (
                            <div className="kanban-card-feature" title={`Feature: ${c.feature}`}>◆ {c.feature}</div>
                          )}
                          <div className="kanban-card-meta">
                            {c.team && <span className="kanban-card-team" style={{ background: subAreaColor(c.team) }}>{c.team}</span>}
                            <span className="kanban-card-hours">{c.hours}h</span>
                            {c.assignee !== '—' && <span className="kanban-card-assignee">{c.assignee}</span>}
                            {run && <AgentBadge status={run.status} prUrl={run.pr_url} onClick={() => setOpenRunId(run.id)} />}
                          </div>
                          {next && (
                            <div className="kanban-card-actions">
                              <button onClick={(e) => { e.stopPropagation(); move(c.id, next.to); }} disabled={updateTask.isPending}>{next.label}</button>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {showCapacity && <CapacityPanel sprintId={sprintId} />}
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
