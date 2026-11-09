<template>
  <div class="memories-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">Portable CLI knowledge</p>
        <h1>Memories</h1>
      </div>
      <div class="filters">
        <label>
          <span class="sr-only">Filter memories by agent</span>
          <select v-model="agent" aria-label="Filter memories by agent" @change="applyFilters">
            <option value="">All agents</option>
            <option value="codex">Codex</option>
            <option value="stepcode">StepCode</option>
            <option value="claude">Claude</option>
            <option value="dsh">DSH</option>
            <option value="opencode">OpenCode</option>
          </select>
        </label>
        <label>
          <span class="sr-only">Filter memories by scope</span>
          <select v-model="scope" aria-label="Filter memories by scope" @change="applyFilters">
            <option value="">All scopes</option>
            <option value="global">Global</option>
            <option value="project">Project</option>
            <option value="thread">Thread</option>
          </select>
        </label>
        <div class="search-box">
          <Search :size="15" aria-hidden="true" />
          <input
            v-model.trim="query"
            type="search"
            aria-label="Search memories"
            placeholder="Title, content, source"
            @keydown.enter="applyFilters"
          />
        </div>
        <label class="missing-toggle">
          <input v-model="includeMissing" type="checkbox" @change="applyFilters" />
          <span>Show missing</span>
        </label>
        <button class="icon-action" type="button" title="Refresh memories" @click="refresh">
          <RefreshCw :size="16" aria-hidden="true" />
          <span>Refresh</span>
        </button>
      </div>
    </header>

    <section class="source-strip" aria-label="Memory sources">
      <div
        v-for="source in sources"
        :key="source.agent"
        class="source-item"
        :data-status="source.status"
        :title="source.source_path || 'No known memory source'"
      >
        <span class="source-dot" aria-hidden="true" />
        <strong>{{ agentLabel(source.agent) }}</strong>
        <span>{{ sourceStatus(source) }}</span>
        <b>{{ source.record_count }}</b>
      </div>
    </section>

    <section v-if="selectedIds.size" class="bulk-bar" aria-label="Selected memory actions">
      <strong>{{ selectedIds.size }} selected</strong>
      <label>
        <span>Target</span>
        <select v-model="exportTarget" aria-label="Export target CLI">
          <option value="generic">Generic Markdown</option>
          <option value="codex">Codex</option>
          <option value="stepcode">StepCode</option>
          <option value="claude">Claude</option>
        </select>
      </label>
      <button
        type="button"
        :disabled="!auth.canOperate || exporting"
        title="Download selected memories as Markdown"
        @click="downloadSelected('markdown')"
      >
        <FileText :size="15" aria-hidden="true" />
        Markdown
      </button>
      <button
        type="button"
        :disabled="!auth.canOperate || exporting"
        title="Download selected memories with manifest"
        @click="downloadSelected('zip')"
      >
        <Archive :size="15" aria-hidden="true" />
        ZIP
      </button>
      <button type="button" class="clear-selection" @click="clearSelection">Clear</button>
      <span v-if="!auth.canOperate" class="permission-note">Operator access required to export.</span>
    </section>

    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <div v-if="error" class="state state--error">{{ error }}</div>
    <div v-else-if="loading" class="memory-shell memory-shell--loading" aria-label="Loading memories">
      <div class="skeleton-list" />
      <div class="skeleton-detail" />
    </div>
    <div v-else-if="entries.length === 0" class="state">
      No indexed memory entries match these filters.
    </div>
    <div v-else class="memory-shell">
      <aside class="memory-list" aria-label="Memory entries">
        <article
          v-for="entry in entries"
          :key="entry.id"
          class="memory-row"
          :class="{ 'memory-row--active': detail?.id === entry.id }"
        >
          <label class="row-select" :title="`Select ${entry.title}`">
            <input
              type="checkbox"
              :checked="selectedIds.has(entry.id)"
              :aria-label="`Select ${entry.title}`"
              @change="toggleSelected(entry.id)"
            />
          </label>
          <button type="button" class="row-open" @click="openEntry(entry.id)">
            <span class="row-title">
              <strong :title="entry.title">{{ entry.title }}</strong>
              <AgentBadge :agent="entry.agent" />
              <ShieldAlert
                v-if="entry.sensitivity === 'sensitive'"
                :size="14"
                class="sensitive-icon"
                aria-label="Sensitive content"
              />
            </span>
            <span class="row-summary">{{ entry.summary || "No summary" }}</span>
            <span class="row-meta">
              <span>{{ scopeLabel(entry.scope) }}</span>
              <span>{{ entry.workspace?.name || entry.project_key || "Unassigned" }}</span>
              <span>{{ formatBytes(entry.content_bytes) }}</span>
              <time :title="absoluteTime(entry.source_updated_at)">
                {{ relativeTime(entry.source_updated_at) }}
              </time>
              <span v-if="!entry.is_present" class="missing-label">missing</span>
            </span>
          </button>
        </article>
        <button v-if="nextCursor" class="load-more" type="button" @click="load(false)">
          Load more
        </button>
      </aside>

      <main class="memory-detail">
        <div v-if="detailLoading" class="detail-state">Loading memory…</div>
        <div v-else-if="!detail" class="detail-state">
          Select a memory entry to inspect its complete source snapshot.
        </div>
        <template v-else>
          <header class="detail-header">
            <div class="detail-heading">
              <div>
                <h2>{{ detail.title }}</h2>
                <p>{{ detail.workspace?.path || detail.project_key || detail.thread_id || detail.scope }}</p>
              </div>
              <div class="detail-badges">
                <AgentBadge :agent="detail.agent" />
                <span class="badge">{{ scopeLabel(detail.scope) }}</span>
                <span v-if="detail.is_index" class="badge">index</span>
                <span v-if="!detail.is_present" class="badge badge--warn">source missing</span>
              </div>
            </div>
            <div class="detail-actions">
              <button
                v-if="detail.content_redacted"
                type="button"
                :disabled="!auth.canOperate || actionBusy"
                title="Reveal complete sensitive memory"
                @click="revealContent"
              >
                <Eye :size="15" aria-hidden="true" />
                Reveal
              </button>
              <button
                type="button"
                :disabled="!auth.canOperate || actionBusy"
                title="Copy exact memory content"
                @click="copyContent('raw')"
              >
                <Check v-if="copied === 'raw'" :size="15" aria-hidden="true" />
                <Copy v-else :size="15" aria-hidden="true" />
                {{ copied === "raw" ? "Copied" : "Copy raw" }}
              </button>
              <button
                type="button"
                :disabled="!auth.canOperate || actionBusy"
                title="Copy memory with portable Markdown metadata"
                @click="copyContent('markdown')"
              >
                <Check v-if="copied === 'markdown'" :size="15" aria-hidden="true" />
                <Copy v-else :size="15" aria-hidden="true" />
                {{ copied === "markdown" ? "Copied" : "Copy Markdown" }}
              </button>
            </div>
          </header>

          <div v-if="detail.content_redacted" class="sensitive-warning">
            <ShieldAlert :size="16" aria-hidden="true" />
            <span>Sensitive values are hidden. Operator access is required to reveal or export the original.</span>
          </div>
          <div v-else-if="detail.sensitivity === 'sensitive'" class="sensitive-warning">
            <ShieldAlert :size="16" aria-hidden="true" />
            <span>Original sensitive content is visible. Check it before moving it to another machine.</span>
          </div>

          <dl class="detail-meta">
            <div>
              <dt>Source</dt>
              <dd class="mono" :title="detail.source_path">{{ detail.source_path }}</dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>{{ absoluteTime(detail.source_updated_at) || "Unknown" }}</dd>
            </div>
            <div>
              <dt>SHA-256</dt>
              <dd class="mono">{{ detail.content_hash }}</dd>
            </div>
          </dl>

          <div class="view-tabs" role="tablist" aria-label="Memory content view">
            <button
              type="button"
              role="tab"
              :aria-selected="contentView === 'rendered'"
              :class="{ active: contentView === 'rendered' }"
              @click="contentView = 'rendered'"
            >
              Rendered
            </button>
            <button
              type="button"
              role="tab"
              :aria-selected="contentView === 'raw'"
              :class="{ active: contentView === 'raw' }"
              @click="contentView = 'raw'"
            >
              Raw
            </button>
          </div>
          <article
            v-if="contentView === 'rendered'"
            class="content-body markdown-body"
            v-html="renderMarkdown(detail.content)"
          />
          <pre v-else class="content-raw">{{ detail.content }}</pre>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import {
  Archive,
  Check,
  Copy,
  Eye,
  FileText,
  RefreshCw,
  Search,
  ShieldAlert,
} from "@lucide/vue";
import AgentBadge from "../components/AgentBadge.vue";
import {
  exportMemories,
  getMemory,
  getMemoryContent,
  getMemorySources,
  listMemories,
  type MemoryDetail,
  type MemoryEntry,
  type MemoryExportFormat,
  type MemoryExportTarget,
  type MemoryScope,
  type MemorySource,
} from "../api/memories";
import { agentLabel } from "../constants/agents";
import { renderMarkdown } from "../markdown/render";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const entries = ref<MemoryEntry[]>([]);
const sources = ref<MemorySource[]>([]);
const detail = ref<MemoryDetail | null>(null);
const selectedIds = ref(new Set<string>());
const nextCursor = ref<string | null>(null);
const query = ref("");
const agent = ref("");
const scope = ref<MemoryScope | "">("");
const includeMissing = ref(false);
const exportTarget = ref<MemoryExportTarget>("generic");
const contentView = ref<"rendered" | "raw">("rendered");
const loading = ref(true);
const detailLoading = ref(false);
const exporting = ref(false);
const actionBusy = ref(false);
const error = ref("");
const notice = ref("");
const copied = ref<"raw" | "markdown" | "">("");
let noticeTimer: ReturnType<typeof window.setTimeout> | null = null;
let copiedTimer: ReturnType<typeof window.setTimeout> | null = null;

