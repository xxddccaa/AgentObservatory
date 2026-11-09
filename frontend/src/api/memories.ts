import { get, post } from "./client";

export type MemoryScope = "global" | "project" | "thread";
export type MemorySensitivity = "normal" | "sensitive";
export type MemoryExportFormat = "markdown" | "zip";
export type MemoryExportTarget = "generic" | "codex" | "stepcode" | "claude";

export interface MemoryWorkspace {
  id: string;
  name: string;
  path: string;
}

export interface MemoryEntry {
  id: string;
  agent: string;
  source_kind: string;
  source_path: string;
  thread_id: string | null;
  scope: MemoryScope;
  title: string;
  summary: string | null;
  content_bytes: number;
  source_updated_at: string | null;
  last_seen_at: string;
  is_present: boolean;
  sensitivity: MemorySensitivity;
  workspace: MemoryWorkspace | null;
  project_key: string | null;
  is_index: boolean;
}

export interface MemoryDetail extends MemoryEntry {
  content: string;
  content_redacted: boolean;
  content_hash: string;
  source_record_id: string;
  metadata: Record<string, unknown>;
}

export interface MemoryPage {
  entries: MemoryEntry[];
  next_cursor: string | null;
}

export interface MemorySource {
  agent: string;
  source_kind: string | null;
  status: string;
  source_path: string | null;
  record_count: number;
  last_success_at: string | null;
}

export interface MemoryContent {
  id: string;
  format: "raw" | "markdown";
  content: string;
  content_hash: string;
}

export interface MemoryExport {
  filename: string;
  media_type: string;
  encoding: "utf-8" | "base64";
  content: string;
}

export function listMemories(options: {
  limit?: number;
  cursor?: string | null;
  q?: string;
  agent?: string;
  scope?: MemoryScope | "";
  workspaceId?: string;
  includeMissing?: boolean;
} = {}): Promise<MemoryPage> {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit ?? 100));
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.q) params.set("q", options.q);
  if (options.agent) params.set("agent", options.agent);
  if (options.scope) params.set("scope", options.scope);
  if (options.workspaceId) params.set("workspace_id", options.workspaceId);
  if (options.includeMissing) params.set("include_missing", "true");
  return get<MemoryPage>(`/api/memories?${params.toString()}`);
}

export function getMemory(id: string): Promise<MemoryDetail> {
  return get<MemoryDetail>(`/api/memories/${encodeURIComponent(id)}`);
}

export function getMemorySources(): Promise<{ sources: MemorySource[] }> {
  return get<{ sources: MemorySource[] }>("/api/memories/sources");
}

export function getMemoryContent(
  id: string,
  format: "raw" | "markdown",
  purpose: "reveal" | "copy",
): Promise<MemoryContent> {
  return post<MemoryContent>(`/api/memories/${encodeURIComponent(id)}/content`, {
    format,
    purpose,
  });
}

export function exportMemories(
  ids: string[],
  format: MemoryExportFormat,
  target: MemoryExportTarget,
): Promise<MemoryExport> {
  return post<MemoryExport>("/api/memories/export", { ids, format, target });
}
