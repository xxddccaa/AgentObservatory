<template>
  <div class="history-detail">
    <header class="hd-header">
      <router-link class="back-link" :to="{ path: '/history', query: route.query }">
        <ArrowLeft :size="16" /> History
      </router-link>
      <div class="hd-title">
        <h1 class="mono">{{ meta?.thread_id?.slice(0, 20) || sessionId.slice(0, 12) }}</h1>
        <AgentBadge v-if="meta?.agent" :agent="meta.agent" />
        <span class="badge" data-tone="muted">read-only</span>
      </div>
      <div class="hd-meta">
        <span>{{ meta?.model || "model?" }}</span>
        <span>{{ meta?.turn_count ?? 0 }} turns</span>
        <span>{{ (meta?.total_tokens ?? 0).toLocaleString() }} tokens</span>
      </div>
    </header>

    <p v-if="meta?.cwd" class="hd-cwd mono">{{ meta.cwd }}</p>

    <section v-if="stepGoRuns.length" class="hd-stepgo" aria-labelledby="hd-stepgo-title">
      <div class="hd-stepgo-head">
        <div>
          <h2 id="hd-stepgo-title">StepGo launches</h2>
          <p>Host evidence is linked to this native transcript; tokens are counted once.</p>
        </div>
        <span class="badge launch-badge">{{ stepGoRuns.length }} host {{ stepGoRuns.length === 1 ? "run" : "runs" }}</span>
      </div>
      <article v-for="run in stepGoRuns" :key="run.id" class="hd-stepgo-run">
        <AgentBadge :agent="run.agent" />
        <span class="badge" :data-tone="stepGoStatusTone(run.status)">{{ run.status }}</span>
        <span class="badge" :data-tone="stepGoLinkTone(run.link_status)">{{ run.link_status }}</span>
        <span v-if="run.exit_code !== null" class="mono muted">exit {{ run.exit_code }}</span>
        <span class="mono muted" :title="run.host_run_id">host: {{ run.host_run_id }}</span>
        <span class="muted">{{ absoluteTime(run.start_time) }}</span>
        <span v-if="run.last_error" class="hd-stepgo-error">{{ run.last_error }}</span>
      </article>
    </section>

    <div class="hd-controls">
      <div class="search">
        <Search :size="15" class="search-icon" />
        <input
          v-model="query"
          class="search-input"
          type="search"
          placeholder="Search this session…"
          aria-label="Search this session"
        />
      </div>
      <div class="export" role="group" aria-label="Export whole session">
        <span class="export-label">Export</span>
        <button class="export-btn" :disabled="exporting === 'text'" @click="exportAndCopy('text')">
          <Copy :size="13" /> Copy text
        </button>
        <button class="export-btn" :disabled="exporting === 'markdown'" @click="exportAndCopy('markdown')">
          <Copy :size="13" /> Copy Markdown
        </button>
        <button class="export-btn" :disabled="exporting === 'json'" @click="exportAndCopy('json')">
          <Copy :size="13" /> Copy JSON
        </button>
      </div>
    </div>
    <p v-if="exportNote" class="hd-note" role="status">{{ exportNote }}</p>

    <div v-if="query.trim()" class="search-results">
      <div class="search-status">
        <span v-if="searching">Searching…</span>
        <template v-else>
          <strong>{{ matches.length }}</strong>
          <span>match{{ matches.length === 1 ? "" : "es" }}</span>
          <span v-if="searchNote" class="muted">· {{ searchNote }}</span>
        </template>
      </div>
      <ul v-if="matches.length" class="match-list">
        <li v-for="(m, i) in matches" :key="`${m.kind}-${m.id}-${i}`">
          <button class="match" @click="gotoMatch(m)">
            <span class="badge" data-tone="muted">{{ m.kind }}</span>
            <span class="match-ref mono">{{ m.ref }}</span>
            <span class="match-snippet">{{ m.snippet }}</span>
          </button>
        </li>
      </ul>
      <div v-else-if="!searching" class="search-empty">
        No matches{{ searchError ? ` — ${searchError}` : "" }}
      </div>
    </div>

    <nav class="tabs" aria-label="History views">
      <button
        v-for="t in tabs"
        :key="t.key"
        class="tab"
        :class="{ 'tab--active': tab === t.key }"
        @click="select(t.key)"
      >
        <component :is="t.icon" :size="16" /> {{ t.label }}
      </button>
    </nav>

    <section class="tab-body">
      <div v-if="loading" class="empty">Loading…</div>
      <div v-else-if="error" class="empty error">{{ error }}</div>

      <template v-else-if="tab === 'conversation'">
        <div v-if="messages.length" class="message-toolbar">
          <div class="message-filter" role="group" aria-label="Filter conversation messages">
            <span class="message-filter__label">Show</span>
            <button
              v-for="option in messageFilterOptions"
              :key="option.value"
              type="button"
              class="message-filter__option"
              :class="{ 'message-filter__option--active': messageFilter === option.value }"
              :aria-pressed="messageFilter === option.value"
              @click="setMessageFilter(option.value)"
            >
              {{ option.label }}
            </button>
          </div>
          <span class="message-count">{{ visibleMessages.length }} / {{ messages.length }} messages</span>
        </div>
        <article
          v-for="m in visibleMessages"
          :id="`msg-${m.id}`"
          :key="m.id"
          class="message"
          :class="{ 'message--reasoning': m.channel === 'reasoning' }"
          :data-role="m.role"
        >
          <header>
            <strong class="role" :data-role="m.role">{{ m.role }}</strong>
            <span v-if="m.channel === 'reasoning'" class="badge" data-tone="muted">reasoning summary</span>
            <button class="copy" @click="copy(m.copy_text)">Copy</button>
          </header>
          <div
            v-if="supportsMarkdown(m)"
            class="message-body"
            :class="[historyMarkdownStyles['markdown-body'], { 'reasoning-text': m.channel === 'reasoning' }]"
            v-html="renderMessage(m)"
          ></div>
          <pre v-else :class="{ 'reasoning-text': m.channel === 'reasoning' }" v-html="highlightPlainText(m.text)"></pre>
        </article>
        <div v-if="messages.length === 0" class="empty">No structured messages indexed.</div>
        <div v-else-if="visibleMessages.length === 0" class="empty">No messages match this filter.</div>
      </template>

      <template v-else-if="tab === 'activity'">
        <SessionWaterfall
          :spans="activitySpans"
          :error="activityError"
          @evidence-select="loadEvidence"
        />
        <section
          v-if="selectedEvidence"
          class="evidence-panel"
          aria-labelledby="evidence-title"
        >
          <header>
            <div>
              <h2 id="evidence-title">{{ selectedEvidence.category }}</h2>
              <span class="muted mono">
                {{ selectedEvidence.recorded_at ? absoluteTime(selectedEvidence.recorded_at) : "No timestamp" }}
              </span>
            </div>
            <div class="evidence-actions">
              <button class="copy" @click="copy(evidenceText)">Copy full evidence</button>
              <button class="copy" @click="selectedEvidence = null">Close</button>
            </div>
          </header>
          <p v-if="selectedEvidence.summary" class="evidence-summary">
            {{ selectedEvidence.summary }}
          </p>
          <pre>{{ evidenceText }}</pre>
        </section>
      </template>

      <template v-else-if="tab === 'timeline'">
        <div class="timeline-controls">
          <label class="toggle">
            <input v-model="onlyUnknown" type="checkbox" />
            Show only unknown
          </label>
          <span class="muted">{{ visibleEvents.length }} / {{ events.length }} events</span>
        </div>
        <article v-for="e in visibleEvents" :key="e.event_index" class="event">
          <div class="event-head">
            <span class="event-index mono">#{{ e.event_index }}</span>
            <strong>{{ e.normalized_type }}</strong>
            <span v-if="e.payload_type && e.payload_type !== e.normalized_type" class="muted mono event-payload">
              {{ e.payload_type }}
            </span>
            <span v-if="!e.is_known" class="badge" data-tone="warn">unknown</span>
            <span v-if="e.event_timestamp" class="muted event-time">{{ absoluteTime(e.event_timestamp) }}</span>
            <button class="copy" @click="copy(pretty(e.raw_json))">Copy JSON</button>
          </div>
          <details class="event-json">
            <summary>raw event</summary>
            <pre>{{ pretty(e.raw_json) }}</pre>
          </details>
        </article>
        <div v-if="events.length === 0" class="empty">No timeline events indexed.</div>
        <div v-else-if="visibleEvents.length === 0" class="empty">No unknown events — all recognized.</div>
      </template>

      <template v-else-if="tab === 'tools'">
        <article v-for="t in tools" :id="`tool-${t.id}`" :key="t.id" class="tool">
          <header>
            <strong>{{ t.tool_name }}</strong>
            <span v-if="t.is_mutating" class="badge" data-tone="warn">mutating</span>
            <span class="badge" :data-tone="t.status === 'completed' ? 'ok' : t.status === 'failed' ? 'danger' : 'muted'">
              {{ t.status }}
            </span>
            <span v-if="t.exit_code !== null && t.exit_code !== undefined" class="muted mono">exit {{ t.exit_code }}</span>
            <button class="copy" @click="copy(t.command_text || t.output_text || '')">Copy</button>
          </header>
          <pre v-if="t.command_text" class="tool-cmd">$ {{ t.command_text }}</pre>
          <pre v-if="t.output_text" class="tool-out">{{ t.output_text }}</pre>
        </article>
        <div v-if="tools.length === 0" class="empty">No tool calls indexed.</div>
      </template>

      <template v-else-if="tab === 'diff'">
        <article v-for="t in diffs" :key="t.id" class="tool">
          <header>
            <strong>{{ t.tool_name }}</strong>
            <button class="copy" @click="copy(t.command_text || t.output_text || '')">Copy patch</button>
          </header>
          <pre class="diff">{{ t.command_text || t.output_text || "(no patch body)" }}</pre>
        </article>
        <div v-if="diffs.length === 0" class="empty">No file-mutating tool calls (patches) indexed.</div>
      </template>
    </section>
    <div class="scroll-controls" aria-label="Page navigation">
      <button type="button" class="scroll-control" aria-label="Scroll to top" title="Scroll to top" @click="scrollToBoundary('top')">
        <ArrowUp :size="17" aria-hidden="true" />
      </button>
      <button type="button" class="scroll-control" aria-label="Scroll to bottom" title="Scroll to bottom" @click="scrollToBoundary('bottom')">
        <ArrowDown :size="17" aria-hidden="true" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import {
  Activity,
  ArrowLeft,
  ArrowDown,
  ArrowUp,
  Copy,
  FileDiff,
  ListTree,
  MessagesSquare,
  Search,
  Wrench,
} from "@lucide/vue";
import {
  getHistory,
  getMessages,
  getEvents,
  getTools,
  exportSession,
  searchSession,
  type ExportFormat,
  type HistoryDetail,
  type HistoryMessage,
  type HistoryEvent,
  type HistoryTool,
  type SearchMatch,
  type StepGoRun,
} from "../api/history";
import {
  getInsightEvidence,
  getSessionTimeline,
  type ActivitySpan,
  type InsightEvidence,
} from "../api/insights";
import SessionWaterfall from "../components/SessionWaterfall.vue";
import AgentBadge from "../components/AgentBadge.vue";
import historyMarkdownStyles from "../markdown/history.module.css";
import { renderMarkdown } from "../markdown/render";
import {
  createSessionMarkdownRenderer,
  type SessionMarkdownRenderer,
} from "../markdown/session";

