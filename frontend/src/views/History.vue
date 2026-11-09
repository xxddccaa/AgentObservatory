<template>
  <div class="list-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">Multi-agent session history</p>
        <h1>History</h1>
      </div>
      <div class="header-actions">
        <label class="agent-filter">
          <span class="sr-only">Filter sessions by agent</span>
          <select v-model="agent" aria-label="Filter sessions by agent" @change="changeAgent">
            <option v-for="option in AGENT_OPTIONS" :key="option.value || 'all'" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <div class="search-box">
          <Search :size="15" />
          <input
            v-model.trim="query"
            type="search"
            placeholder="Thread, workspace, title"
            aria-label="Search sessions"
            @keydown.enter="applyFilters"
          />
        </div>
        <label class="subagent-filter">
          <input v-model="showSubagents" type="checkbox" @change="applyFilters" />
          <span>Show subagents</span>
        </label>
        <button class="refresh" title="Refresh history" @click="applyFilters">
          <RefreshCw :size="16" />
          Refresh
        </button>
      </div>
    </header>
    <div v-if="error" class="empty error">{{ error }}</div>
    <div v-else-if="loading" class="empty">Loading sessions…</div>
    <template v-else>
      <section v-if="agent === 'stepgo'" class="stepgo-panel" aria-labelledby="stepgo-runs-title">
        <div class="stepgo-heading">
          <div>
            <h2 id="stepgo-runs-title">StepGo host runs</h2>
            <p>Launch evidence only. Native transcripts own turns and token counts.</p>
          </div>
          <div class="stepgo-counts">
            <span class="badge">{{ stepgoRuns.length }} runs</span>
            <span v-if="stepgoUnlinkedCount" class="badge" data-tone="warn">{{ stepgoUnlinkedCount }} unlinked / ambiguous</span>
          </div>
        </div>
        <div v-if="stepgoError" class="empty error">{{ stepgoError }}</div>
        <div v-else-if="stepgoRuns.length === 0" class="empty">No StepGo host runs were found.</div>
        <div v-else class="stepgo-run-list">
          <article
            v-for="run in stepgoRuns"
            :key="run.id"
            class="stepgo-run"
            :data-link-status="run.link_status"
          >
            <div class="stepgo-run-main">
              <AgentBadge :agent="run.agent" />
              <router-link
                v-if="run.linked_agent_session_id"
                class="stepgo-link"
                :to="{ path: `/history/${run.linked_agent_session_id}`, query: route.query }"
              >{{ stepgoRunSessionTitle(run) }}</router-link>
              <span v-else class="stepgo-link muted">{{ stepgoLinkLabel(run) }}</span>
              <span class="badge" :data-tone="stepgoStatusTone(run.status)">{{ run.status }}</span>
              <span class="badge" :data-tone="stepgoLinkTone(run.link_status)">{{ run.link_status }}</span>
              <span v-if="run.exit_code !== null" class="mono muted">exit {{ run.exit_code }}</span>
            </div>
            <div class="stepgo-run-meta">
              <span class="mono" :title="run.host_run_id">host: {{ shortHostRunId(run.host_run_id) }}</span>
              <span>{{ run.event_count }} events</span>
              <span>{{ relativeTime(run.start_time) }}</span>
              <span v-if="run.index_status !== 'indexed'" class="index" :data-status="run.index_status">{{ run.index_status }}</span>
            </div>
            <p v-if="run.link_reason || run.last_error" class="stepgo-note">
              {{ run.last_error || run.link_reason }}
            </p>
          </article>
        </div>
        <button v-if="stepgoNextCursor" class="load-more" :disabled="stepgoLoading" @click="loadStepGoRuns(false)">
          Load more host runs
        </button>
      </section>

      <div v-if="sessions.length === 0" class="empty">
        {{ agent === "stepgo" ? "No native sessions are linked to the listed StepGo runs." : "No indexed agent sessions match these filters." }}
      </div>
      <div v-else class="history-groups">
        <section
          v-for="group in groupedSessions"
          :key="group.path"
          class="history-group"
          :data-latest-updated="group.sessions[0] ? sessionActivityAt(group.sessions[0]) || '' : ''"
        >
          <div
            class="group-header"
            role="button"
            tabindex="0"
            :id="groupHeaderId(group.path)"
            :aria-controls="groupContentId(group.path)"
            :aria-expanded="expandedGroups.has(group.path)"
            @click="onGroupClick(group.path, $event)"
            @keydown.enter.self="toggleGroup(group.path)"
            @keydown.space.self.prevent="toggleGroup(group.path)"
          >
            <component :is="expandedGroups.has(group.path) ? ChevronDown : ChevronRight" :size="15" />
            <component :is="expandedGroups.has(group.path) ? FolderOpen : Folder" :size="15" class="group-folder" aria-hidden="true" />
            <span class="group-title">{{ group.label }}</span>
            <span
              v-if="group.workspaceKind"
              class="badge workspace-kind-badge"
              :data-kind="group.workspaceKind"
              :title="group.workspaceConfidence ? `Classification confidence: ${group.workspaceConfidence}` : undefined"
            >{{ workspaceKindLabel(group.workspaceKind) }}</span>
            <span class="group-path mono" :title="group.path">{{ group.path }}</span>
            <span class="group-count" :aria-label="`${group.sessions.length} sessions`">{{ group.sessions.length }}</span>
            <button
              class="group-copy"
              type="button"
              :title="copiedPath === group.path ? 'Copied' : 'Copy directory path'"
              :aria-label="copiedPath === group.path ? 'Directory path copied' : 'Copy directory path'"
              @click.stop="copyWorkspace(group.path)"
            >
              <Check v-if="copiedPath === group.path" :size="14" aria-hidden="true" />
              <Copy v-else :size="14" aria-hidden="true" />
            </button>
          </div>
          <div
            v-show="expandedGroups.has(group.path)"
            :id="groupContentId(group.path)"
            class="group-rows"
            role="region"
            :aria-labelledby="groupHeaderId(group.path)"
          >
            <router-link
              v-for="session in group.sessions"
              :key="session.id"
              class="row"
              :to="{ path: `/history/${session.id}`, query: route.query }"
            >
              <div class="row-main">
                <div class="session-heading">
                  <strong class="session-title" :title="sessionTitle(session)">{{ sessionTitle(session) }}</strong>
                  <AgentBadge v-if="session.agent" :agent="session.agent" />
                  <span
                    v-if="isStepGoSession(session)"
                    class="badge launch-badge"
                    :title="`Launched by StepGo${session.stepgo_run_count ? ` (${session.stepgo_run_count} host runs)` : ''}`"
                  >via StepGo{{ session.stepgo_run_count && session.stepgo_run_count > 1 ? ` · ${session.stepgo_run_count}` : "" }}</span>
                  <span v-if="session.is_subagent" class="badge relation-badge" title="Child session spawned from another session">subagent</span>
                </div>
                <span class="muted mono session-id" :title="session.thread_id">id: {{ shortThreadId(session.thread_id) }}</span>
              </div>
              <div class="row-meta">
                <span>{{ session.model || "model unknown" }}</span>
                <span>{{ session.turn_count }} turns</span>
                <span>{{ (session.total_tokens ?? 0).toLocaleString() }} tokens</span>
                <span class="badge" :data-tone="sessionStatus(session).tone">{{ sessionStatus(session).label }}</span>
                <span
                  v-if="sessionWorkspaceKind(session)"
                  class="badge workspace-kind-badge"
                  :data-kind="sessionWorkspaceKind(session)"
                  :title="session.workspace_confidence ? `Classification confidence: ${session.workspace_confidence}` : undefined"
                >{{ workspaceKindLabel(sessionWorkspaceKind(session)!) }}</span>
                <span class="index" :data-status="session.index_status">{{ session.index_status }}</span>
                <span class="when" :title="absoluteTime(sessionActivityAt(session))">{{ relativeTime(sessionActivityAt(session)) }}</span>
              </div>
            </router-link>
          </div>
        </section>
        <button v-if="nextCursor" class="load-more" @click="load(false)">
          Load more
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Check, ChevronDown, ChevronRight, Copy, Folder, FolderOpen, RefreshCw, Search } from "@lucide/vue";
import {
  listHistory,
  listStepGoRuns,
  sessionActivityAt,
  sessionStatus,
  sessionTitle,
  type HistorySession,
  type StepGoRun,
  type WorkspaceKind,
} from "../api/history";
import AgentBadge from "../components/AgentBadge.vue";
import { AGENT_OPTIONS, type AgentFilter } from "../constants/agents";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const sessions = ref<HistorySession[]>([]);
const nextCursor = ref<string | null>(null);
const query = ref(typeof route.query.q === "string" ? route.query.q : "");
const routeAgent = typeof route.query.agent === "string" ? route.query.agent : "";
const showSubagents = ref(route.query.subagents === "true");
const agent = ref<AgentFilter>(
  AGENT_OPTIONS.some((option) => option.value === routeAgent)
    ? (routeAgent as AgentFilter)
    : ""
);
const loading = ref(true);
const error = ref("");
const expandedGroups = ref<Set<string>>(readExpandedGroups());
const copiedPath = ref<string | null>(null);
const stepgoRuns = ref<StepGoRun[]>([]);
const stepgoError = ref("");
const stepgoNextCursor = ref<string | null>(null);
const stepgoLoading = ref(false);
const UNKNOWN_WORKSPACE = "Unknown workspace";
const MAX_REMEMBERED_GROUPS = 200;

