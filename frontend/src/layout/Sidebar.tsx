import {
  LayoutGrid, ListTodo, Gauge, Bot, ClipboardCheck, Layers, GitBranch, GitPullRequest, Boxes,
  Home, Newspaper, MessageCircleQuestion, Cpu, Activity, Map, ChartNoAxesCombined,
} from 'lucide-react';
import { SprintSwitcher } from './SprintSwitcher';
import { Logo } from '../ui/Logo';
import { subAreaColor, subAreasOf } from '../lib/subAreas';
import { useProposals, useStreams, useTasks } from '../hooks/useApi';
import { NotificationBell } from '../notifications/NotificationBell';
import { useAgentActivityOptional } from '../notifications/AgentActivityContext';

export type Page =
  | 'command' | 'workforce' | 'briefs' | 'ask'
  | 'delivery' | 'flow' | 'roadmap'
  | 'board' | 'backlog' | 'features' | 'capacity' | 'agent' | 'proposals' | 'prs' | 'portfolio' | 'stream';

const PRIMARY: { id: Page; label: string; Icon: typeof LayoutGrid }[] = [
  { id: 'board', label: 'Kanban Board', Icon: LayoutGrid },
  { id: 'backlog', label: 'Backlog', Icon: ListTodo },
  { id: 'features', label: 'Features', Icon: Boxes },
];

interface SidebarProps {
  page: Page;
  setPage: (p: Page) => void;
  selectedTeam: string | null;
  onSelectTeam: (team: string) => void;
  selectedStreamId: string | null;
  onSelectStream: (id: string) => void;
  sprintId: string | null;
  onSelectSprint: (id: string | null) => void;
}

export function Sidebar({
  page,
  setPage,
  selectedTeam,
  onSelectTeam,
  selectedStreamId,
  onSelectStream,
  sprintId,
  onSelectSprint,
}: SidebarProps) {
  const { data: streams = [] } = useStreams();
  const { data: proposals = [] } = useProposals();
  const { data: tasks = [] } = useTasks();
  const subAreas = subAreasOf(tasks);
  const activity = useAgentActivityOptional();

  const pendingProposals = proposals.filter((p) => p.state === 'pending_review').length;
  const openPrs = activity?.openPrCount ?? 0;
  const runsLive = (activity?.activeRunCount ?? 0) > 0;

  const navItem = ({
    id, label, Icon, badge, live,
  }: { id: Page; label: string; Icon: typeof LayoutGrid; badge?: number; live?: boolean }) => (
    <button
      key={id}
      className={`sidebar-item ${page === id ? 'active' : ''}`}
      onClick={() => setPage(id)}
      aria-current={page === id ? 'page' : undefined}
    >
      <Icon size={20} strokeWidth={2.25} />
      <span>{label}</span>
      {live && <span className="sidebar-live-dot" aria-label="Agent runs in flight" />}
      {badge ? <span className={`sidebar-count ${live ? 'accent' : ''}`}>{badge}</span> : null}
    </button>
  );

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <Logo />
        <NotificationBell />
      </div>
      <SprintSwitcher sprintId={sprintId} onSelectSprint={onSelectSprint} />
      {sprintId && (
        <a
          className="btn btn-secondary sidebar-export"
          href={`/api/v1/sprints/${sprintId}/export.csv`}
          download
        >
          Export CSV
        </a>
      )}

      <nav className="sidebar-nav" aria-label="Primary">
        {/* Executive altitude — the default landing for top management. */}
        <div className="sidebar-section">
          <div className="sidebar-section-title">Executive</div>
          {navItem({ id: 'command', label: 'Command Center', Icon: Home, badge: pendingProposals })}
          {navItem({ id: 'workforce', label: 'AI Workforce', Icon: Cpu })}
          {navItem({ id: 'briefs', label: 'Briefs', Icon: Newspaper })}
          {navItem({ id: 'ask', label: 'Ask AI', Icon: MessageCircleQuestion })}
        </div>

        {/* PM altitude — the delivery instruments. */}
        <div className="sidebar-section">
          <div className="sidebar-section-title">Delivery</div>
          {navItem({ id: 'delivery', label: 'Sprint Dashboard', Icon: ChartNoAxesCombined })}
          {navItem({ id: 'roadmap', label: 'Roadmap', Icon: Map })}
          {navItem({ id: 'flow', label: 'Flow', Icon: Activity })}
        </div>

        <div className="sidebar-section">{PRIMARY.map(navItem)}</div>

        {/* The agent layer — promoted right under the delivery core. */}
        <div className="sidebar-section promoted">
          <div className="sidebar-section-title">Agent</div>
          {navItem({ id: 'prs', label: 'PRs', Icon: GitPullRequest, badge: openPrs, live: runsLive })}
          {navItem({ id: 'proposals', label: 'Proposals', Icon: ClipboardCheck, badge: pendingProposals })}
          {navItem({ id: 'agent', label: 'AI Agent', Icon: Bot })}
          {navItem({ id: 'capacity', label: 'Capacity', Icon: Gauge })}
        </div>

        <div className="sidebar-section">
          <div className="sidebar-section-title">Altitude</div>
          <button
            className={`sidebar-item ${page === 'portfolio' ? 'active' : ''}`}
            onClick={() => setPage('portfolio')}
            aria-current={page === 'portfolio' ? 'page' : undefined}
          >
            <Layers size={20} strokeWidth={2.25} />
            <span>Portfolio</span>
          </button>
          {streams.map((s) => {
            const active = page === 'stream' && selectedStreamId === s.id;
            return (
              <button
                key={s.id}
                className={`sidebar-item ${active ? 'active' : ''}`}
                onClick={() => onSelectStream(s.id)}
                aria-current={active ? 'page' : undefined}
              >
                <GitBranch size={18} strokeWidth={2.25} />
                <span>{s.name}</span>
              </button>
            );
          })}
        </div>

        {/* Sub-areas actually present in the synced work, not a fixed list.
            Hidden entirely when nothing carries one, so the filter never
            offers a group that cannot match anything. */}
        {subAreas.length > 0 && (
          <div className="sidebar-section">
            <div className="sidebar-section-title">Filter by Area</div>
            {subAreas.map((area) => (
              <button
                key={area.id}
                className={`sidebar-item ${selectedTeam === area.id ? 'active' : ''}`}
                type="button"
                onClick={() => onSelectTeam(area.id)}
                aria-pressed={selectedTeam === area.id}
              >
                <span className="sidebar-dot" style={{ background: subAreaColor(area.id) }} />
                <span>{area.name}</span>
                <span className="sidebar-count">{area.count}</span>
              </button>
            ))}
          </div>
        )}
      </nav>

      <div className="sidebar-footer">
        <div className="sidebar-status">
          <span className="status-dot online" />
          <span>API Connected</span>
        </div>
      </div>
    </aside>
  );
}