const props = defineProps<{ id: string }>();
const route = useRoute();
const sessionId = computed(() => props.id);

type TabKey = "conversation" | "activity" | "timeline" | "tools" | "diff";
const tabs: { key: TabKey; label: string; icon: unknown }[] = [
  { key: "conversation", label: "Conversation", icon: MessagesSquare },
  { key: "activity", label: "Activity", icon: Activity },
  { key: "timeline", label: "Timeline", icon: ListTree },
  { key: "tools", label: "Tools", icon: Wrench },
  { key: "diff", label: "Diff", icon: FileDiff },
];

const tab = ref<TabKey>("conversation");
const loading = ref(false);
const error = ref("");
const meta = ref<HistoryDetail | null>(null);
const stepGoRuns = computed<StepGoRun[]>(() => meta.value?.stepgo_runs ?? []);
const messages = ref<HistoryMessage[]>([]);
const events = ref<HistoryEvent[]>([]);
const tools = ref<HistoryTool[]>([]);
const activitySpans = ref<ActivitySpan[]>([]);
const activityError = ref("");
const selectedEvidence = ref<InsightEvidence | null>(null);
const loaded = new Set<TabKey>();
const sessionRenderer = shallowRef<SessionMarkdownRenderer | null>(null);
let rendererRequest = 0;
let unmounted = false;

