import { get } from "./client";

export type InsightRange = "24h" | "7d" | "30d" | "custom";
export type MetricConfidence = "exact" | "derived" | "inferred";

export interface InsightFilters {
  range?: InsightRange;
  since?: string;
  until?: string;
  workspace?: string;
  model?: string;
  source?: string;
  tool?: string;
  status?: string;
  confidence?: MetricConfidence;
  agent?: string;
}

export interface InsightWindow {
  since: string;
  until: string;
  timezone?: string;
}

export interface OverviewTotals {
  session_count: number;
  turn_count: number;
  total_tokens: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
}

export interface InsightsQuality {
  outcomes: Record<string, number>;
  validation: Record<string, number>;
  confidence: Record<MetricConfidence | "unknown", number>;
}

export interface InsightsOverview extends InsightWindow {
  totals: OverviewTotals;
  quality: InsightsQuality;
  by_agent?: AgentComparison[];
}

export interface TimeseriesPoint {
  timestamp: string;
  session_count: number;
  turn_count: number;
  total_tokens: number;
  success_count: number;
  failure_count: number;
  unknown_count: number;
}

export interface InsightsTimeseries extends InsightWindow {
  bucket: string;
  points: TimeseriesPoint[];
  by_agent?: AgentComparison[];
}

export interface AgentComparison {
  agent: string;
  session_count: number;
  turn_count: number;
  total_tokens: number;
  tool_call_count: number;
  success_count?: number;
  failure_count: number;
  unknown_count?: number;
}

export interface LatencyMeasure {
  count: number;
  p50: number;
  p95: number;
  average: number;
}

export interface LatencySummary {
  turn_duration_ms: LatencyMeasure;
  first_agent_latency_ms: LatencyMeasure;
  active_tool_ms: LatencyMeasure;
  confidence: Record<MetricConfidence | "unknown", number>;
}

export interface LatencyBucket {
  range: string;
  count: number;
}

export interface InsightsLatency extends InsightWindow {
  summary: LatencySummary;
  distribution: LatencyBucket[];
}

export interface ToolInsight {
  tool_name: string;
  tool_kind: string;
  call_count: number;
  failure_count: number;
  failure_rate: number;
  duration_count: number;
  average_ms: number;
  p50_ms: number;
  p95_ms: number;
}

export interface InsightsTools {
  items: ToolInsight[];
  by_agent?: AgentComparison[];
}

export type SkillEvidenceType = "available" | "candidate" | "loaded" | "inferred";

export interface SkillInsight {
  skill_name: string;
  description: string | null;
  scope: string | null;
  enabled: boolean | null;
  evidence: Record<SkillEvidenceType, number>;
  confidence: Record<MetricConfidence | "unknown", number>;
  session_count: number;
  last_seen_at: string | null;
}

export interface InsightsSkills {
  items: SkillInsight[];
}

export interface AgentNode {
  thread_id: string;
  session_id: string | null;
  title: string | null;
  model: string | null;
  workspace_id: string | null;
  is_subagent: boolean | null;
  outcome: string | null;
  tokens_used: number | null;
}

export interface AgentEdge {
  source: string;
  target: string;
  status: string;
  event_time?: string | null;
}

export interface InsightsAgents {
  nodes: AgentNode[];
  edges: AgentEdge[];
  truncated?: boolean;
}

export interface LocalDataSourceInsight {
  id: string;
  agent?: string | null;
  source_kind: string;
  source_path: string;
  source_format: string;
  parser_version: string;
  status: string;
  record_count: number;
  byte_size: number;
  coverage_started_at: string | null;
  coverage_ended_at: string | null;
  backfill_cutoff_at: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_error_kind: string | null;
  last_error: string | null;
  updated_at: string | null;
}

export interface InsightsSources {
  sources: LocalDataSourceInsight[];
}

export interface ActivitySpan {
  id: string;
  source_kind: string;
  source_record_id: string | null;
  turn_id: string | null;
  type: string;
  name: string | null;
  status: string;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  confidence: MetricConfidence;
  evidence_id: string | null;
}

export interface SessionTimeline {
  session_id: string;
  started_at?: string | null;
  completed_at?: string | null;
  spans: ActivitySpan[];
}

