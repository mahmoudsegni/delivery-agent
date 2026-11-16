// Thin fetch wrappers over the backend API (proxied at /api/v1 by Vite).
import type {
  AgentRun, AskMessage, AskResponse, AutopilotState, AutopilotUpdate, Brief, CodingStats,
  DecisionItem, Feature, FeatureCreate, PortfolioResponse, Proposal, RepoCreateResponse,
  RunEventsResponse, Sprint, SprintCreate, StreamDetail, StreamListItem, Task, TaskCreate, TaskUpdate, TeamMember,
  CostByStreamResponse, DeliverySummary, FlowMetrics, IntegrationHealth, RoadmapResponse,
  SprintBurndown, SprintDashboard, SprintForecast, TeamLoadRow, VelocityResponse, WorkforceTrend,
} from '../types';

// Base URL for the backend. Empty in local dev (`npm run dev`) so paths stay
// relative ("/api/v1/...") and flow through the Vite proxy. Set at build time
// (VITE_API_BASE_URL) for the Azure DevOps extension bundle, where there is no
// proxy and the iframe must call an absolute, reachable backend URL.
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? '';

// ---- Who is using the app ----
// There is no login on the LAN deployment. Decisions (approve / reject / merge)
// must still record a person, so the app asks for a name once, remembers it in
// this browser, and sends it as X-User-Name on every request.
const USER_KEY = 'da-user-name';

export function getUserName(): string {
  try { return localStorage.getItem(USER_KEY) || ''; } catch { return ''; }
}

export function setUserName(name: string): void {
  try { localStorage.setItem(USER_KEY, name.trim()); } catch { /* private mode */ }
}

const DECISION_PATH = /\/(approve|reject|merge|revise)$/;

function ensureUserName(): string {
  let name = getUserName();
  while (!name) {
    const entered = window.prompt('Your name (recorded on this decision):');
    if (entered === null) break; // cancelled — the server records "unknown"
    name = entered.trim();
    if (name) setUserName(name);
  }
  return name;
}

// Single fetch seam: prepends the configurable base URL and attaches the
// user's name (the one place to swap in real auth later).
export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const isDecision = (init?.method || 'GET').toUpperCase() === 'POST' && DECISION_PATH.test(path);
  const name = isDecision ? ensureUserName() : getUserName();
  const headers = new Headers(init?.headers);
  if (name) headers.set('X-User-Name', encodeURIComponent(name));
  return fetch(`${API_BASE}${path}`, { ...init, headers });
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as any).detail || `Request failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

// ---- Tasks ----
// Real TFS syncs run to a few thousand rows; a 500 default silently hid the
// tail of the board. Keep this above the expected import size.
export async function listTasks(limit = 3000): Promise<Task[]> {
  const data = await json<{ tasks: Task[]; total: number }>(await apiFetch(`/api/v1/tasks?limit=${limit}`));
  return data.tasks;
}

export async function createTask(body: TaskCreate): Promise<Task> {
  return json<Task>(await apiFetch('/api/v1/tasks', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }));
}

export async function updateTask(id: string, body: TaskUpdate): Promise<Task> {
  return json<Task>(await apiFetch(`/api/v1/tasks/${id}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(body) }));
}

// ---- Team ----
export async function listTeamMembers(): Promise<TeamMember[]> {
  const data = await json<{ members: TeamMember[]; total: number }>(await apiFetch('/api/v1/team/members'));
  return data.members;
}

// ---- Sprints ----
export async function getSprints(status?: string): Promise<Sprint[]> {
  const url = status ? `/api/v1/sprints?status=${status}` : '/api/v1/sprints';
  const data = await json<{ sprints: Sprint[]; total: number }>(await apiFetch(url));
  return data.sprints;
}

export async function createSprint(body: SprintCreate): Promise<Sprint> {
  return json<Sprint>(await apiFetch('/api/v1/sprints', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }));
}

// ---- Features ----
export async function getFeatures(streamId?: string): Promise<Feature[]> {
  const url = streamId ? `/api/v1/features?stream_id=${streamId}` : '/api/v1/features';
  const data = await json<{ features: Feature[]; total: number }>(await apiFetch(url));
  return data.features;
}

export async function createFeature(body: FeatureCreate): Promise<Feature> {
  return json<Feature>(await apiFetch('/api/v1/features', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }));
}

// ---- Proposals ----
export interface CreateProposalBody {
  type: string;
  content?: Record<string, unknown> | null;
  reasoning?: string | null;
  confidence?: string | null;
  created_by_agent?: string | null;
}

export async function createProposal(body: CreateProposalBody): Promise<{ id: string }> {
  return json<{ id: string }>(await apiFetch('/api/v1/agent/proposals', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }));
}