const evidenceText = computed(() => {
  const evidence = selectedEvidence.value;
  if (!evidence) return "";
  if (evidence.raw_text) return evidence.raw_text;
  return pretty(evidence.raw_json ?? evidence.metadata);
});

const onlyUnknown = ref(false);
const visibleEvents = computed(() => (onlyUnknown.value ? events.value.filter((e) => !e.is_known) : events.value));

type MessageFilter = "focused" | "all" | "user" | "assistant" | "reasoning";
const messageFilterOptions: { value: MessageFilter; label: string }[] = [
  { value: "focused", label: "Focused" },
  { value: "all", label: "All" },
  { value: "user", label: "User" },
  { value: "assistant", label: "Assistant" },
  { value: "reasoning", label: "Reasoning summary" },
];
const messageFilter = ref<MessageFilter>("focused");
const visibleMessages = computed(() => {
  if (messageFilter.value === "all") return messages.value;
  if (messageFilter.value === "user") return messages.value.filter((message) => message.role === "user");
  if (messageFilter.value === "assistant") {
    return messages.value.filter((message) => message.role === "assistant" && message.channel !== "reasoning");
  }
  if (messageFilter.value === "reasoning") return messages.value.filter((message) => message.channel === "reasoning");
  return messages.value.filter(
    (message) =>
      message.channel !== "reasoning" &&
      message.role !== "system" &&
      message.role !== "developer"
  );
});

