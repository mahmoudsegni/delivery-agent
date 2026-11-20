import { useEffect, useState } from 'react';
import { AppShell } from './layout/AppShell';
import { type Page } from './layout/Sidebar';
import { KanbanBoard } from './features/board/KanbanBoard';
import { BacklogView } from './features/backlog/BacklogView';
import { FeaturesView } from './features/features/FeaturesView';
import { CapacityView } from './features/capacity/CapacityView';
import { AgentPage } from './features/agent/AgentPage';
import { ProposalsPage } from './features/proposals/ProposalsPage';
import { PrsPage } from './features/prs/PrsPage';
import { PortfolioView } from './features/portfolio/PortfolioView';
import { StreamView } from './features/stream/StreamView';
import { CommandCenter } from './features/command/CommandCenter';
import { WorkforceView } from './features/workforce/WorkforceView';
import { SprintDashboard } from './features/delivery/SprintDashboard';
import { RoadmapView } from './features/roadmap/RoadmapView';
import { FlowView } from './features/flow/FlowView';
import { BriefsPage } from './features/briefs/BriefsPage';
import { AskPage } from './features/ask/AskPage';
import { AgentActivityProvider } from './notifications/AgentActivityContext';
import { StandupForm } from './features/standup/StandupForm';
import { CommandPalette } from './ui/CommandPalette';
import type { MemberHit, TaskHit } from './types';

// A standup link (?standup=<round>[&member=<id>]) opens just the answer form.
const params = new URLSearchParams(window.location.search);
const STANDUP_ROUND = params.get('standup');
const STANDUP_MEMBER = params.get('member');

export default function App() {
  if (STANDUP_ROUND) return <StandupForm roundId={STANDUP_ROUND} memberId={STANDUP_MEMBER} />;
  return <MainApp />;
}

function MainApp() {
  const [page, setPage] = useState<Page>('command');
  const [showCapacity, setShowCapacity] = useState(true);
  const [teamFilter, setTeamFilter] = useState<string | null>(null);
  const [streamId, setStreamId] = useState<string | null>(null);
  // null = "the active sprint"; set when drilling into a specific one.
  const [sprintId, setSprintId] = useState<string | null>(null);
  // Open task drawer lives here so the Ctrl/Cmd+K palette can preset it.
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);

  // Ctrl/Cmd+K toggles the global search palette from anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Palette selections navigate exactly like clicking through the board.
  const openTaskFromSearch = (t: TaskHit) => {
    setSprintId(t.sprint_id);
    setPage('board');
    setOpenTaskId(t.id);
  };
  const openMemberFromSearch = (m: MemberHit) => {
    setTeamFilter(m.name);
    setPage('board');
  };

  // Clicking a team in the sidebar filters the board/backlog by that team
  // (toggles off if it's already active) and jumps to the board so the effect
  // is visible.
  const selectTeam = (team: string) => {
    setTeamFilter((prev) => (prev === team ? null : team));
    setPage((p) => (p === 'board' || p === 'backlog' ? p : 'board'));
  };

  // Selecting a stream (sidebar or a portfolio row) drills into the PM altitude.
  const openStream = (id: string) => {
    setStreamId(id);
    setPage('stream');
  };

  // Drill from a stream or a Command Center tile into one sprint's numbers.
  const openSprint = (id: string | null) => {
    setSprintId(id);
    setPage('delivery');
  };

  return (
    <AgentActivityProvider onOpenPrs={() => setPage('prs')} onOpenBriefs={() => setPage('briefs')}>
    <AppShell
      page={page}
      setPage={setPage}
      selectedTeam={teamFilter}
      onSelectTeam={selectTeam}
      selectedStreamId={streamId}
      onSelectStream={openStream}
      sprintId={sprintId}
      onSelectSprint={setSprintId}
    >
      {page === 'command' && (
        <CommandCenter
          onOpenStream={openStream}
          onOpenBriefs={() => setPage('briefs')}
          onOpenAsk={() => setPage('ask')}
        />
      )}
      {page === 'workforce' && <WorkforceView />}
      {page === 'delivery' && <SprintDashboard sprintId={sprintId} onSelectSprint={openSprint} />}
      {page === 'roadmap' && <RoadmapView streamId={streamId} onSelectStream={setStreamId} />}
      {page === 'flow' && <FlowView streamId={streamId} onSelectStream={setStreamId} />}
      {page === 'briefs' && <BriefsPage />}
      {page === 'ask' && <AskPage />}
      {page === 'board' && (
        <KanbanBoard
          showCapacity={showCapacity}
          setShowCapacity={setShowCapacity}
          teamFilter={teamFilter}
          onClearTeam={() => setTeamFilter(null)}
          sprintId={sprintId}
          openTaskId={openTaskId}
          setOpenTaskId={setOpenTaskId}
        />
      )}
      {page === 'backlog' && <BacklogView teamFilter={teamFilter} onClearTeam={() => setTeamFilter(null)} sprintId={sprintId} />}
      {page === 'features' && <FeaturesView />}
      {page === 'capacity' && <CapacityView />}
      {page === 'agent' && <AgentPage />}
      {page === 'proposals' && <ProposalsPage />}
      {page === 'prs' && <PrsPage />}
      {page === 'portfolio' && <PortfolioView onOpenStream={openStream} />}
      {page === 'stream' && streamId && <StreamView streamId={streamId} />}
    </AppShell>
    <CommandPalette
      open={paletteOpen}
      onClose={() => setPaletteOpen(false)}
      onSelectTask={openTaskFromSearch}
      onSelectMember={openMemberFromSearch}
    />
    </AgentActivityProvider>
  );
}
