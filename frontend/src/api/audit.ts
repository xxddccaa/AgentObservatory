// Audit trail reads (append-only activity log).
import { get } from "./client";

export interface AuditItem {
  id: number;
  actor_id: string | null;
  actor_type: string;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  request_id: string | null;
  session_id: string | null;
  detail: Record<string, unknown>;
  result: string;
  error_kind: string | null;
  error_message: string | null;
  created_at: string | null;
}

export interface AuditPage {
  items: AuditItem[];
  next_cursor: number | null;
}

export function listAudit(
  params: { limit?: number; action?: string; cursor?: number } = {}
): Promise<AuditPage> {
  const query = new URLSearchParams();
  if (params.limit) query.set("limit", String(params.limit));
  if (params.action) query.set("action", params.action);
  if (params.cursor) query.set("cursor", String(params.cursor));
  const qs = query.toString();
  return get<AuditPage>(`/api/audit${qs ? `?${qs}` : ""}`);
}