function setMessageFilter(filter: MessageFilter) {
  messageFilter.value = filter;
}

function stepGoStatusTone(status: string): "ok" | "warn" | "muted" | "danger" {
  if (status === "completed") return "ok";
  if (status === "failed") return "danger";
  if (status === "running") return "ok";
  return "warn";
}

function stepGoLinkTone(status: string): "ok" | "warn" | "muted" | "danger" {
  if (status === "linked") return "ok";
  if (status === "ambiguous") return "warn";
  return "muted";
}

function scrollToBoundary(boundary: "top" | "bottom") {
  const scrollingElement = document.scrollingElement ?? document.documentElement;
  window.scrollTo({
    top: boundary === "top" ? 0 : scrollingElement.scrollHeight,
    behavior: "smooth",
  });
}

const MUTATING = new Set(["apply_patch", "exec_command"]);
const diffs = computed(() =>
  tools.value.filter((t) => t.is_mutating || MUTATING.has(t.tool_name) || MUTATING.has(t.tool_kind))
);

// --- Whole-session export -------------------------------------------------
const exporting = ref<ExportFormat | "">("");
const exportNote = ref("");
let noteTimer: ReturnType<typeof setTimeout> | undefined;

const FORMAT_LABEL: Record<ExportFormat, string> = { text: "text", markdown: "Markdown", json: "JSON" };

