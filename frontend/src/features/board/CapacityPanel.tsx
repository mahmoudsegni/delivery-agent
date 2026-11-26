import { useState } from 'react';
import { PERSON_WIP_LIMIT, wipBreached } from '../../lib/boardColumns';
import { DEFAULT_CAPACITY_HOURS, WORKDAY_HOURS, countWorkdays, currentSprintId, overlapsWindow } from '../../lib/capacity';
import type { Sprint } from '../../types';
import { useAbsences, useCreateAbsence, useDeleteAbsence, useSprints, useTasks, useTeamMembers } from '../../hooks/useApi';

type CapacityPanelProps = {
  /** Sprint selected on the board, or null for its "all sprints" view. */
  sprintId: string | null;
};

/** Add-time-off form: two native date inputs + optional note, defaults to the sprint window. */
function TimeOffForm({ memberId, sprint, onDone }: { memberId: string; sprint: Sprint; onDone: () => void }) {
  const create = useCreateAbsence();
  const [startsOn, setStartsOn] = useState(sprint.start_date);
  const [endsOn, setEndsOn] = useState(sprint.end_date);
  const [note, setNote] = useState('');
  return (
    <form
      className="capacity-timeoff-form"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate(
          { memberId, starts_on: startsOn, ends_on: endsOn, note: note || undefined },
          { onSuccess: onDone },
        );
      }}
    >
      <input type="date" aria-label="Time off start" value={startsOn} onChange={(e) => setStartsOn(e.target.value)} />
      <input type="date" aria-label="Time off end" value={endsOn} onChange={(e) => setEndsOn(e.target.value)} />
      <input
        aria-label="Note (optional)"
        placeholder="note (optional)"
        maxLength={500}
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="capacity-timeoff-actions">
        <button type="submit" disabled={create.isPending}>Save</button>
        <button type="button" onClick={onDone}>Cancel</button>
      </div>
      {/* 422s (e.g. reversed dates) arrive as readable text from the api layer. */}
      {create.isError && <div className="capacity-timeoff-error">{create.error.message}</div>}
    </form>
  );
}