const stepgoUnlinkedCount = computed(
  () => stepgoRuns.value.filter((run) => run.link_status !== "linked").length
);

function expandedGroupsStorageKey(): string {
  const identity = auth.principal?.id || auth.username || "anonymous";
  return `agent-observatory.history.expanded-workspaces.v1:${identity}`;
}

function readExpandedGroups(): Set<string> {
  try {
    const stored = JSON.parse(localStorage.getItem(expandedGroupsStorageKey()) ?? "[]");
    if (!Array.isArray(stored)) return new Set();
    return new Set(
      stored
        .filter((path): path is string => typeof path === "string")
        .slice(-MAX_REMEMBERED_GROUPS)
    );
  } catch {
    return new Set();
  }
}

function persistExpandedGroups() {
  try {
    const paths = [...expandedGroups.value].slice(-MAX_REMEMBERED_GROUPS);
    localStorage.setItem(expandedGroupsStorageKey(), JSON.stringify(paths));
  } catch {
    // Browser storage may be unavailable; the in-memory state still works.
  }
}

function normalizeWorkspace(cwd: string | null): string {
  const path = cwd?.trim();
  if (!path) return UNKNOWN_WORKSPACE;
  // Keep filesystem roots intact while coalescing equivalent trailing separators.
  if (path === "/" || /^[A-Za-z]:[\\/]*$/.test(path)) return path;
  return path.replace(/[\\/]+$/, "") || path;
}

