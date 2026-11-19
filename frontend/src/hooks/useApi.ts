// React Query hooks over the backend API. One QueryClient is provided in main.tsx.
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import type { AskMessage, AutopilotUpdate, FeatureCreate, SprintCreate, TaskCreate, TaskUpdate } from '../types';
import {
  listTasks, createTask, updateTask, listTeamMembers, getSprints, createSprint,
  getFeatures, createFeature, getCodingRepositories, createCodingRepository,
  createProposal, type CreateProposalBody, listProposals,
  getPortfolio, getStreams, getStream, approveProposal, rejectProposal,
  listCodingRuns, mergeCodingRun, reviseCodingRun, getRunEvents, dispatchCodingAgent,
  getDecisions, getCodingStats, getAutopilot, updateAutopilot,
  listBriefs, getLatestBrief, generateBrief, askDeliveryManager,
  getSprintDashboard, getTeamLoad, getVelocity, getDeliverySummary, getIntegrationHealth,
  getCostByStream, getWorkforceTrend, getFlowMetrics, getRoadmap, getBurndown, snapshotSprint,
  getForecast, getInvestmentProfile, getInvestmentTrend,
} from '../lib/api';

export const taskKeys = { all: ['tasks'] as const };
export const teamKeys = { all: ['team-members'] as const };
export const sprintKeys = { all: ['sprints'] as const };
export const featureKeys = { all: ['features'] as const };
export const repoKeys = { all: ['coding-repositories'] as const };
export const codingKeys = { all: ['coding-runs'] as const };
export const portfolioKeys = { all: ['portfolio'] as const };
export const streamKeys = {
  all: ['streams'] as const,
  detail: (id: string) => ['stream', id] as const,
};
export const runEventKeys = {
  detail: (id: string) => ['run-events', id] as const,
};
export const proposalKeys = { all: ['agent-proposals'] as const };
export const decisionKeys = { all: ['decisions'] as const };
export const codingStatsKeys = { all: ['coding-stats'] as const };
export const autopilotKeys = { all: ['autopilot'] as const };
export const briefKeys = {
  all: ['briefs'] as const,
  latest: ['briefs', 'latest'] as const,
};

export function useTasks() {
  return useQuery({ queryKey: taskKeys.all, queryFn: () => listTasks() });
}

export function useTeamMembers() {
  return useQuery({ queryKey: teamKeys.all, queryFn: listTeamMembers });
}

export function useSprints() {
  return useQuery({ queryKey: sprintKeys.all, queryFn: () => getSprints() });
}

export function useCreateSprint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SprintCreate) => createSprint(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: sprintKeys.all }),
  });
}

export function useFeatures() {
  return useQuery({ queryKey: featureKeys.all, queryFn: () => getFeatures() });
}

export function useCreateFeature() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: FeatureCreate) => createFeature(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: featureKeys.all }),
  });
}

// Repos to offer in the dispatch form (config + previously used; user can add).
export function useCodingRepositories() {
  return useQuery({ queryKey: repoKeys.all, queryFn: getCodingRepositories });
}

export function useCreateRepository() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, seed = true }: { name: string; seed?: boolean }) => createCodingRepository(name, seed),
    onSuccess: () => qc.invalidateQueries({ queryKey: repoKeys.all }),
  });
}

export function useCreateTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: TaskCreate) => createTask(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: taskKeys.all }),
  });
}

export function useUpdateTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: TaskUpdate }) => updateTask(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: taskKeys.all });
      // Moving a card changes progress, team load and the burndown — the
      // dashboards would otherwise sit stale until their next poll.
      qc.invalidateQueries({ queryKey: dashboardKeys.all });
      qc.invalidateQueries({ queryKey: burndownKeys.all });
      qc.invalidateQueries({ queryKey: forecastKeys.all });
    },
  });
}

export function useCreateProposal() {
  return useMutation({ mutationFn: (body: CreateProposalBody) => createProposal(body) });
}

// ---- Two-altitude views ----
export function usePortfolio() {
  return useQuery({ queryKey: portfolioKeys.all, queryFn: getPortfolio });
}

export function useStreams() {
  return useQuery({ queryKey: streamKeys.all, queryFn: getStreams });
}

export function useStream(id: string | null) {
  return useQuery({
    queryKey: id ? streamKeys.detail(id) : ['stream', 'none'],
    queryFn: () => getStream(id as string),
    enabled: !!id,
  });
}

// Proposals list (PRs page joins these to runs by proposal_id; the sidebar
// shows the pending-review count). One shared 10s poll.
export function useProposals() {
  return useQuery({ queryKey: proposalKeys.all, queryFn: () => listProposals(), refetchInterval: 10_000 });
}