async function exportAndCopy(format: ExportFormat) {
  exporting.value = format;
  try {
    const result = await exportSession(sessionId.value, format);
    if (await copy(result.content)) {
      flashNote(`Copied ${FORMAT_LABEL[format]} to clipboard`);
    } else {
      download(result.filename, result.content);
      flashNote(`Downloaded ${result.filename}`);
    }
  } catch (cause) {
    flashNote(cause instanceof Error ? cause.message : "Export failed");
  } finally {
    exporting.value = "";
  }
}

function flashNote(text: string) {
  exportNote.value = text;
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => (exportNote.value = ""), 4000);
}

function download(filename: string, content: string) {
  try {
    const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename || "agent-session.txt";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  } catch {
    /* download unavailable */
  }
}

// --- In-session search ----------------------------------------------------
const query = ref("");
const searching = ref(false);
const searchError = ref("");
const searchNote = ref("");
const matches = ref<SearchMatch[]>([]);
const highlightTerm = ref("");
let searchTimer: ReturnType<typeof setTimeout> | undefined;

watch(query, (value) => {
  clearTimeout(searchTimer);
  const term = value.trim();
  if (!term) {
    matches.value = [];
    searching.value = false;
    searchError.value = "";
    searchNote.value = "";
    highlightTerm.value = "";
    return;
  }
  searching.value = true;
  searchTimer = setTimeout(() => runSearch(term), 250);
});

async function runSearch(term: string) {
  searching.value = true;
  searchError.value = "";
  searchNote.value = "";
  try {
    matches.value = (await searchSession(sessionId.value, term)).matches;
    highlightTerm.value = term;
  } catch (cause) {
    // Graceful degradation: filter what we've already loaded client-side so
    // the box stays useful even before the server search endpoint is live.
    const local = localMatches(term);
    matches.value = local;
    highlightTerm.value = term;
    if (local.length) searchNote.value = "server search unavailable — showing local results";
    else searchError.value = cause instanceof Error ? cause.message : "Search failed";
  } finally {
    searching.value = false;
  }
}

function localMatches(term: string): SearchMatch[] {
  const needle = term.toLowerCase();
  const out: SearchMatch[] = [];
  for (const m of messages.value) {
    if (m.text.toLowerCase().includes(needle)) {
      out.push({ kind: "message", id: m.id, ref: m.role, snippet: snippet(m.text, needle) });
    }
  }
  for (const t of tools.value) {
    const hay = `${t.command_text ?? ""} ${t.output_text ?? ""}`;
    if (hay.toLowerCase().includes(needle)) {
      out.push({ kind: "tool", id: t.id, ref: t.tool_name, snippet: snippet(hay, needle) });
    }
  }
  return out;
}

function snippet(text: string, needle: string): string {
  const at = text.toLowerCase().indexOf(needle);
  if (at < 0) return text.slice(0, 120);
  const start = Math.max(0, at - 40);
  return (start > 0 ? "…" : "") + text.slice(start, start + 120).replace(/\s+/g, " ").trim() + "…";
}