export async function listProposals(state?: string, limit = 200): Promise<Proposal[]> {
  const url = state
    ? `/api/v1/agent/proposals?state=${state}&limit=${limit}`
    : `/api/v1/agent/proposals?limit=${limit}`;
  return json<Proposal[]>(await apiFetch(url));
}

export async function approveProposal(id: string, reason?: string): Promise<unknown> {
  return json(await apiFetch(`/api/v1/agent/proposals/${id}/approve`, {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(reason ? { reason } : {}),
  }));
}

export async function rejectProposal(id: string, reason: string): Promise<unknown> {
  return json(await apiFetch(`/api/v1/agent/proposals/${id}/reject`, {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ reason }),
  }));
}

// ---- Agent analyses (computed server-side from the synced ADO data) ----
export type AnalysisKind = 'sprint_planning' | 'blocker_detection' | 'release_management';

export async function runAnalysis(kind: AnalysisKind): Promise<{ proposal_id: string; report: any }> {
  return json(await apiFetch(`/api/v1/agent/analysis/${kind}`, { method: 'POST' }));
}

// ---- Standup (web form; the link is shared in-app and DM'd on Webex) ----
export interface StandupRound {
  id: string;
  status: string;
  entries: { member_id: string; member_name: string | null; completed: boolean }[];
}

export async function getStandupRound(roundId: string): Promise<StandupRound> {
  return json<StandupRound>(await apiFetch(`/api/v1/standup/rounds/${roundId}`));
}

export async function submitStandup(
  roundId: string,
  body: { member_id: string; yesterday: string; today: string; blockers: string },
): Promise<{ recorded: boolean; round_complete: boolean }> {
  return json(await apiFetch(`/api/v1/standup/rounds/${roundId}/entries`, {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body),
  }));
}

// ---- Coding agent (dispatch from the authoring form) ----
export async function dispatchCodingAgent(
  taskId: string, repository?: string | null,
): Promise<{ run_id: string; status: string }> {
  return json(await apiFetch('/api/v1/coding/dispatch', {
    method: 'POST', headers: JSON_HEADERS,
    body: JSON.stringify({ task_id: taskId, repository: repository || null }),
  }));
}

export async function getCodingRepositories(): Promise<string[]> {
  const data = await json<{ repositories: string[] }>(await apiFetch('/api/v1/coding/repositories'));
  return data.repositories;
}

// Creates the repo in Azure DevOps; seed=true pushes a sample app so the agent
// has something to build on.
export async function createCodingRepository(name: string, seed = true): Promise<RepoCreateResponse> {
  return json<RepoCreateResponse>(await apiFetch('/api/v1/coding/repositories', {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ name, seed }),
  }));
}

export async function listCodingRuns(): Promise<AgentRun[]> {
  return json<AgentRun[]>(await apiFetch('/api/v1/coding/runs'));
}

export async function getCodingRun(taskId: string): Promise<AgentRun | null> {
  const res = await apiFetch(`/api/v1/coding/runs/${taskId}`);
  if (!res.ok) return null;
  return res.json();
}

export async function getRunEvents(runId: string, afterSeq = 0): Promise<RunEventsResponse> {
  return json<RunEventsResponse>(await apiFetch(`/api/v1/coding/runs/${runId}/events?after_seq=${afterSeq}`));
}

export async function mergeCodingRun(runId: string): Promise<unknown> {
  return json(await apiFetch(`/api/v1/coding/runs/${runId}/merge`, { method: 'POST', headers: JSON_HEADERS }));
}

export async function reviseCodingRun(runId: string, comment: string): Promise<unknown> {
  return json(await apiFetch(`/api/v1/coding/runs/${runId}/revise`, {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ comment }),
  }));
}

// ---- Portfolio / Streams (two-altitude views) ----
export async function getPortfolio(): Promise<PortfolioResponse> {
  return json<PortfolioResponse>(await apiFetch('/api/v1/portfolio'));
}

export async function getStreams(): Promise<StreamListItem[]> {
  return json<StreamListItem[]>(await apiFetch('/api/v1/streams'));
}

export async function getStream(id: string): Promise<StreamDetail> {
  return json<StreamDetail>(await apiFetch(`/api/v1/streams/${id}`));
}

// ---- Executive layer (Command Center / Briefs / Autopilot / Ask) ----
export async function getDecisions(): Promise<DecisionItem[]> {
  return json<DecisionItem[]>(await apiFetch('/api/v1/decisions'));
}

export async function getCodingStats(): Promise<CodingStats> {
  return json<CodingStats>(await apiFetch('/api/v1/coding/stats'));
}

export async function getAutopilot(): Promise<AutopilotState> {
  return json<AutopilotState>(await apiFetch('/api/v1/autopilot'));
}