// Approving/declining an escalation or operational proposal should refresh both
// altitudes (the portfolio escalations list and any open stream queue), the
// proposals list, the runs (merge eligibility depends on proposal state), and
// the Command Center decision queue (decisions are proposals + open PRs).
function useProposalRefresh() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: portfolioKeys.all });
    qc.invalidateQueries({ queryKey: ['stream'] });
    qc.invalidateQueries({ queryKey: proposalKeys.all });
    qc.invalidateQueries({ queryKey: codingKeys.all });
    qc.invalidateQueries({ queryKey: decisionKeys.all });
    // Approving/merging moves tasks and spends budget, so every dashboard
    // aggregate is downstream of this too.
    qc.invalidateQueries({ queryKey: dashboardKeys.all });
    qc.invalidateQueries({ queryKey: burndownKeys.all });
    qc.invalidateQueries({ queryKey: forecastKeys.all });
  };
}

export function useApproveProposal() {
  const refresh = useProposalRefresh();
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) => approveProposal(id, reason),
    onSuccess: refresh,
  });
}

export function useRejectProposal() {
  const refresh = useProposalRefresh();
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => rejectProposal(id, reason),
    onSuccess: refresh,
  });
}

// ---- Coding-agent runs ----
// Poll so card badges follow the loop (dispatched → … → pr_open → merged).
export function useCodingRuns() {
  return useQuery({ queryKey: codingKeys.all, queryFn: listCodingRuns, refetchInterval: 4000 });
}

export function useMergeCodingRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => mergeCodingRun(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: codingKeys.all });
      qc.invalidateQueries({ queryKey: taskKeys.all });
      qc.invalidateQueries({ queryKey: proposalKeys.all });
      // A merged PR leaves the Command Center decision queue.
      qc.invalidateQueries({ queryKey: decisionKeys.all });
    },
  });
}

export function useReviseCodingRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, comment }: { runId: string; comment: string }) => reviseCodingRun(runId, comment),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: codingKeys.all });
      qc.invalidateQueries({ queryKey: proposalKeys.all });
    },
  });
}

// Live event feed for one run (the drawer). Polls faster than the runs list and
// keeps the previous page while refetching so the feed doesn't flicker.
export function useRunEvents(runId: string | null) {
  return useQuery({
    queryKey: runId ? runEventKeys.detail(runId) : ['run-events', 'none'],
    queryFn: () => getRunEvents(runId as string),
    enabled: !!runId,
    refetchInterval: 2000,
    placeholderData: keepPreviousData,
  });
}

// Dispatch (or retry) the coding agent for a task; the runs poll picks it up.
export function useDispatchAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, repository }: { taskId: string; repository?: string | null }) =>
      dispatchCodingAgent(taskId, repository),
    onSuccess: () => qc.invalidateQueries({ queryKey: codingKeys.all }),
  });
}

// ---- Executive layer (Command Center / Briefs / Autopilot / Ask) ----
// The unified decision queue — polls like the proposals list.
export function useDecisions() {
  return useQuery({ queryKey: decisionKeys.all, queryFn: getDecisions, refetchInterval: 10_000 });
}

// AI-workforce rollup; changes slowly, so a lazier poll.
export function useCodingStats() {
  return useQuery({ queryKey: codingStatsKeys.all, queryFn: getCodingStats, refetchInterval: 30_000 });
}

// Autopilot dial + today's counters. Polled so a tick's activity shows up
// without a reload; mutations below invalidate for instant feedback.
export function useAutopilot() {
  return useQuery({ queryKey: autopilotKeys.all, queryFn: getAutopilot, refetchInterval: 15_000 });
}

export function useUpdateAutopilot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AutopilotUpdate) => updateAutopilot(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: autopilotKeys.all }),
  });
}

export function useBriefs() {
  return useQuery({ queryKey: briefKeys.all, queryFn: () => listBriefs() });
}

// Latest brief — also drives the "Morning brief ready" toast in the
// notifications provider, hence the steady 60s poll.
export function useLatestBrief() {
  return useQuery({ queryKey: briefKeys.latest, queryFn: getLatestBrief, refetchInterval: 60_000 });
}

export function useGenerateBrief() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => generateBrief(),
    // briefKeys.all is a prefix of briefKeys.latest, so this refreshes both.
    onSuccess: () => qc.invalidateQueries({ queryKey: briefKeys.all }),
  });
}

export function useAskDeliveryManager() {
  return useMutation({
    mutationFn: ({ question, history }: { question: string; history: AskMessage[] }) =>
      askDeliveryManager(question, history),
  });
}

// ---- Dashboards ----
// These are heavy aggregates that change slowly, so they poll far less often
// than runs (2s) or decisions (10s). Over-polling them buys nothing and costs
// a full re-aggregation per tick.

