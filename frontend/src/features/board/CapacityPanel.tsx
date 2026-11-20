import { DEFAULT_CAPACITY_HOURS, WORKDAY_HOURS, countWorkdays, currentSprintId } from '../../lib/capacity';
import { useAbsences, useSprints, useTasks, useTeamMembers } from '../../hooks/useApi';

type CapacityPanelProps = {
  /** Sprint selected on the board, or null for its "all sprints" view. */
  sprintId: string | null;
};

/** Right-hand capacity rail: assigned hours vs capacity, per member. */
export function CapacityPanel({ sprintId }: CapacityPanelProps) {
  const { data: members = [] } = useTeamMembers();
  const { data: tasks = [] } = useTasks();
  const { data: sprints = [] } = useSprints();

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
      const start = a.starts_on > scopedSprint.start_date ? a.starts_on : scopedSprint.start_date;
      const end = a.ends_on < scopedSprint.end_date ? a.ends_on : scopedSprint.end_date;
      if (start > end) continue;
      daysOffByMember.set(
        a.member_id,
        (daysOffByMember.get(a.member_id) || 0) + countWorkdays(start, end)
      );
    }
  }

  const assignedByMember = new Map<string, number>();
  for (const t of inSprint) {
    if (!t.assignee_id) continue;
    // Same measure the board columns use. Synced work items estimate in hours
    // (OriginalEstimate) and carry no story points, so points alone read zero.
    const hours = t.estimated_hours ?? t.story_points ?? 0;
    assignedByMember.set(t.assignee_id, (assignedByMember.get(t.assignee_id) || 0) + hours);
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
          const base = member.capacity_hours_per_sprint || DEFAULT_CAPACITY_HOURS;
          const capacity = Math.max(0, base - daysOff * WORKDAY_HOURS);
          const percent = Math.min((assigned / capacity) * 100, 100);
          const overCapacity = assigned > capacity;
          return (
            <div key={member.id} className="capacity-member">
              <div className="capacity-member-name">{member.name}</div>
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
                {daysOff > 0 && <span className="capacity-days-off">{daysOff}d off</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