function workspaceLabel(path: string): string {
  if (path === UNKNOWN_WORKSPACE) return path;
  const trimmed = path.replace(/[\\/]+$/, "");
  return trimmed.split(/[\\/]/).pop() || path;
}

function groupIdPrefix(path: string): string {
  return `history-group-${encodeURIComponent(path)}`;
}

function groupHeaderId(path: string): string {
  return `${groupIdPrefix(path)}-header`;
}

function groupContentId(path: string): string {
  return `${groupIdPrefix(path)}-content`;
}

function compareSessions(left: HistorySession, right: HistorySession): number {
  const leftActivity = sessionActivityAt(left);
  const rightActivity = sessionActivityAt(right);
  const leftTime = leftActivity ? Date.parse(leftActivity) : Number.NEGATIVE_INFINITY;
  const rightTime = rightActivity ? Date.parse(rightActivity) : Number.NEGATIVE_INFINITY;
  if (leftTime !== rightTime) return rightTime - leftTime;
  return left.id.localeCompare(right.id, undefined, { numeric: true, sensitivity: "base" });
}

const groupedSessions = computed(() => {
  const groups = new Map<string, HistorySession[]>();
  for (const session of sessions.value) {
    const path = normalizeWorkspace(session.cwd);
    const items = groups.get(path) ?? [];
    items.push(session);
    groups.set(path, items);
  }
  return Array.from(groups, ([path, groupSessions]) => ({
    path,
    label: workspaceLabel(path),
    workspaceKind: sessionWorkspaceKind(groupSessions[0]),
    workspaceConfidence: groupSessions[0]?.workspace_confidence || null,
    sessions: [...groupSessions].sort(compareSessions),
  })).sort((left, right) => {
    const leftActivity = left.sessions[0] ? sessionActivityAt(left.sessions[0]) : null;
    const rightActivity = right.sessions[0] ? sessionActivityAt(right.sessions[0]) : null;
    const leftLatest = leftActivity ? Date.parse(leftActivity) : Number.NEGATIVE_INFINITY;
    const rightLatest = rightActivity ? Date.parse(rightActivity) : Number.NEGATIVE_INFINITY;
    const leftTime = Number.isNaN(leftLatest) ? Number.NEGATIVE_INFINITY : leftLatest;
    const rightTime = Number.isNaN(rightLatest) ? Number.NEGATIVE_INFINITY : rightLatest;
    if (leftTime !== rightTime) return rightTime - leftTime;
    return left.path.localeCompare(right.path, undefined, { numeric: true, sensitivity: "base" });
  });
});

