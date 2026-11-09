// Storage / retention API. The browser only ever sends a selection — every
// path is derived server-side from the backend catalog, and deletion happens
// through the Supervisor command queue.
import { get, post } from "./client";

export type RetentionRisk = "regenerable" | "history" | "runtime";

export interface RetentionCatalogEntry {
  id: string;
  agent: string;
  category: string;
  label: string;
  root: string;
  risk: RetentionRisk;
  unit: "file" | "dir";
  description: string;
}

export interface RetentionEntryUsage extends RetentionCatalogEntry {
  status: string;
  files: number;
  bytes: number;
  newest_mtime?: number | null;
  protected?: boolean;
}

export interface RetentionIndexTable {
  name: string;
  bytes: number;
  estimated_rows: number;
}

export interface RetentionIndexUsage {
  database_bytes: number;
  database_pretty: string;
  tables: RetentionIndexTable[];
}

export interface RetentionOverview {
  generated_at: string;
  cached: boolean;
  enabled: boolean;
  min_hours: number;
  large_run_bytes: number;
  protected_roots: string[];
  entries: RetentionEntryUsage[];
  index: RetentionIndexUsage;
}

export interface RetentionCatalog {
  enabled: boolean;
  min_hours: number;
  large_run_bytes: number;
  protected_roots: string[];
  entries: RetentionCatalogEntry[];
}

export interface RetentionSelection {
  keep_days: number;
  agents?: string[];
  categories?: string[];
  entry_ids?: string[];
  include_raw?: boolean;
  include_index?: boolean;
}

export interface RetentionSkipped {
  entry_id: string;
  agent: string;
  reason: string;
  files: number;
  bytes: number;
}

export interface RetentionPlanEntry extends RetentionCatalogEntry {
  status: string;
  files: number;
  bytes: number;
}

export interface RetentionPlanItem {
  path: string;
  entry_id: string;
  agent: string;
  category: string;
  risk: RetentionRisk;
  kind: "file" | "dir";
  bytes: number;
  mtime: number;
}

export interface RetentionBucket {
  bytes: number;
  files: number;
}

export interface RetentionPlan {
  floor_at: string;
  generated_at: string;
  keep_days: number;
  totals: {
    bytes: number;
    files: number;
    entries: number;
    sessions: number;
    sources: number;
  };
  by_agent: Record<string, RetentionBucket>;
  by_category: Record<string, RetentionBucket>;
  by_risk: Record<string, RetentionBucket>;
  index: { sessions: number; sources: number; sessions_by_entry: Record<string, number> };
  entries: RetentionPlanEntry[];
  skipped: RetentionSkipped[];
  truncated: boolean;
  plan_token: string;
  selection: Required<Pick<RetentionSelection, "keep_days">> & RetentionSelection;
  protected_roots: string[];
  large_run_bytes: number;
  items: RetentionPlanItem[];
  items_truncated: boolean;
}

export interface RetentionRun {
  id: string;
  command_id: number | null;
  requested_by: string;
  scope: string;
  status: "pending" | "running" | "applied" | "failed" | "refused" | string;
  keep_days: number;
  floor_at: string | null;
  selection: RetentionSelection;
  plan: Record<string, unknown>;
  result: Record<string, unknown>;
  bytes_freed: number;
  files_deleted: number;
  sessions_deleted: number;
  started_at: string | null;
  finished_at: string | null;
  error_kind: string | null;
  error_message: string | null;
  created_at: string | null;
}

export interface RetentionRunCreated {
  run: RetentionRun;
  command_id: number;
}

export function getRetentionOverview(refresh = false): Promise<RetentionOverview> {
  return get<RetentionOverview>(`/api/retention/overview${refresh ? "?refresh=true" : ""}`);
}

export function getRetentionCatalog(): Promise<RetentionCatalog> {
  return get<RetentionCatalog>("/api/retention/catalog");
}

export function previewRetention(selection: RetentionSelection): Promise<RetentionPlan> {
  return post<RetentionPlan>("/api/retention/preview", { selection });
}

export function createRetentionRun(body: {
  plan_token: string;
  confirm: boolean;
  acknowledge_large?: boolean;
}): Promise<RetentionRunCreated> {
  return post<RetentionRunCreated>("/api/retention/runs", body);
}

export function listRetentionRuns(limit = 20): Promise<{ runs: RetentionRun[] }> {
  return get<{ runs: RetentionRun[] }>(`/api/retention/runs?limit=${limit}`);
}

export function getRetentionRun(runId: string): Promise<RetentionRun> {
  return get<RetentionRun>(`/api/retention/runs/${encodeURIComponent(runId)}`);
}

const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

export function formatBytes(value: number | null | undefined): string {
  const bytes = typeof value === "number" && Number.isFinite(value) ? value : 0;
  if (bytes <= 0) return "0 B";
  let index = 0;
  let size = bytes;
  while (size >= 1024 && index < UNITS.length - 1) {
    size /= 1024;
    index += 1;
  }
  const digits = size >= 100 || index === 0 ? 0 : 1;
  return `${size.toFixed(digits)} ${UNITS[index]}`;
}

export function riskLabel(risk: RetentionRisk): string {
  if (risk === "regenerable") return "可重建";
  if (risk === "history") return "历史会话";
  return "运行状态";
}

export function skipReasonLabel(reason: string): string {
  switch (reason) {
    case "in_use":
      return "仍在保留窗口内";
    case "protected":
      return "已被配置锁定";
    case "missing":
      return "目录不存在";
    case "unbounded":
      return "目录过大无法确认，已跳过";
    case "truncated":
      return "扫描达到 AGENT_OBS_RETENTION_MAX_ENTRIES 上限";
    default:
      return reason;
  }
}
