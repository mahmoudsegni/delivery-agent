/**
 * TypeScript types matching backend SQLAlchemy models
 */

export interface TeamMember {
  id: string;
  name: string;
  email: string;
  role: string;
  capacity_hours_per_sprint: number;
  is_available: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface MemberAbsence {
  id: string;
  member_id: string;
  starts_on: string;
  ends_on: string;
  note: string | null;
}

export interface Task {
  id: string;
  title: string;
  description: string | null;
  acceptance_criteria: string | null;
  status: 'backlog' | 'todo' | 'in_progress' | 'review' | 'done';
  priority: 'low' | 'medium' | 'high' | 'critical';
  assignee_id: string | null;
  sprint_id: string | null;
  feature_id: string | null;
  story_points: number | null;
  estimated_hours: number | null;
  technology: string | null;
  /** Azure DevOps sub-area ("Storefront", "Smart Assistant", "Pulse"); null on
   *  items filed directly on the stream root. */
  project_name: string | null;
  /** Azure DevOps type: "Bug", "Issue", "Task", "Feature", ... null if local. */
  work_item_type: string | null;
  priority_order: number;
  external_id: string | null;
  created_at: string;
  updated_at: string | null;
}

export type TaskStatus = Task['status'];
export type TaskPriority = Task['priority'];

export interface TaskCreate {
  title: string;
  description?: string | null;
  acceptance_criteria?: string | null;
  status?: TaskStatus;
  priority?: TaskPriority;
  assignee_id?: string | null;
  sprint_id?: string | null;
  feature_id?: string | null;
  story_points?: number | null;
  estimated_hours?: number | null;
  technology?: string | null;
  priority_order?: number;
  external_id?: string | null;
}

export interface TaskUpdate {
  title?: string;
  description?: string | null;
  acceptance_criteria?: string | null;
  status?: TaskStatus;
  priority?: TaskPriority;
  assignee_id?: string | null;
  sprint_id?: string | null;
  feature_id?: string | null;
  story_points?: number | null;
  estimated_hours?: number | null;
  technology?: string | null;
  priority_order?: number;
  external_id?: string | null;
}

// Feature: a deliverable grouping of tasks within a stream (the "what").
export interface Feature {
  id: string;
  name: string;
  description: string | null;
  stream_id: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface FeatureCreate {
  name: string;
  description?: string | null;
  stream_id?: string | null;
}

// Sprint types
export type SprintStatus = 'planning' | 'active' | 'completed' | 'cancelled';

export interface Sprint {
  id: string;
  name: string;
  goal: string | null;
  start_date: string;
  end_date: string;
  status: SprintStatus;
  stream_id: string | null;
  capacity_hours: number;
  capacity_breakdown: Record<string, number> | null;
  velocity: number | null;
  retrospective_notes: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface SprintWithStats extends Sprint {
  total_tasks: number;
  completed_tasks: number;
  total_story_points: number;
  completed_story_points: number;
  progress_percentage: number;
}

export interface SprintCreate {
  name: string;
  goal?: string | null;
  start_date: string;
  // Omitted -> backend defaults to a two-week sprint (start + 13 days).
  end_date?: string;
  capacity_hours?: number;
  capacity_breakdown?: Record<string, number> | null;
}

export interface SprintUpdate {
  name?: string;
  goal?: string | null;
  start_date?: string;
  end_date?: string;
  status?: SprintStatus;
  capacity_hours?: number;
  capacity_breakdown?: Record<string, number> | null;
  velocity?: number;
  retrospective_notes?: string | null;
}

export interface SprintListResponse {
  sprints: Sprint[];
  total: number;
}

export interface BurndownDataPoint {
  date: string;
  /**
   * Null on a day with no reading — either a future date, or a day the daily
   * confidence snapshot did not run. Must break the chart line; rendering it
   * as 0 would claim the sprint finished.
   */
  remaining_points: number | null;
  ideal_points: number;
  /** Where the reading came from: a stored snapshot, or today's live state. */
  source?: 'snapshot' | 'live' | null;
}

export interface SprintBurndown {
  sprint_id: string;
  sprint_name: string;
  total_points: number;
  unit?: 'pts' | 'items';
  data_points: BurndownDataPoint[];
}

/** GET /sprints/{id}/forecast — Monte Carlo completion forecast. */
export interface SprintForecast {
  /** Sprint already finished — nothing to project. */
  done?: boolean;
  /** Fewer than two non-zero throughput weeks — no honest projection. */
  sufficient_history?: boolean;
  weekly_throughput?: number[];
  p50_days?: number;
  p85_days?: number;
  /** Fraction (0–1) of simulated runs finishing by the end date. */
  on_time_probability?: number;
  iterations?: number;
  unit?: 'pts' | 'items';
  remaining?: number;
}

// Agent types
export interface AgentRequest {
  type: string;
  payload: Record<string, unknown>;
}

export interface AgentRequestResponse {
  proposal_id: string;
  status: string;
}

export interface TaskListResponse {
  tasks: Task[];
  total: number;
}

export interface TaskStatusCount {
  status: string;
  count: number;
}

export interface TaskSummary {
  total: number;
  by_status: TaskStatusCount[];
  by_priority: Record<string, number>;
}

export interface ImportMockResponse {
  imported: number;
  tasks: Task[];
}

/** POST /tasks/sync — provider pull result (backend SyncResponse). */
export interface SyncResponse {
  provider: string;
  inserted: number;
  updated: number;
}

export interface Proposal {
  id: string;
  type: string;
  state: 'draft' | 'pending_review' | 'approved' | 'rejected' | 'executed' | 'failed';
  scope?: 'project' | 'stream' | 'portfolio';
  escalated?: boolean;
  stream_id?: string | null;
  content: Record<string, unknown> | null;
  reasoning: string | null;
  confidence: 'low' | 'medium' | 'high' | null;
  created_by_agent: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string | null;
}

// ---- Two-altitude views: Portfolio (executive) + Stream (PM) ----
export type StreamStatus = 'On track' | 'At risk' | 'Behind' | 'Ahead' | 'No data';
export type StreamDirection = 'improving' | 'steady' | 'declining';

// Number-light row for the executive portfolio (no precise confidence value).
export interface StreamSummary {
  id: string;
  name: string;
  owner_name: string | null;
  status: StreamStatus;
  direction: StreamDirection;
  sprint_count: number;
}

export interface EscalationItem {
  id: string;
  type: string;
  scope: string;
  escalated: boolean;
  stream_id: string | null;
  state: string;
  reasoning: string | null;
  confidence: 'low' | 'medium' | 'high' | null;
  created_at: string | null;
}

export interface PortfolioResponse {
  streams: StreamSummary[];
  escalations: EscalationItem[];
  // Executive additions (backend defaults them, so older payloads stay valid).
  confidence_trend: TrendPoint[];
  avg_confidence: number | null;
}

export interface TrendPoint {
  date: string;
  value: number;
}

export interface ConfidenceFactors {
  scope_remaining: number;
  velocity_vs_plan: number;
  open_blockers: number;
  untested_surface: number;
}

// Precise per-sprint confidence for the PM drill-in.
export interface SprintConfidence {
  sprint_id: string;
  sprint_name: string;
  status: StreamStatus;
  confidence_value: number | null;
  factors: ConfidenceFactors | null;
  trend: TrendPoint[];
}

export interface StreamProposal {
  id: string;
  type: string;
  state: string;
  scope: string;
  content: Record<string, unknown> | null;
  reasoning: string | null;
  confidence: 'low' | 'medium' | 'high' | null;
  created_at: string | null;
}

export interface StreamDetail {
  id: string;
  name: string;
  description: string | null;
  owner_name: string | null;
  status: StreamStatus;
  direction: StreamDirection;
  sprints: SprintConfidence[];
  proposals: StreamProposal[];
}

export interface StreamListItem {
  id: string;
  name: string;
}

// ---- Coding-agent PR loop ----
export type AgentRunStatus =
  | 'dispatched' | 'groomed' | 'skipped_human_only' | 'running'
  | 'ci_pending' | 'ci_failed' | 'pr_open' | 'merged' | 'failed';

export interface AgentRun {
  id: string;
  task_id: string;
  proposal_id: string | null;
  status: AgentRunStatus | string;
  branch_name: string | null;
  repository: string | null;
  pr_id: string | null;
  pr_url: string | null;
  iteration: number;
  cost_estimate: number;
  error: string | null;
  created_at: string;
  updated_at: string | null;
}

// One streamed progress event for a run (lifecycle | assistant | inspect |
// file_edit | command | result | error), polled from /coding/runs/{id}/events.
export interface AgentRunEvent {
  seq: number;
  kind: string;
  message: string;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface RunEventsResponse {
  events: AgentRunEvent[];
  run_status: string;
}

export interface RepoCreateResponse {
  name: string;
  web_url: string;
  created: boolean;
  seeded: boolean;
}

// ---- Executive layer: Command Center / Briefs / Autopilot / Ask ----

// One outstanding decision for the Command Center queue: every pending_review
// proposal plus approved coding_pr proposals whose run is still pr_open.
export interface DecisionItem {
  id: string;
  type: string;
  state: string;
  scope: string;
  escalated: boolean;
  stream_id: string | null;
  reasoning: string | null;
  confidence: 'low' | 'medium' | 'high' | null;
  created_by_agent: string | null;
  created_at: string;
  summary: string;
  run_id?: string | null;
  run_status?: string | null;
  pr_url?: string | null;
  task_id?: string | null;
  task_title?: string | null;
  repository?: string | null;
}

// AI-workforce rollup (GET /coding/stats).
export interface CodingStats {
  total_runs: number;
  merged: number;
  pr_open: number;
  failed: number;
  active_now: number;
  // 0-100 percentage; null until at least one run has finished (merged/failed).
  success_rate: number | null;
  total_cost: number;
  avg_minutes_to_pr: number | null;
  hours_saved: number;
  runs_today: number;
  dispatches_today_autopilot: number;
}

export type AutopilotMode = 'off' | 'propose' | 'auto';

export interface AutopilotSkip {
  task_id: string;
  title: string;
  reason: string;
}

export interface AutopilotToday {
  triaged: number;
  dispatched: number;
  suggested: number;
  skipped: number;
  nudged: number;
  escalated: number;
  skips: AutopilotSkip[];
}

// Runtime autopilot dial (GET/PUT /autopilot).
export interface AutopilotState {
  mode: AutopilotMode;
  daily_limit: number;
  repository: string | null;
  stale_task_days: number;
  blocker_escalation_days: number;
  today: AutopilotToday;
  budget_remaining: number;
}

export interface AutopilotUpdate {
  mode?: AutopilotMode;
  daily_limit?: number;
  repository?: string | null;
}

// Groq-written morning-brief body (content JSONB — fields may be sparse).
export interface BriefContent {
  headline?: string;
  shipped?: string[];
  ai_delivered?: string[];
  risks?: string[];
  needs_decision?: string[];
  numbers?: Record<string, string | number>;
  narrative?: string;
}

export interface Brief {
  id: string;
  brief_date: string;
  headline: string;
  content: BriefContent | null;
  created_at: string;
}

export interface AskMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface AskResponse {
  answer: string;
  generated_at: string;
}

export interface HealthResponse {
  status: string;
  version: string;
}

export interface ApiResponse<T> {
  data: T;
  message?: string;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

// ============================================================
// Dashboards (PM + executive) — GET /api/v1/dashboard/*
//
// Nullability is load-bearing. `velocity` is null until a sprint completes,
// `confidence` is null with no snapshot, `remaining_points` is null on a day
// with no reading. Render those as "—" and break chart lines; never coerce to 0.
// ============================================================

export interface ReleaseReadiness {
  sprint_id: string | null;
  sprint_name: string | null;
  completion_rate: number;
  tasks_done: number;
  tasks_remaining: number;
  /** Items with no update for N days. NOT the same as "blocked". */
  tasks_stale: number;
  risk_level: 'low' | 'medium' | 'high';
  recommendation: string;
}

export interface StaleTask {
  id: string;
  title: string;
  assignee_name: string | null;
  story_points: number | null;
  days_idle: number;
}

export interface TeamLoadRow {
  id: string;
  name: string;
  role: string | null;
  capacity_hours: number;
  absent_hours: number;
  assigned_hours: number;
  open_points: number;
  in_progress: number;
  done: number;
  stale: number;
}

export interface SprintDashboard {
  sprint_id: string;
  name: string;
  goal: string | null;
  start_date: string;
  end_date: string;
  status: string;
  stream_id: string | null;
  stream_name: string | null;
  capacity_hours: number | null;
  total_tasks: number;
  completed_tasks: number;
  total_story_points: number;
  completed_story_points: number;
  progress_percentage: number;
  days_remaining: number | null;
  velocity: number | null;
  confidence: number | null;
  factors: ConfidenceFactors | null;
  readiness: ReleaseReadiness;
  stale_tasks: StaleTask[];
  team_load: TeamLoadRow[];
}

export interface VelocityPoint {
  sprint_id: string;
  name: string;
  start_date: string;
  status: string;
  committed_points: number;
  completed_points: number;
  velocity: number | null;
  in_flight: boolean;
}

export interface VelocityResponse {
  points: VelocityPoint[];
  rolling_mean: number | null;
  sigma: number | null;
  sprints_counted: number;
  unit?: 'pts' | 'items';
}

export interface ThroughputPoint {
  date: string;
  completed: number;
  points: number;
}

export interface DeliverySummary {
  days: number;
  completed: number;
  points: number;
  daily: ThroughputPoint[];
  /** Completion date inferred from updated_at — label it in the UI. */
  approximate: boolean;
}

export interface IntegrationHealth {
  provider: string | null;
  last_sync_at: string | null;
  last_status: string | null;
  last_error: string | null;
  inserted_count: number | null;
  updated_count: number | null;
  last_brief_date: string | null;
  last_snapshot_date: string | null;
  snapshot_lag_days: number | null;
}

/** Mirrors AgentAnalysisService.grooming_data(): counts only, items are the Telegram drill-down. */
export interface GroomingSummary {
  summary: { stale: number; unestimated: number; unassigned: number };
  items: { category: string; ref: string; title: string; days: number | null }[];
}

export interface LlmUsage {
  days: number;
  calls: number;
  total_tokens: number;
  today_tokens: number;
  daily_budget: number;
  avg_latency_ms: number;
  by_feature: Record<string, { calls: number; total_tokens: number }>;
}

export interface CostByStream {
  stream_id: string | null;
  stream_name: string;
  cost: number;
  runs: number;
  merged: number;
}

export interface CostByStreamResponse {
  streams: CostByStream[];
  total_cost: number;
  /** Spend on runs whose task has no sprint/stream — reported, never hidden. */
  unattributed_cost: number;
}

export interface InvestmentRow {
  work_item_type: string;
  total_points: number;
  done_points: number;
  open_points: number;
  item_count: number;
}

export interface InvestmentProfileResponse {
  rows: InvestmentRow[];
  sprint_id: string | null;
  sprint_name: string | null;
  total_points: number;
}

export interface InvestmentTrendSprint {
  sprint_id: string;
  name: string;
  end_date: string;
  rows: InvestmentRow[];
}

export interface InvestmentTrendResponse {
  sprints: InvestmentTrendSprint[];
}

export interface WorkforcePoint {
  date: string;
  dispatched: number;
  merged: number;
  failed: number;
  cost: number;
}

export interface WorkforceTrend {
  points: WorkforcePoint[];
  bucket_days: number;
}

export interface FlowBucket {
  date: string;
  count: number;
}

export interface CycleTimeItem {
  task_id: string;
  title: string;
  days: number;
  completed_on: string;
}

export interface StatusCount {
  date: string;
  backlog: number;
  todo: number;
  in_progress: number;
  review: number;
  done: number;
}

export interface AgingItem {
  task_id: string;
  title: string;
  status: string;
  days_in_status: number;
  assignee_name: string | null;
}

export interface FlowMetrics {
  days: number;
  history_starts: string | null;
  /** True while the only events are the migration's synthetic backfill. */
  backfilled_only: boolean;
  throughput: FlowBucket[];
  cycle_time_p50: number | null;
  cycle_time_p85: number | null;
  lead_time_p50: number | null;
  lead_time_p85: number | null;
  items: CycleTimeItem[];
  cfd: StatusCount[];
  aging_wip: AgingItem[];
}

export interface RoadmapItem {
  id: string;
  name: string;
  stream_id: string | null;
  stream_name: string | null;
  /** Null when no task in the feature is assigned to a sprint yet. */
  start_date: string | null;
  end_date: string | null;
  status: string;
  progress: number;
  total_tasks: number;
  completed_tasks: number;
  total_points: number;
  completed_points: number;
}

export interface RoadmapResponse {
  items: RoadmapItem[];
  window_start: string | null;
  window_end: string | null;
  today: string;
}

// Global search (Ctrl/Cmd+K palette)
export interface TaskHit {
  id: string;
  title: string;
  status: string;
  sprint_id: string | null;
  sprint_name: string | null;
  assignee_name: string | null;
}
export interface MemberHit {
  id: string;
  name: string;
  role: string;
}
export interface SearchResults {
  tasks: TaskHit[];
  members: MemberHit[];
}
