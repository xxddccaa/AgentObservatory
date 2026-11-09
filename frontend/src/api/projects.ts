import { get } from "./client";
import type { HistoryList, WorkspaceKind } from "./history";

export type ProjectRange = "all" | "day" | "week" | "month";
export type ProjectTimeseriesRange = Exclude<ProjectRange, "all">;

export interface ProjectTokenTotals {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
}

export interface ProjectSummary {
  id: string;
  display_name: string;
  path: string;
  is_allowed: boolean;
  workspace_kind?: WorkspaceKind | null;
  workspace_confidence?: string | null;
  last_active_at: string | null;
  session_count: number;
  turn_count: number;
  all_time: ProjectTokenTotals;
  selected_range: ProjectTokenTotals & { range?: ProjectRange };
}

export interface ProjectsPage {
  items: ProjectSummary[];
  total: number;
  range: ProjectRange;
  since: string | null;
  until: string | null;
}

export interface ProjectSplit {
  agent?: string | null;
  model?: string | null;
  session_count: number;
  turn_count: number;
  total_tokens: number;
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  reasoning_output_tokens?: number;
}

export interface ProjectDetail {
  project: {
    id: string;
    display_name: string;
    path: string;
    is_allowed: boolean;
    workspace_kind?: WorkspaceKind | null;
    workspace_confidence?: string | null;
    first_active_at: string | null;
    last_active_at: string | null;
  };
  totals: {
    all_time: ProjectTokenTotals;
    day: ProjectTokenTotals;
    week: ProjectTokenTotals;
    month: ProjectTokenTotals;
  };
  counts: {
    session_count: number;
    turn_count: number;
  };
  by_agent: ProjectSplit[];
  by_model: ProjectSplit[];
}

export interface ProjectTimeseriesPoint {
  timestamp: string;
  session_count: number;
  turn_count: number;
  total_tokens: number;
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  reasoning_output_tokens?: number;
}

export interface ProjectTimeseries {
  since: string;
  until: string;
  bucket: string;
  points: ProjectTimeseriesPoint[];
}

export interface ListProjectsOptions {
  limit?: number;
  offset?: number;
  q?: string;
  range?: ProjectRange;
  agent?: string;
  model?: string;
}

export function listProjects(options: ListProjectsOptions = {}): Promise<ProjectsPage> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  if (options.q) params.set("q", options.q);
  if (options.range) params.set("range", options.range);
  if (options.agent) params.set("agent", options.agent);
  if (options.model) params.set("model", options.model);
  const query = params.toString();
  return get<ProjectsPage>(`/api/projects${query ? `?${query}` : ""}`);
}

export function getProject(workspaceId: string): Promise<ProjectDetail> {
  return get<ProjectDetail>(`/api/projects/${encodeURIComponent(workspaceId)}`);
}

export function getProjectTimeseries(
  workspaceId: string,
  range: ProjectTimeseriesRange = "day"
): Promise<ProjectTimeseries> {
  return get<ProjectTimeseries>(
    `/api/projects/${encodeURIComponent(workspaceId)}/timeseries?range=${encodeURIComponent(range)}`
  );
}

/** Project-scoped history query kept separate from the shared History API. */
export function listProjectSessions(
  workspaceId: string,
  options: { limit?: number; cursor?: string | null } = {}
): Promise<HistoryList> {
  const params = new URLSearchParams();
  params.set("workspace_id", workspaceId);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.cursor) params.set("cursor", options.cursor);
  return get<HistoryList>(`/api/codex/sessions?${params.toString()}`);
}