function inferWorkspaceKind(path: string): WorkspaceKind {
  if (path === UNKNOWN_WORKSPACE) return "unknown";
  if (/pytest|test[-_]?fixture|fixture/i.test(path)) return "test_fixture";
  if (/^\/tmp\/(?:tmp|rcw-|task-)/i.test(path) || /\/rcw-[^/]+(?:\/|$)/i.test(path)) {
    return "ephemeral_task";
  }
  if (/^\/(?:etc|var|usr|root\/\.codex|root\/\.stepcode)(?:\/|$)/i.test(path)) {
    return "system_directory";
  }
  return "unknown";
}

function sessionWorkspaceKind(session: HistorySession | undefined): WorkspaceKind | null {
  if (!session) return null;
  if (session.workspace_kind) return session.workspace_kind;
  const inferred = inferWorkspaceKind(normalizeWorkspace(session.cwd));
  return inferred === "unknown" ? null : inferred;
}

function workspaceKindLabel(kind: WorkspaceKind): string {
  switch (kind) {
    case "persistent_project":
      return "project";
    case "ephemeral_task":
      return "ephemeral";
    case "test_fixture":
      return "test";
    case "system_directory":
      return "system";
    case "deleted_path":
      return "deleted";
    default:
      return "unknown";
  }
}

function onGroupClick(path: string, event: MouseEvent) {
  const selection = window.getSelection();
  if (
    selection && !selection.isCollapsed && selection.rangeCount > 0
    && event.currentTarget instanceof Node
    && selection.getRangeAt(0).intersectsNode(event.currentTarget)
  ) return;
  toggleGroup(path);
}

function toggleGroup(path: string) {
  const next = new Set(expandedGroups.value);
  if (next.has(path)) next.delete(path);
  else next.add(path);
  expandedGroups.value = next;
  persistExpandedGroups();
}

function shortThreadId(threadId: string): string {
  return threadId.length > 18 ? `${threadId.slice(0, 18)}...` : threadId;
}

function shortHostRunId(runId: string): string {
  return runId.length > 20 ? `${runId.slice(0, 20)}...` : runId;
}

function isStepGoSession(session: HistorySession): boolean {
  return (
    Boolean(session.launch_sources?.includes("stepgo")) ||
    (session.stepgo_run_count ?? 0) > 0
  );
}

function stepgoRunSessionTitle(run: StepGoRun): string {
  const session = run.session;
  if (!session) return run.native_session_id || "linked native session";
  return (
    session.display_title ||
    session.ai_title ||
    session.thread_id ||
    "linked native session"
  );
}

function stepgoLinkLabel(run: StepGoRun): string {
  if (run.link_status === "ambiguous") return "ambiguous native link";
  if (run.native_session_id || run.transcript_path) return "native session not indexed";
  return "unlinked";
}

function stepgoLinkTone(status: string): "ok" | "warn" | "muted" | "danger" {
  if (status === "linked") return "ok";
  if (status === "ambiguous") return "warn";
  if (status === "failed") return "danger";
  return "muted";
}

function stepgoStatusTone(status: string): "ok" | "warn" | "muted" | "danger" {
  if (status === "completed") return "ok";
  if (status === "failed") return "danger";
  if (status === "running") return "ok";
  return "warn";
}

async function copyWorkspace(path: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(path);
    copiedPath.value = path;
    window.setTimeout(() => {
      if (copiedPath.value === path) copiedPath.value = null;
    }, 1400);
  } catch {
    // Selecting the path remains available when clipboard permissions are unavailable.
  }
}