async function gotoMatch(match: SearchMatch) {
  const target: TabKey = match.kind === "tool" ? "tools" : "conversation";
  if (tab.value !== target) await select(target);
  highlightTerm.value = query.value.trim();
  await nextTick();
  const el = document.getElementById(`${match.kind === "tool" ? "tool" : "msg"}-${match.id}`);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1600);
  }
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function highlightPlainText(text: string): string {
  const escaped = escapeHtml(text);
  const term = highlightTerm.value.trim();
  if (!term) return escaped;
  const pattern = escapeHtml(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (!pattern) return escaped;
  return escaped.replace(new RegExp(pattern, "gi"), (hit) => `<mark>${hit}</mark>`);
}

function supportsMarkdown(message: HistoryMessage): boolean {
  return message.role === "user" || message.role === "assistant";
}

function renderMessage(message: HistoryMessage): string {
  const source = message.channel === "reasoning"
    ? message.text
    : message.markdown_text || message.text;
  return sessionRenderer.value?.render(source, highlightTerm.value)
    ?? renderMarkdown(source, highlightTerm.value);
}

function loadSessionRenderer(): void {
  const request = ++rendererRequest;
  void createSessionMarkdownRenderer()
    .then((renderer) => {
      if (unmounted || request !== rendererRequest) {
        renderer.clearCache();
        return;
      }
      sessionRenderer.value = renderer;
    })
    .catch(() => {
      // The synchronous renderer remains available as the safe fallback.
    });
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

function onBoundaryKey(event: KeyboardEvent) {
  if (
    event.defaultPrevented
    || event.altKey
    || event.ctrlKey
    || event.metaKey
    || event.shiftKey
    || isEditableTarget(event.target)
    || (event.key !== "Home" && event.key !== "End")
  ) {
    return;
  }

  event.preventDefault();
  const scrollingElement = document.scrollingElement ?? document.documentElement;
  window.scrollTo({
    top: event.key === "Home" ? 0 : scrollingElement.scrollHeight,
    behavior: "auto",
  });
}

// --- Formatting -----------------------------------------------------------
function pretty(value: unknown): string {
  try {
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function absoluteTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

async function copy(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}

// --- Loading --------------------------------------------------------------
async function select(next: TabKey) {
  tab.value = next;
  await loadTab(next);
}

async function loadTab(which: TabKey) {
  const key: TabKey = which === "diff" ? "tools" : which;
  if (loaded.has(key)) return;
  loading.value = true;
  error.value = "";
  try {
    if (key === "conversation") messages.value = (await getMessages(sessionId.value)).messages;
    else if (key === "activity") {
      activitySpans.value = (await getSessionTimeline(sessionId.value)).spans;
    }
    else if (key === "timeline") events.value = (await getEvents(sessionId.value)).events;
    else if (key === "tools") tools.value = (await getTools(sessionId.value)).tools;
    loaded.add(key);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Failed to load session data";
  } finally {
    loading.value = false;
  }
}

async function loadEvidence(evidenceId: string) {
  activityError.value = "";
  try {
    selectedEvidence.value = await getInsightEvidence(evidenceId);
  } catch (cause) {
    activityError.value = cause instanceof Error ? cause.message : "Failed to load evidence";
  }
}

onMounted(async () => {
  window.addEventListener("keydown", onBoundaryKey);
  loadSessionRenderer();
  await loadMeta();
  await loadTab("conversation");
});

onBeforeUnmount(() => {
  unmounted = true;
  rendererRequest += 1;
  sessionRenderer.value?.clearCache();
  sessionRenderer.value = null;
  window.removeEventListener("keydown", onBoundaryKey);
});

async function loadMeta() {
  try {
    meta.value = await getHistory(sessionId.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Session not found";
  }
}

// Reload when navigating between two history sessions (component is reused).
watch(sessionId, async () => {
  sessionRenderer.value?.clearCache();
  meta.value = null;
  messages.value = [];
  events.value = [];
  tools.value = [];
  activitySpans.value = [];
  selectedEvidence.value = null;
  activityError.value = "";
  loaded.clear();
  tab.value = "conversation";
  query.value = "";
  matches.value = [];
  highlightTerm.value = "";
  onlyUnknown.value = false;
  messageFilter.value = "focused";
  await loadMeta();
  await loadTab("conversation");
});
</script>

<style scoped>
.history-detail {
  max-width: 1000px;
  margin: 0 auto;
  padding: 24px 22px 48px;
}
.hd-header {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.back-link {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--fg-muted);
  text-decoration: none;
}
.hd-title {
  display: flex;
  align-items: center;
  gap: 10px;
}
.hd-title h1 {
  margin: 0;
  font-size: 18px;
}
.hd-meta {
  display: flex;
  gap: 14px;
  margin-left: auto;
  color: var(--fg-muted);
  font-size: 12px;
}
.hd-cwd {
  margin: 8px 0 0;
  color: var(--fg-muted);
  font-size: 12px;
}
.hd-stepgo {
  margin-top: 14px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel);
}
.hd-stepgo-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 11px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-2);
}
.hd-stepgo-head h2 { margin: 0; font-size: 14px; }
.hd-stepgo-head p { margin: 3px 0 0; color: var(--fg-muted); font-size: 12px; }
.hd-stepgo-run { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 9px 14px; border-bottom: 1px solid var(--border); }
.hd-stepgo-run:last-child { border-bottom: 0; }
.hd-stepgo-error { width: 100%; color: var(--danger); font-size: 12px; }
.launch-badge { border-color: color-mix(in srgb, #f08bb0 48%, var(--border)); background: color-mix(in srgb, #f08bb0 12%, transparent); color: #f5a4c2; }
.hd-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 16px 0 0;
}
.search {
  position: relative;
  flex: 1 1 260px;
  min-width: 200px;
}
.search-icon {
  position: absolute;
  left: 10px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--fg-muted);
  pointer-events: none;
}
.search-input {
  width: 100%;
  height: 34px;
  padding: 0 12px 0 32px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel);
  color: var(--fg);
  font-size: 13px;
}
.search-input:focus {
  outline: none;
  border-color: var(--accent);
}
.export {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.export-label {
  color: var(--fg-muted);
  font-size: 12px;
  margin-right: 2px;
}
.export-btn {
  display: flex;
  align-items: center;
  gap: 5px;
  height: 30px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--panel);
  color: var(--fg);
  cursor: pointer;
  font-size: 12px;
}
.export-btn:hover:not(:disabled) {
  border-color: var(--accent);
}
.export-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.hd-note {
  margin: 8px 0 0;
  color: var(--ok);
  font-size: 12px;
}
.search-results {
  margin: 12px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--panel);
}
.search-status {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--fg-muted);
  font-size: 12px;
}
.search-status strong {
  color: var(--fg);
}
.match-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: grid;
  gap: 4px;
  max-height: 240px;
  overflow: auto;
}
.match {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 7px 9px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: var(--panel-2);
  color: var(--fg);
  cursor: pointer;
  text-align: left;
  font-size: 12px;
}
.match:hover {
  border-color: var(--accent);
}
.match-ref {
  color: var(--accent);
  flex-shrink: 0;
}
.match-snippet {
  color: var(--fg-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.search-empty {
  margin-top: 8px;
  color: var(--fg-muted);
  font-size: 12px;
}
.tabs {
  display: flex;
  gap: 4px;
  max-width: 100%;
  margin: 18px 0 14px;
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
  overflow-y: hidden;
}
.tab {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 13px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--fg-muted);
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
}
.tab--active {
  color: var(--fg);
  border-bottom-color: var(--accent);
}
.tab-body {
  min-height: 200px;
}
.message-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 0 0 12px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel-2);
}
.message-filter {
  display: flex;
  align-items: center;
  gap: 3px;
  min-width: 0;
  overflow-x: auto;
}
.message-filter__label {
  margin-right: 5px;
  color: var(--fg-muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .05em;
}
.message-filter__option {
  flex: 0 0 auto;
  min-height: 28px;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  color: var(--fg-muted);
  cursor: pointer;
  font-size: 12px;
  white-space: nowrap;
}
.message-filter__option:hover {
  color: var(--fg);
  border-color: var(--border-strong);
}
.message-filter__option--active {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
  background: var(--accent-weak);
  color: var(--fg);
}
.message-count {
  flex: 0 0 auto;
  color: var(--fg-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.scroll-controls {
  position: fixed;
  right: 22px;
  bottom: 22px;
  z-index: 20;
  display: grid;
  gap: 6px;
}
.scroll-control {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: color-mix(in srgb, var(--panel) 92%, transparent);
  color: var(--fg-muted);
  box-shadow: 0 6px 18px rgba(0, 0, 0, .28);
  cursor: pointer;
}
.scroll-control:hover {
  border-color: var(--accent);
  color: var(--fg);
}
.evidence-panel {
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}
.evidence-panel > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.evidence-panel h2 {
  margin: 0;
  font-size: 15px;
  letter-spacing: 0;
}
.evidence-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 6px;
}
.evidence-actions .copy {
  margin-left: 0;
}
.evidence-summary {
  margin: 12px 0 0;
  color: var(--fg-muted);
}
.evidence-panel pre {
  max-height: 520px;
  overflow: auto;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel);
}
.empty {
  padding: 50px 20px;
  text-align: center;
  color: var(--fg-muted);
}
.empty.error {
  color: var(--danger);
}
.message,
.tool {
  margin-bottom: 10px;
  padding: 13px 14px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--panel);
}
.message--reasoning {
  background: transparent;
  border-style: dashed;
  border-color: var(--border);
}
.message header,
.tool header,
.event-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.message header,
.tool header {
  justify-content: flex-start;
  color: var(--fg-muted);
  font-size: 12px;
}
.role {
  text-transform: capitalize;
}
.role[data-role="assistant"] {
  color: var(--accent);
}
.role[data-role="user"] {
  color: var(--ok);
}
.copy {
  margin-left: auto;
  border: 0;
  border-radius: 4px;
  padding: 4px 9px;
  background: var(--panel-2);
  color: var(--fg-muted);
  cursor: pointer;
  font-size: 12px;
}
.copy:hover {
  color: var(--fg);
}
pre {
  margin: 10px 0 0;
  padding: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font: 13px/1.6 var(--mono);
  color: var(--fg);
}
.message-body {
  margin-top: 10px;
  color: var(--fg);
  font-size: 14px;
  line-height: 1.68;
  overflow-wrap: anywhere;
}
.reasoning-text {
  color: var(--fg-muted);
  font-style: italic;
}
.tool-cmd {
  color: var(--accent);
}
.tool-out {
  color: var(--fg-muted);
  max-height: 320px;
  overflow: auto;
}
.diff {
  max-height: 480px;
  overflow: auto;
}
.timeline-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  color: var(--fg-muted);
  font-size: 12px;
}
.toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.event {
  margin-bottom: 6px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel);
  font-size: 13px;
}
.event-head {
  font-size: 13px;
}
.event-index {
  color: var(--fg-muted);
  font-size: 11px;
}
.event-payload {
  font-size: 11px;
}
.event-time {
  font-size: 11px;
}
.event-json {
  margin-top: 8px;
}
.event-json summary {
  cursor: pointer;
  color: var(--fg-muted);
  font-size: 12px;
}
.event-json pre {
  margin-top: 8px;
  max-height: 320px;
  overflow: auto;
  color: var(--fg-muted);
  font-size: 12px;
}
.muted {
  color: var(--fg-muted);
}
mark {
  background: var(--warn-weak);
  color: var(--warn);
  border-radius: 2px;
  padding: 0 1px;
}
@media (max-width: 640px) {
  .message-toolbar {
    align-items: stretch;
    flex-direction: column;
  }
  .message-count {
    align-self: flex-end;
  }
  .scroll-controls {
    right: 14px;
    bottom: 14px;
  }
}
.flash {
  animation: flash 1.5s ease-out;
}
@keyframes flash {
  from {
    box-shadow: 0 0 0 2px var(--accent);
  }
  to {
    box-shadow: 0 0 0 2px transparent;
  }
}
.badge {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--fg-muted);
}
.badge[data-tone="ok"] {
  background: var(--ok-weak);
  color: var(--ok);
  border-color: transparent;
}
.badge[data-tone="warn"] {
  background: var(--warn-weak);
  color: var(--warn);
  border-color: transparent;
}
.badge[data-tone="danger"] {
  background: var(--danger-weak);
  color: var(--danger);
  border-color: transparent;
}
</style>