function showNotice(message: string): void {
  notice.value = message;
  if (noticeTimer) window.clearTimeout(noticeTimer);
  noticeTimer = window.setTimeout(() => (notice.value = ""), 3500);
}

async function load(reset = true): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const response = await listMemories({
      limit: 100,
      cursor: reset ? null : nextCursor.value,
      q: query.value,
      agent: agent.value,
      scope: scope.value,
      includeMissing: includeMissing.value,
    });
    entries.value = reset ? response.entries : [...entries.value, ...response.entries];
    nextCursor.value = response.next_cursor;
    if (reset) {
      const currentStillVisible = detail.value
        ? entries.value.some((entry) => entry.id === detail.value?.id)
        : false;
      if (!currentStillVisible) detail.value = null;
      if (!detail.value && entries.value[0]) await openEntry(entries.value[0].id);
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load memories";
  } finally {
    loading.value = false;
  }
}

async function loadSources(): Promise<void> {
  try {
    sources.value = (await getMemorySources()).sources;
  } catch {
    sources.value = [];
  }
}

async function refresh(): Promise<void> {
  await Promise.all([load(true), loadSources()]);
}

async function applyFilters(): Promise<void> {
  clearSelection();
  await load(true);
}

async function openEntry(id: string): Promise<void> {
  detailLoading.value = true;
  error.value = "";
  contentView.value = "rendered";
  try {
    detail.value = await getMemory(id);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load memory";
  } finally {
    detailLoading.value = false;
  }
}