export interface InsightEvidence {
  id: string;
  source_id: string;
  category: string;
  level: string | null;
  recorded_at: string | null;
  summary: string | null;
  raw_text: string | null;
  raw_json: Record<string, unknown> | null;
  source_locator: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface InsightEvidenceSummary {
  id: string;
  source_id: string;
  source_kind: string;
  source_record_id: string;
  session_id: string | null;
  thread_id: string | null;
  turn_id: string | null;
  recorded_at: string | null;
  category: string;
  level: string | null;
  target: string | null;
  summary: string | null;
  raw_size: number;
}

export interface InsightEvidenceList {
  items: InsightEvidenceSummary[];
}

export interface InsightsExport {
  format: "csv" | "json";
  filename: string;
  media_type: string;
  content: string | Record<string, unknown>;
}

type FilterKey =
  | "time"
  | "agent"
  | "workspace"
  | "model"
  | "source"
  | "tool"
  | "status"
  | "confidence";

function insightQuery(
  filters: InsightFilters = {},
  allowed: ReadonlySet<FilterKey> = new Set(["time", "agent", "workspace", "model"])
): string {
  const query = new URLSearchParams();
  if (allowed.has("time")) {
    const rangeWindow = presetWindow(filters.range);
    const since = filters.since ?? rangeWindow?.since;
    const until = filters.until ?? rangeWindow?.until;
    if (since) query.set("from", since);
    if (until) query.set("to", until);
  }
  if (allowed.has("workspace") && filters.workspace) query.set("workspace_id", filters.workspace);
  if (allowed.has("agent") && filters.agent) query.set("agent", filters.agent);
  if (allowed.has("model") && filters.model) query.set("model", filters.model);
  if (allowed.has("source") && filters.source) query.set("source", filters.source);
  if (allowed.has("tool") && filters.tool) query.set("tool", filters.tool);
  if (allowed.has("status") && filters.status) query.set("status", filters.status);
  if (allowed.has("confidence") && filters.confidence) query.set("confidence", filters.confidence);
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

function presetWindow(range?: InsightRange): { since: string; until: string } | null {
  if (!range || range === "custom") return null;
  const hours = range === "24h" ? 24 : range === "7d" ? 7 * 24 : 30 * 24;
  const until = new Date();
  return {
    since: new Date(until.getTime() - hours * 60 * 60 * 1000).toISOString(),
    until: until.toISOString(),
  };
}

export function getInsightsOverview(filters?: InsightFilters): Promise<InsightsOverview> {
  return get<InsightsOverview>(`/api/insights/overview${insightQuery(filters)}`);
}

export function getInsightsTimeseries(filters?: InsightFilters): Promise<InsightsTimeseries> {
  return get<InsightsTimeseries>(`/api/insights/timeseries${insightQuery(filters)}`);
}

export function getInsightsLatency(filters?: InsightFilters): Promise<InsightsLatency> {
  return get<InsightsLatency>(`/api/insights/latency${insightQuery(filters)}`);
}

export function getInsightsTools(filters?: InsightFilters): Promise<InsightsTools> {
  return get<InsightsTools>(
    `/api/insights/tools${insightQuery(filters, new Set(["time", "agent", "workspace", "model", "tool", "status"]))}`
  );
}

export function getInsightsSkills(filters?: InsightFilters): Promise<InsightsSkills> {
  return get<InsightsSkills>(
    `/api/insights/skills${insightQuery(filters, new Set(["time", "agent", "workspace", "model", "confidence"]))}`
  );
}

export function getInsightsAgents(filters?: InsightFilters): Promise<InsightsAgents> {
  return get<InsightsAgents>(`/api/insights/agents${insightQuery(filters)}`);
}

export function getInsightsSources(filters?: InsightFilters): Promise<InsightsSources> {
  return get<InsightsSources>(
    `/api/insights/sources${insightQuery(filters, new Set(["agent", "source", "status"]))}`
  );
}

export function getSessionTimeline(sessionId: string): Promise<SessionTimeline> {
  return get<SessionTimeline>(
    `/api/insights/sessions/${encodeURIComponent(sessionId)}/timeline`
  );
}

export function getInsightEvidence(evidenceId: string): Promise<InsightEvidence> {
  return get<InsightEvidence>(`/api/insights/evidence/${encodeURIComponent(evidenceId)}`);
}

export function listInsightEvidence(
  sourceId: string,
  filters?: InsightFilters
): Promise<InsightEvidenceList> {
  const query = new URLSearchParams(
    insightQuery(filters, new Set(["time"])).replace(/^\?/, "")
  );
  query.set("source_id", sourceId);
  query.set("limit", "100");
  return get<InsightEvidenceList>(`/api/insights/evidence?${query.toString()}`);
}

export function getInsightsExportUrl(
  format: "csv" | "json",
  filters?: InsightFilters
): string {
  const query = insightQuery(filters, new Set(["time", "agent", "workspace", "model"]));
  const suffix = query ? `&${query.slice(1)}` : "";
  return `/api/insights/export?format=${format}${suffix}`;
}

export function getInsightsExport(
  format: "csv" | "json",
  filters?: InsightFilters
): Promise<InsightsExport> {
  return get<InsightsExport>(getInsightsExportUrl(format, filters));
}