export async function updateAutopilot(body: AutopilotUpdate): Promise<AutopilotState> {
  return json<AutopilotState>(await apiFetch('/api/v1/autopilot', {
    method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body),
  }));
}

export async function listBriefs(limit = 30): Promise<Brief[]> {
  return json<Brief[]>(await apiFetch(`/api/v1/briefs?limit=${limit}`));
}

// Null-safe: a 404 or an empty/null body just means "no brief yet".
export async function getLatestBrief(): Promise<Brief | null> {
  const res = await apiFetch('/api/v1/briefs/latest');
  if (res.status === 404) return null;
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as any).detail || `Request failed: ${res.status}`);
  }
  const data = await res.json().catch(() => null);
  return (data as Brief | null) ?? null;
}

export async function generateBrief(): Promise<Brief> {
  return json<Brief>(await apiFetch('/api/v1/briefs/generate', { method: 'POST', headers: JSON_HEADERS }));
}

export async function askDeliveryManager(question: string, history: AskMessage[]): Promise<AskResponse> {
  return json<AskResponse>(await apiFetch('/api/v1/agent/ask', {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ question, history }),
  }));
}

// ---- Dashboards (PM + executive) ----
// Every one of these is a read-only aggregate; none mutate.

export async function getSprintDashboard(sprintId?: string | null): Promise<SprintDashboard | null> {
  const path = sprintId ? `/api/v1/dashboard/sprint/${sprintId}` : '/api/v1/dashboard/sprint/active';
  return json<SprintDashboard | null>(await apiFetch(path));
}

export async function getTeamLoad(sprintId?: string | null): Promise<TeamLoadRow[]> {
  const qs = sprintId ? `?sprint_id=${encodeURIComponent(sprintId)}` : '';
  return json<TeamLoadRow[]>(await apiFetch(`/api/v1/dashboard/team-load${qs}`));
}

export async function getVelocity(streamId?: string | null, limit = 8): Promise<VelocityResponse> {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (streamId) qs.set('stream_id', streamId);
  return json<VelocityResponse>(await apiFetch(`/api/v1/dashboard/velocity?${qs}`));
}

export async function getDeliverySummary(days = 14, streamId?: string | null): Promise<DeliverySummary> {
  const qs = new URLSearchParams({ days: String(days) });
  if (streamId) qs.set('stream_id', streamId);
  return json<DeliverySummary>(await apiFetch(`/api/v1/dashboard/delivery-summary?${qs}`));
}

export async function getIntegrationHealth(): Promise<IntegrationHealth> {
  return json<IntegrationHealth>(await apiFetch('/api/v1/dashboard/integrations'));
}

export async function getCostByStream(days?: number | null): Promise<CostByStreamResponse> {
  const qs = days ? `?days=${days}` : '';
  return json<CostByStreamResponse>(await apiFetch(`/api/v1/dashboard/cost-by-stream${qs}`));
}

export async function getWorkforceTrend(days = 84, bucketDays = 7): Promise<WorkforceTrend> {
  const qs = new URLSearchParams({ days: String(days), bucket_days: String(bucketDays) });
  return json<WorkforceTrend>(await apiFetch(`/api/v1/dashboard/workforce-trend?${qs}`));
}

export async function getFlowMetrics(days = 30, streamId?: string | null): Promise<FlowMetrics> {
  const qs = new URLSearchParams({ days: String(days) });
  if (streamId) qs.set('stream_id', streamId);
  return json<FlowMetrics>(await apiFetch(`/api/v1/dashboard/flow?${qs}`));
}

export async function getRoadmap(streamId?: string | null): Promise<RoadmapResponse> {
  const qs = streamId ? `?stream_id=${encodeURIComponent(streamId)}` : '';
  return json<RoadmapResponse>(await apiFetch(`/api/v1/dashboard/roadmap${qs}`));
}

// Burndown had types in the frontend but no fetcher — the component that used
// it was deleted before one was written.
export async function getBurndown(sprintId: string): Promise<SprintBurndown> {
  return json<SprintBurndown>(await apiFetch(`/api/v1/sprints/${sprintId}/burndown`));
}

export async function getForecast(sprintId: string): Promise<SprintForecast> {
  return json<SprintForecast>(await apiFetch(`/api/v1/sprints/${sprintId}/forecast`));
}

// Forces today's confidence snapshot so a PM does not wait for the 23:30 beat
// job to see the burndown move. Idempotent per (sprint, date).
export async function snapshotSprint(sprintId: string): Promise<{ snapshotted: boolean }> {
  return json<{ snapshotted: boolean }>(
    await apiFetch(`/api/v1/dashboard/sprint/${sprintId}/snapshot`, { method: 'POST' }),
  );
}
