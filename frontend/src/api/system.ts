import { get } from "./client";
import type { ComponentState, HealthComponent } from "./health";

export interface SystemComponent extends Omit<HealthComponent, "status"> {
  status:
    | ComponentState
    | "running"
    | "offline"
    | "starting"
    | "stopped"
    | "crash_loop"
    | "unknown";
  pid?: number | null;
  last_success_at?: string | null;
  last_error?: string | null;
  restart_count?: number | null;
  runtime_status?: string | null;
  next_restart_at?: string | null;
}

export interface SupervisorStatus {
  status: SystemComponent["status"];
  pid?: number | null;
  started_at?: string | null;
  updated_at?: string | null;
  restart_count?: number | null;
  last_error?: string | null;
  message?: string | null;
}

export interface SystemStatus {
  status: "ok" | "degraded";
  environment?: string;
  generated_at?: string;
  supervisor?: SupervisorStatus | null;
  components: Record<string, SystemComponent>;
  recovery_actions: string[];
}

export interface SystemLogEntry {
  timestamp: string;
  component: string;
  level: string;
  message: string;
  request_id?: string | null;
  fields?: Record<string, unknown>;
}

export interface SystemLogsPage {
  items: SystemLogEntry[];
  next_cursor: string | null;
}

export interface SystemLogsQuery {
  component?: string;
  cursor?: string;
  limit?: number;
}

export function getSystemStatus(): Promise<SystemStatus> {
  return get<SystemStatus>("/api/system/status");
}

export function getSystemLogs(query: SystemLogsQuery = {}): Promise<SystemLogsPage> {
  const params = new URLSearchParams();
  if (query.component && query.component !== "all") params.set("component", query.component);
  if (query.cursor) params.set("cursor", query.cursor);
  params.set("limit", String(Math.min(500, Math.max(1, query.limit ?? 100))));
  return get<SystemLogsPage>(`/api/system/logs?${params.toString()}`);
}

export function componentEntries(
  status: SystemStatus | null
): Array<[string, SystemComponent]> {
  return status ? Object.entries(status.components) : [];
}