function toggleSelected(id: string): void {
  const next = new Set(selectedIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selectedIds.value = next;
}

function clearSelection(): void {
  selectedIds.value = new Set();
}

async function revealContent(): Promise<void> {
  if (!detail.value) return;
  actionBusy.value = true;
  try {
    const result = await getMemoryContent(detail.value.id, "raw", "reveal");
    detail.value = {
      ...detail.value,
      content: result.content,
      content_redacted: false,
    };
    showNotice("Complete memory revealed.");
  } catch (cause) {
    showNotice(cause instanceof Error ? cause.message : "Unable to reveal memory");
  } finally {
    actionBusy.value = false;
  }
}

async function copyContent(format: "raw" | "markdown"): Promise<void> {
  if (!detail.value) return;
  actionBusy.value = true;
  try {
    const result = await getMemoryContent(detail.value.id, format, "copy");
    await navigator.clipboard.writeText(result.content);
    copied.value = format;
    if (copiedTimer) window.clearTimeout(copiedTimer);
    copiedTimer = window.setTimeout(() => (copied.value = ""), 1600);
  } catch (cause) {
    showNotice(cause instanceof Error ? cause.message : "Unable to copy memory");
  } finally {
    actionBusy.value = false;
  }
}

async function downloadSelected(format: MemoryExportFormat): Promise<void> {
  if (!selectedIds.value.size) return;
  exporting.value = true;
  try {
    const result = await exportMemories(
      [...selectedIds.value],
      format,
      exportTarget.value,
    );
    const bytes =
      result.encoding === "base64"
        ? Uint8Array.from(window.atob(result.content), (character) =>
            character.charCodeAt(0),
          )
        : result.content;
    const url = URL.createObjectURL(new Blob([bytes], { type: result.media_type }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = result.filename;
    anchor.click();
    URL.revokeObjectURL(url);
    showNotice(`Downloaded ${selectedIds.value.size} memories.`);
  } catch (cause) {
    showNotice(cause instanceof Error ? cause.message : "Unable to export memories");
  } finally {
    exporting.value = false;
  }
}

function sourceStatus(source: MemorySource): string {
  if (source.status === "unsupported") return "not detected";
  if (source.status === "missing") return "source missing";
  return source.status;
}

function scopeLabel(value: MemoryScope): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function relativeTime(value: string | null): string {
  if (!value) return "unknown";
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return "unknown";
  const delta = Math.max(0, Date.now() - timestamp);
  if (delta < 60_000) return "just now";
  if (delta < 3_600_000) return `${Math.round(delta / 60_000)}m ago`;
  if (delta < 86_400_000) return `${Math.round(delta / 3_600_000)}h ago`;
  if (delta < 30 * 86_400_000) return `${Math.round(delta / 86_400_000)}d ago`;
  return new Date(value).toLocaleDateString();
}

function absoluteTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

onMounted(() => {
  void refresh();
});
</script>

<style scoped>
.memories-page { width: min(1420px, 100%); margin: 0 auto; padding: 26px 22px 42px; }
.page-header { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.eyebrow { margin: 0 0 4px; color: var(--fg-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
h1 { margin: 0; font-size: 25px; letter-spacing: 0; }
.filters { display: flex; align-items: center; justify-content: flex-end; gap: 7px; flex-wrap: wrap; }
.filters select, .bulk-bar select { height: 34px; padding: 0 28px 0 9px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); }
.search-box { display: flex; align-items: center; gap: 7px; width: 246px; height: 34px; padding: 0 9px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg-muted); }
.search-box input { min-width: 0; flex: 1; border: 0; outline: 0; background: transparent; color: var(--fg); }
.missing-toggle { display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 7px; color: var(--fg-muted); font-size: 12px; white-space: nowrap; }
.missing-toggle input, .row-select input { accent-color: var(--accent); }
.icon-action, .bulk-bar button, .detail-actions button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 34px; padding: 0 10px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); white-space: nowrap; cursor: pointer; }
.icon-action:active, .bulk-bar button:active, .detail-actions button:active { transform: translateY(1px); }
button:disabled { opacity: .48; cursor: not-allowed; }
.source-strip { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); margin-bottom: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); overflow: hidden; }
.source-item { display: grid; grid-template-columns: 8px auto minmax(0, 1fr) auto; align-items: center; gap: 7px; min-width: 0; padding: 9px 11px; border-right: 1px solid var(--border); color: var(--fg-muted); font-size: 11px; }
.source-item:last-child { border-right: 0; }
.source-item strong { color: var(--fg); font-size: 12px; }
.source-item span:not(.source-dot) { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-item b { color: var(--fg); font-variant-numeric: tabular-nums; }
.source-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--fg-subtle); }
.source-item[data-status="indexed"] .source-dot { background: var(--ok); }
.source-item[data-status="degraded"] .source-dot, .source-item[data-status="missing"] .source-dot { background: var(--warn); }
.bulk-bar { display: flex; align-items: center; gap: 8px; min-height: 42px; margin-bottom: 12px; padding: 4px 8px 4px 12px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent-weak); }
.bulk-bar label { display: flex; align-items: center; gap: 6px; margin-left: auto; color: var(--fg-muted); font-size: 11px; }
.bulk-bar .clear-selection { border-color: transparent; background: transparent; color: var(--fg-muted); }
.permission-note { color: var(--warn); font-size: 11px; }
.notice { min-height: 24px; margin: 0; color: var(--ok); font-size: 12px; }
.memory-shell { display: grid; grid-template-columns: minmax(300px, 390px) minmax(0, 1fr); min-height: 640px; max-height: calc(100dvh - 230px); border: 1px solid var(--border); border-radius: 7px; background: var(--panel); overflow: hidden; }
.memory-list { min-width: 0; overflow-y: auto; border-right: 1px solid var(--border); background: var(--panel); }
.memory-row { display: grid; grid-template-columns: 34px minmax(0, 1fr); min-width: 0; border-bottom: 1px solid var(--border); }
.memory-row--active { background: #1a222b; box-shadow: inset 2px 0 var(--accent); }
.row-select { display: grid; place-items: start center; padding-top: 17px; cursor: pointer; }
.row-open { min-width: 0; padding: 13px 13px 13px 0; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
.row-title { display: flex; align-items: center; gap: 7px; min-width: 0; }
.row-title strong { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.sensitive-icon { flex: 0 0 auto; color: var(--warn); }
.row-summary { display: -webkit-box; overflow: hidden; margin-top: 5px; color: var(--fg-muted); font-size: 12px; line-height: 1.45; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.row-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 9px; margin-top: 8px; color: var(--fg-subtle); font-size: 10px; }
.missing-label { color: var(--warn); }
.memory-detail { min-width: 0; overflow-y: auto; background: var(--panel-2); }
.detail-header { position: sticky; top: 0; z-index: 2; display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding: 18px 20px 14px; border-bottom: 1px solid var(--border); background: rgba(27, 31, 28, .97); }
.detail-heading { min-width: 0; display: flex; flex-direction: column; gap: 9px; }
.detail-heading h2 { margin: 0; overflow-wrap: anywhere; font-size: 18px; letter-spacing: 0; }
.detail-heading p { max-width: 80ch; margin: 3px 0 0; overflow: hidden; color: var(--fg-muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.detail-badges { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.badge { padding: 1px 7px; border: 1px solid var(--border); border-radius: 999px; color: var(--fg-muted); font-size: 10px; }
.badge--warn { border-color: transparent; background: var(--warn-weak); color: var(--warn); }
.detail-actions { display: flex; flex: 0 0 auto; align-items: center; gap: 6px; }
.sensitive-warning { display: flex; align-items: center; gap: 8px; margin: 14px 20px 0; padding: 9px 11px; border: 1px solid rgba(231,183,95,.28); border-radius: 5px; background: var(--warn-weak); color: var(--warn); font-size: 12px; }
.detail-meta { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(150px, .7fr) minmax(200px, 1fr); gap: 1px; margin: 14px 20px 0; border: 1px solid var(--border); background: var(--border); }
.detail-meta div { min-width: 0; padding: 8px 10px; background: var(--panel); }
.detail-meta dt { margin-bottom: 3px; color: var(--fg-subtle); font-size: 10px; text-transform: uppercase; }
.detail-meta dd { margin: 0; overflow: hidden; color: var(--fg-muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.view-tabs { display: flex; align-items: center; gap: 2px; margin: 16px 20px 0; border-bottom: 1px solid var(--border); }
.view-tabs button { padding: 7px 10px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--fg-muted); cursor: pointer; }
.view-tabs button.active { border-bottom-color: var(--accent); color: var(--fg); }
.content-body, .content-raw { margin: 0; padding: 18px 20px 48px; overflow-wrap: anywhere; }
.content-raw { min-height: 320px; overflow: auto; color: var(--fg); font: 12px/1.65 var(--mono); white-space: pre-wrap; }
.detail-state, .state { display: grid; min-height: 280px; place-items: center; padding: 30px; color: var(--fg-muted); text-align: center; }
.state { border: 1px dashed var(--border); border-radius: 7px; }
.state--error { color: var(--danger); }
.load-more { width: 100%; height: 40px; border: 0; border-top: 1px solid var(--border); background: var(--panel); color: var(--accent); cursor: pointer; }
.memory-shell--loading { min-height: 540px; }
.skeleton-list, .skeleton-detail { background: linear-gradient(90deg, var(--panel) 20%, var(--panel-2) 50%, var(--panel) 80%); background-size: 220% 100%; animation: shimmer 1.4s infinite linear; }
.skeleton-list { border-right: 1px solid var(--border); }
.skeleton-detail { background-color: var(--panel-2); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
.mono { font-family: var(--mono); }
@keyframes shimmer { to { background-position: -220% 0; } }
@media (prefers-reduced-motion: reduce) { .skeleton-list, .skeleton-detail { animation: none; } }
@media (max-width: 1050px) {
  .page-header { align-items: stretch; flex-direction: column; }
  .filters { justify-content: flex-start; }
  .memory-shell { grid-template-columns: minmax(280px, 340px) minmax(0, 1fr); }
  .detail-header { flex-direction: column; }
}
@media (max-width: 760px) {
  .memories-page { padding: 18px 12px 86px; }
  .filters { display: grid; grid-template-columns: 1fr 1fr; }
  .filters label:not(.missing-toggle) select, .search-box { width: 100%; }
  .search-box { grid-column: 1 / -1; }
  .source-strip { grid-template-columns: 1fr; }
  .source-item { border-right: 0; border-bottom: 1px solid var(--border); }
  .source-item:last-child { border-bottom: 0; }
  .bulk-bar { align-items: stretch; flex-direction: column; padding: 10px; }
  .bulk-bar label { width: 100%; margin-left: 0; justify-content: space-between; }
  .bulk-bar select { min-width: 0; flex: 1; }
  .memory-shell { display: block; max-height: none; }
  .memory-list { max-height: 420px; border-right: 0; border-bottom: 1px solid var(--border); }
  .memory-detail { min-height: 500px; }
  .detail-actions { width: 100%; overflow-x: auto; }
  .detail-meta { grid-template-columns: 1fr; }
}
</style>