async function load(reset = true) {
  loading.value = true;
  error.value = "";
  try {
    const response = await listHistory({
      limit: 100,
      cursor: reset ? null : nextCursor.value,
      q: query.value,
      agent: agent.value || undefined,
      includeSubagents: showSubagents.value,
    });
    sessions.value = reset
      ? response.sessions
      : [...sessions.value, ...response.sessions];
    nextCursor.value = response.next_cursor;
    if (reset && agent.value === "stepgo") {
      await loadStepGoRuns();
    } else if (reset) {
      stepgoRuns.value = [];
      stepgoError.value = "";
      stepgoNextCursor.value = null;
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load history";
  } finally {
    loading.value = false;
  }
}

async function loadStepGoRuns(reset = true) {
  stepgoError.value = "";
  stepgoLoading.value = true;
  try {
    const response = await listStepGoRuns(reset
      ? { limit: 100 }
      : { limit: 100, cursor: stepgoNextCursor.value });
    stepgoRuns.value = reset ? response.runs : [...stepgoRuns.value, ...response.runs];
    stepgoNextCursor.value = response.next_cursor;
  } catch (cause) {
    if (reset) stepgoRuns.value = [];
    stepgoError.value =
      cause instanceof Error ? cause.message : "Unable to load StepGo host runs";
  } finally {
    stepgoLoading.value = false;
  }
}

async function applyFilters() {
  const nextQuery: Record<string, string> = {};
  if (query.value) nextQuery.q = query.value;
  if (agent.value) nextQuery.agent = agent.value;
  if (showSubagents.value) nextQuery.subagents = "true";
  await router.replace({ query: nextQuery });
  await load(true);
}

function changeAgent(event: Event) {
  agent.value = (event.target as HTMLSelectElement).value as AgentFilter;
  void applyFilters();
}

const MIN = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

/** Compact "time ago" for the list; server already sorts newest-first. */
function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Math.max(0, Date.now() - then);
  if (diff < MIN) return "just now";
  if (diff < HOUR) return `${Math.round(diff / MIN)}m ago`;
  if (diff < DAY) return `${Math.round(diff / HOUR)}h ago`;
  if (diff < 30 * DAY) return `${Math.round(diff / DAY)}d ago`;
  return new Date(iso).toLocaleDateString();
}

function absoluteTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

onMounted(() => load(true));
</script>