export const dashboardKeys = {
  all: ['dashboard'] as const,
  sprint: (id?: string | null) => ['dashboard', 'sprint', id ?? 'active'] as const,
  teamLoad: (id?: string | null) => ['dashboard', 'team-load', id ?? 'active'] as const,
  velocity: (streamId?: string | null, limit?: number) =>
    ['dashboard', 'velocity', streamId ?? 'all', limit ?? 8] as const,
  delivery: (days: number, streamId?: string | null) =>
    ['dashboard', 'delivery', days, streamId ?? 'all'] as const,
  integrations: ['dashboard', 'integrations'] as const,
  costByStream: (days?: number | null) => ['dashboard', 'cost-by-stream', days ?? 'all'] as const,
  investment: (sprintId?: string | null) =>
    ['dashboard', 'investment', sprintId ?? 'active'] as const,
  investmentTrend: (sprints = 4) => ['dashboard', 'investment-trend', sprints] as const,
  workforceTrend: (days: number, bucket: number) =>
    ['dashboard', 'workforce-trend', days, bucket] as const,
  flow: (days: number, streamId?: string | null) =>
    ['dashboard', 'flow', days, streamId ?? 'all'] as const,
  roadmap: (streamId?: string | null) => ['dashboard', 'roadmap', streamId ?? 'all'] as const,
};

export const burndownKeys = {
  all: ['burndown'] as const,
  detail: (sprintId: string) => ['burndown', sprintId] as const,
};

export const forecastKeys = {
  all: ['forecast'] as const,
  detail: (sprintId: string) => ['forecast', sprintId] as const,
};

export function useSprintDashboard(sprintId?: string | null) {
  return useQuery({
    queryKey: dashboardKeys.sprint(sprintId),
    queryFn: () => getSprintDashboard(sprintId),
    refetchInterval: 60_000,
  });
}

export function useTeamLoad(sprintId?: string | null) {
  return useQuery({
    queryKey: dashboardKeys.teamLoad(sprintId),
    queryFn: () => getTeamLoad(sprintId),
    refetchInterval: 30_000,
  });
}

export function useVelocity(streamId?: string | null, limit = 8) {
  return useQuery({
    queryKey: dashboardKeys.velocity(streamId, limit),
    queryFn: () => getVelocity(streamId, limit),
    refetchInterval: 60_000,
  });
}

export function useDeliverySummary(days = 14, streamId?: string | null) {
  return useQuery({
    queryKey: dashboardKeys.delivery(days, streamId),
    queryFn: () => getDeliverySummary(days, streamId),
    refetchInterval: 60_000,
  });
}

export function useIntegrationHealth() {
  return useQuery({
    queryKey: dashboardKeys.integrations,
    queryFn: getIntegrationHealth,
    refetchInterval: 120_000,
  });
}

export function useCostByStream(days?: number | null) {
  return useQuery({
    queryKey: dashboardKeys.costByStream(days),
    queryFn: () => getCostByStream(days),
    refetchInterval: 60_000,
  });
}

export function useInvestmentProfile() {
  return useQuery({
    queryKey: dashboardKeys.investment(),
    queryFn: () => getInvestmentProfile(),
    refetchInterval: 60_000,
  });
}

export function useInvestmentTrend(sprints = 4) {
  return useQuery({
    queryKey: dashboardKeys.investmentTrend(sprints),
    queryFn: () => getInvestmentTrend(sprints),
    refetchInterval: 60_000,
  });
}

export function useWorkforceTrend(days = 84, bucketDays = 7) {
  return useQuery({
    queryKey: dashboardKeys.workforceTrend(days, bucketDays),
    queryFn: () => getWorkforceTrend(days, bucketDays),
    refetchInterval: 60_000,
  });
}

export function useFlowMetrics(days = 30, streamId?: string | null) {
  return useQuery({
    queryKey: dashboardKeys.flow(days, streamId),
    queryFn: () => getFlowMetrics(days, streamId),
    refetchInterval: 60_000,
  });
}

export function useRoadmap(streamId?: string | null) {
  return useQuery({
    queryKey: dashboardKeys.roadmap(streamId),
    queryFn: () => getRoadmap(streamId),
    refetchInterval: 60_000,
  });
}

export function useBurndown(sprintId?: string | null) {
  return useQuery({
    queryKey: burndownKeys.detail(sprintId ?? 'none'),
    queryFn: () => getBurndown(sprintId as string),
    enabled: Boolean(sprintId),
    refetchInterval: 60_000,
  });
}

export function useForecast(sprintId?: string | null) {
  return useQuery({
    queryKey: forecastKeys.detail(sprintId ?? 'none'),
    queryFn: () => getForecast(sprintId as string),
    enabled: Boolean(sprintId),
    refetchInterval: 60_000,
  });
}

/** Force today's snapshot, then refresh anything derived from it. */
export function useSnapshotSprint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sprintId: string) => snapshotSprint(sprintId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: burndownKeys.all });
      qc.invalidateQueries({ queryKey: dashboardKeys.all });
      qc.invalidateQueries({ queryKey: portfolioKeys.all });
    },
  });
}