/** Right-hand capacity rail: assigned hours vs capacity, per member. */
export function CapacityPanel({ sprintId }: CapacityPanelProps) {
  const { data: members = [] } = useTeamMembers();
  const { data: tasks = [] } = useTasks();
  const { data: sprints = [] } = useSprints();
  const [addingFor, setAddingFor] = useState<string | null>(null);
  const [listingFor, setListingFor] = useState<string | null>(null);
  const del = useDeleteAbsence();

  // Capacity is per sprint, so this rail is always scoped to exactly one — the
  // board's "all sprints" view falls back to the current sprint rather than
  // summing every sprint, which compared a year of work against one sprint's
  // hours and produced things like "357h / 60h".
  const scopedTo = sprintId ?? currentSprintId(sprints);
  const scopedSprint = sprints.find((s) => s.id === scopedTo);
  const inSprint = scopedTo ? tasks.filter((t) => t.sprint_id === scopedTo) : [];

  // Dated time off prorates the bar: same 8h-per-workday math the backend
  // applies to team-load and the agent's sprint drafts.
  const { data: absences = [] } = useAbsences(scopedSprint?.start_date, scopedSprint?.end_date);
  const daysOffByMember = new Map<string, number>();
  if (scopedSprint) {
    for (const a of absences) {
      if (!overlapsWindow(a, scopedSprint.start_date, scopedSprint.end_date)) continue;
      const start = a.starts_on > scopedSprint.start_date ? a.starts_on : scopedSprint.start_date;
      const end = a.ends_on < scopedSprint.end_date ? a.ends_on : scopedSprint.end_date;
      daysOffByMember.set(
        a.member_id,
        (daysOffByMember.get(a.member_id) || 0) + countWorkdays(start, end)
      );
    }
  }

  const assignedByMember = new Map<string, number>();
  const wipByMember = new Map<string, number>();
  for (const t of inSprint) {
    if (!t.assignee_id) continue;
    // Same measure the board columns use. Synced work items estimate in hours
    // (OriginalEstimate) and carry no story points, so points alone read zero.
    const hours = t.estimated_hours ?? t.story_points ?? 0;
    assignedByMember.set(t.assignee_id, (assignedByMember.get(t.assignee_id) || 0) + hours);
    if (t.status === 'in_progress') {
      wipByMember.set(t.assignee_id, (wipByMember.get(t.assignee_id) || 0) + 1);
    }
  }

  // Only people carrying work in this sprint, busiest first. The full roster is
  // 50 people; listing everyone put a wall of empty bars above the ones that
  // matter, and someone with no work in the sprint has nothing to report here.
  const ordered = members
    .filter((m) => (assignedByMember.get(m.id) || 0) > 0)
    .sort((a, b) => (assignedByMember.get(b.id) || 0) - (assignedByMember.get(a.id) || 0));

  return (
    <div className="capacity-panel">
      <div className="capacity-panel-header">
        <h3>Assigned To</h3>
        {/* Name the sprint: the rail can be scoped to the current sprint while
            the board itself shows all of them, and silent disagreement there is
            how the numbers stopped making sense in the first place. */}
        {scopedSprint && <span className="capacity-panel-scope">{scopedSprint.name}</span>}
      </div>
      <div className="capacity-panel-list">
        {ordered.map((member) => {
          const assigned = assignedByMember.get(member.id) || 0;
          const daysOff = daysOffByMember.get(member.id) || 0;
          const wip = wipByMember.get(member.id) || 0;
          const base = member.capacity_hours_per_sprint || DEFAULT_CAPACITY_HOURS;
          const capacity = Math.max(0, base - daysOff * WORKDAY_HOURS);
          const percent = Math.min((assigned / capacity) * 100, 100);
          const overCapacity = assigned > capacity;
          const memberAbsences = scopedSprint
            ? absences.filter(
                (a) => a.member_id === member.id && overlapsWindow(a, scopedSprint.start_date, scopedSprint.end_date)
              )
            : [];
          return (
            <div key={member.id} className="capacity-member">
              <div className="capacity-member-name">
                <span className="capacity-member-name-text">{member.name}</span>
                {wipBreached(PERSON_WIP_LIMIT, wip) && (
                  <span className="capacity-wip" title="In-progress cards beyond the per-person WIP cap">
                    WIP {wip}/{PERSON_WIP_LIMIT}
                  </span>
                )}
                {scopedSprint && (
                  <button
                    className="capacity-add-off"
                    title="Add time off"
                    aria-label={`Add time off for ${member.name}`}
                    onClick={() => {
                      setAddingFor(addingFor === member.id ? null : member.id);
                      setListingFor(null);
                    }}
                  >
                    ＋
                  </button>
                )}
              </div>
              <div className="capacity-member-bar">
                <div
                  className="capacity-member-fill"
                  style={{ width: `${percent}%`, background: overCapacity ? 'var(--danger)' : 'var(--success)' }}
                />
                {overCapacity && (
                  <div
                    className="capacity-member-over"
                    style={{ width: `${((assigned - capacity) / capacity) * 100}%`, left: '100%' }}
                  />
                )}
              </div>
              <div className="capacity-member-text">
                {assigned}h / {capacity}h
                {daysOff > 0 && (
                  <button
                    className="capacity-days-off"
                    title="Time off in this sprint"
                    onClick={() => {
                      setListingFor(listingFor === member.id ? null : member.id);
                      setAddingFor(null);
                    }}
                  >
                    {daysOff}d off
                  </button>
                )}
              </div>
              {addingFor === member.id && scopedSprint && (
                <TimeOffForm memberId={member.id} sprint={scopedSprint} onDone={() => setAddingFor(null)} />
              )}
              {listingFor === member.id && memberAbsences.length > 0 && (
                <div className="capacity-absence-list">
                  {memberAbsences.map((a) => (
                    <div key={a.id} className="capacity-absence-item">
                      <span>
                        {a.starts_on} → {a.ends_on}
                        {a.note ? ` · ${a.note}` : ''}
                      </span>
                      <button aria-label={`Delete time off ${a.starts_on} to ${a.ends_on}`} onClick={() => del.mutate(a.id)}>
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