<style scoped>
.list-page { max-width: 1120px; margin: 0 auto; padding: 30px 22px; }
.page-header { display: flex; align-items: end; justify-content: space-between; margin-bottom: 24px; }
.header-actions { display: flex; align-items: center; gap: 8px; }
.agent-filter select { height: 34px; min-width: 126px; border: 1px solid var(--border); border-radius: 5px; padding: 0 28px 0 9px; background: var(--panel); color: var(--fg); cursor: pointer; }
.search-box { height: 34px; min-width: 260px; display: flex; align-items: center; gap: 7px; padding: 0 9px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg-muted); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.search-box input { min-width: 0; flex: 1; border: 0; outline: 0; background: transparent; color: var(--fg); }
.subagent-filter { display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 8px; color: var(--fg-muted); font-size: 12px; white-space: nowrap; cursor: pointer; }
.subagent-filter input { accent-color: var(--accent); }
.eyebrow { margin: 0 0 5px; color: var(--fg-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
h1 { margin: 0; font-size: 25px; letter-spacing: 0; }
.refresh { display: flex; align-items: center; gap: 7px; height: 34px; padding: 0 10px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); cursor: pointer; }
.history-groups { display: grid; gap: 14px; }
.stepgo-panel { margin-bottom: 22px; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.stepgo-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 14px 16px; border-bottom: 1px solid var(--border); background: var(--panel-2); }
.stepgo-heading h2 { margin: 0; font-size: 15px; }
.stepgo-heading p { margin: 4px 0 0; color: var(--fg-muted); font-size: 12px; }
.stepgo-counts { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.stepgo-run-list { display: grid; }
.stepgo-run { display: grid; gap: 7px; padding: 12px 16px; border-bottom: 1px solid var(--border); }
.stepgo-run:last-child { border-bottom: 0; }
.stepgo-run[data-link-status="ambiguous"] { background: color-mix(in srgb, var(--warn) 5%, transparent); }
.stepgo-run-main, .stepgo-run-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
.stepgo-run-meta { color: var(--fg-muted); font-size: 12px; }
.stepgo-link { min-width: 0; overflow: hidden; color: var(--fg); font-weight: 600; text-decoration: none; text-overflow: ellipsis; white-space: nowrap; }
.stepgo-link:hover { color: var(--accent); }
.stepgo-note { margin: 0; color: var(--warn); font-size: 12px; }
.launch-badge { border-color: color-mix(in srgb, #f08bb0 48%, var(--border)); background: color-mix(in srgb, #f08bb0 12%, transparent); color: #f5a4c2; }
.history-group { min-width: 0; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.group-header { width: 100%; min-height: 38px; display: grid; grid-template-columns: 18px 18px minmax(120px, max-content) auto minmax(0, 1fr) auto auto; align-items: center; gap: 8px; padding: 7px 10px; border-bottom: 1px solid var(--border); background: var(--panel-2); color: var(--fg); text-align: left; cursor: pointer; }
.group-header:hover { background: #202933; }
.group-folder { color: var(--accent); }
.group-title { min-width: 0; overflow: hidden; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.group-path { min-width: 0; overflow: hidden; color: var(--fg-muted); font-size: 12px; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; user-select: text; cursor: text; }
.group-count { min-width: 22px; color: var(--fg-muted); font-size: 12px; text-align: right; }
.group-copy { display: inline-grid; place-items: center; width: 24px; height: 24px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: var(--fg-muted); cursor: pointer; }
.group-copy:hover { background: var(--panel); color: var(--fg); }
.group-rows { display: grid; min-width: 0; overflow: hidden; border-left: 2px solid var(--border); }
.row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, 44%); align-items: center; gap: 18px; width: 100%; min-width: 0; padding: 14px 16px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: inherit; text-decoration: none; }
.group-rows .row { border-width: 0 0 1px; border-radius: 0; padding-left: 28px; }
.group-rows .row:last-child { border-bottom: 0; }
.row:hover { border-color: #4b657c; background: #1a222b; }
.row-main, .row-meta { display: flex; gap: 12px; align-items: center; min-width: 0; max-width: 100%; }
.row-main { flex-direction: column; align-items: flex-start; }
.session-heading { display: flex; align-items: center; gap: 8px; max-width: 100%; min-width: 0; }
.session-title { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-id { max-width: 100%; font-size: 11px; }
.row-meta { justify-content: flex-end; justify-self: end; flex-wrap: wrap; color: var(--fg-muted); font-size: 12px; white-space: normal; }
.row-meta > * { min-width: 0; max-width: 100%; overflow-wrap: anywhere; }
.row-meta .index[data-status="indexed"] { color: var(--ok); }
.when { color: var(--fg-muted); }
.badge { padding: 1px 8px; border-radius: 999px; font-size: 11px; background: var(--panel-2); border: 1px solid var(--border); color: var(--fg-muted); }
.badge[data-tone="ok"] { background: var(--ok-weak); color: var(--ok); border-color: transparent; }
.badge[data-tone="warn"] { background: var(--warn-weak); color: var(--warn); border-color: transparent; }
.badge[data-tone="danger"] { background: var(--danger-weak); color: var(--danger); border-color: transparent; }
.workspace-kind-badge[data-kind="ephemeral_task"], .workspace-kind-badge[data-kind="test_fixture"] { background: var(--warn-weak); color: var(--warn); border-color: transparent; }
.workspace-kind-badge[data-kind="persistent_project"] { background: var(--ok-weak); color: var(--ok); border-color: transparent; }
.workspace-kind-badge[data-kind="system_directory"], .workspace-kind-badge[data-kind="deleted_path"] { background: var(--panel-3); color: var(--fg-muted); border-color: transparent; }
.relation-badge { background: var(--accent-weak, var(--panel-2)); color: var(--accent); border-color: transparent; }
.muted { color: var(--fg-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 650px; }
.empty { padding: 56px 20px; border: 1px dashed var(--border); color: var(--fg-muted); text-align: center; }
.error { color: var(--danger); }
.load-more { height: 38px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); cursor: pointer; }
@media (max-width: 820px) { .group-rows .row { grid-template-columns: minmax(0, 1fr); gap: 8px; } .row-meta { justify-content: flex-start; justify-self: stretch; } }
@media (max-width: 700px) { .page-header { align-items: stretch; flex-direction: column; gap: 12px; } .header-actions { align-items: stretch; flex-direction: column; } .agent-filter select { width: 100%; } .search-box { min-width: 0; } .group-header { grid-template-columns: 18px 18px minmax(0, 1fr) auto auto; grid-template-rows: auto auto; } .group-title { grid-column: 3 / 4; grid-row: 1; } .group-path { grid-column: 3 / 4; grid-row: 2; } .group-count { grid-column: 4 / 5; grid-row: 1 / 3; align-self: center; } .group-copy { grid-column: 5 / 6; grid-row: 1 / 3; align-self: center; } .row-main, .row-meta { width: 100%; } .session-heading { flex-wrap: wrap; } .session-title, .session-id { max-width: 100%; } }
</style>
