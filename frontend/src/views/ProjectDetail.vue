<template>
  <main class="project-detail" aria-labelledby="project-title">
    <p v-if="loadingProject" class="state" role="status">Loading project…</p>
    <p v-else-if="notFound" class="state error" role="alert">
      Project not found.
      <router-link to="/projects">Back to projects</router-link>
    </p>
    <p v-else-if="projectError" class="state error" role="alert">{{ projectError }}</p>
    <template v-else-if="detail">
      <header class="detail-header">
        <div class="detail-heading">
          <router-link class="back-link" to="/projects">
            <ArrowLeft :size="15" aria-hidden="true" />
            Projects
          </router-link>
          <p class="eyebrow">Project usage</p>
          <h1 id="project-title">{{ detail.project.display_name }}</h1>
          <div class="path-line">
            <code class="mono" :title="detail.project.path">{{ detail.project.path }}</code>
            <button
              class="icon-btn"
              type="button"
              :title="copiedPath ? 'Copied' : 'Copy directory path'"
              :aria-label="copiedPath ? 'Directory path copied' : 'Copy directory path'"
              @click="copyPath"
            >
              <Check v-if="copiedPath" :size="14" aria-hidden="true" />
              <Copy v-else :size="14" aria-hidden="true" />
            </button>
          </div>
          <p class="activity">
            <span>{{ detail.project.is_allowed ? "Allowed workspace" : "Restricted workspace" }}</span>
            <span
              v-if="detail.project.workspace_kind"
              class="badge workspace-kind-badge"
              :data-kind="detail.project.workspace_kind"
              :title="detail.project.workspace_confidence ? `Classification confidence: ${detail.project.workspace_confidence}` : undefined"
            >{{ workspaceKindLabel(detail.project.workspace_kind) }}</span>
            <span>First active {{ formatTime(detail.project.first_active_at) }}</span>
            <span>Last active {{ formatTime(detail.project.last_active_at) }}</span>
          </p>
        </div>
        <button
          class="refresh"
          type="button"
          title="Refresh project"
          :disabled="loadingProject || loadingTrend"
          @click="load"
        >
          <RefreshCw :size="15" :class="{ spinning: loadingProject || loadingTrend }" aria-hidden="true" />
          Refresh
        </button>
      </header>

      <section class="metrics" aria-label="Token totals">
        <article v-for="item in totalCards" :key="item.key" class="metric">
          <span class="metric-label">{{ item.label }}</span>
          <strong class="metric-value">{{ number(item.value.total_tokens) }}</strong>
          <small>
            {{ number(item.value.input_tokens) }} input · {{ number(item.value.output_tokens) }} output
            · {{ number(item.value.cached_input_tokens) }} cached ·
            {{ number(item.value.reasoning_output_tokens) }} reasoning
          </small>
        </article>
      </section>
      <dl class="counts" aria-label="Project counts">
        <div><dt>Sessions</dt><dd>{{ number(detail.counts.session_count) }}</dd></div>
        <div><dt>Turns</dt><dd>{{ number(detail.counts.turn_count) }}</dd></div>
      </dl>

      <section class="panel trend-panel" aria-labelledby="trend-title">
        <header class="section-heading">
          <div>
            <p class="eyebrow">Usage over time</p>
            <h2 id="trend-title">Token trend</h2>
          </div>
          <div class="range-tabs" role="tablist" aria-label="Project trend range">
            <button
              v-for="item in trendRanges"
              :key="item.value"
              class="range-tab"
              type="button"
              role="tab"
              :aria-selected="trendRange === item.value"
              :class="{ active: trendRange === item.value }"
              @click="selectTrendRange(item.value)"
            >
              {{ item.label }}
            </button>
          </div>
        </header>
        <p v-if="trendError" class="inline-error" role="alert">{{ trendError }}</p>
        <BaseChart
          v-else
          :option="trendOption"
          :loading="loadingTrend"
          :empty="!timeseries?.points.length"
          label="Project token usage over time"
          height="300px"
        />
        <div class="table-wrap trend-table-wrap">
          <table>
            <thead>
              <tr><th>Bucket (UTC)</th><th>Sessions</th><th>Turns</th><th>Total tokens</th><th>Input</th><th>Output</th></tr>
            </thead>
            <tbody>
              <tr v-for="point in timeseries?.points || []" :key="point.timestamp">
                <td>{{ formatTime(point.timestamp) }}</td>
                <td>{{ number(point.session_count) }}</td>
                <td>{{ number(point.turn_count) }}</td>
                <td>{{ number(point.total_tokens) }}</td>
                <td>{{ number(point.input_tokens) }}</td>
                <td>{{ number(point.output_tokens) }}</td>
              </tr>
              <tr v-if="!timeseries?.points.length">
                <td colspan="6" class="empty-cell">No indexed activity in this range.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <div class="split-grid">
        <section class="panel" aria-labelledby="agent-split-title">
          <header class="section-heading">
            <div><p class="eyebrow">Contributors</p><h2 id="agent-split-title">By agent</h2></div>
          </header>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Agent</th><th>Sessions</th><th>Turns</th><th>Tokens</th></tr></thead>
              <tbody>
                <tr v-for="item in detail.by_agent" :key="item.agent || 'unknown'">
                  <td>{{ item.agent || "Unknown" }}</td>
                  <td>{{ number(item.session_count) }}</td>
                  <td>{{ number(item.turn_count) }}</td>
                  <td>{{ number(item.total_tokens) }}</td>
                </tr>
                <tr v-if="!detail.by_agent.length"><td colspan="4" class="empty-cell">No agent breakdown available.</td></tr>
              </tbody>
            </table>
          </div>
        </section>
        <section class="panel" aria-labelledby="model-split-title">
          <header class="section-heading">
            <div><p class="eyebrow">Model usage</p><h2 id="model-split-title">By model</h2></div>
          </header>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Model</th><th>Sessions</th><th>Turns</th><th>Tokens</th></tr></thead>
              <tbody>
                <tr v-for="item in detail.by_model" :key="item.model || 'unknown'">
                  <td class="breakable">{{ item.model || "Unknown" }}</td>
                  <td>{{ number(item.session_count) }}</td>
                  <td>{{ number(item.turn_count) }}</td>
                  <td>{{ number(item.total_tokens) }}</td>
                </tr>
                <tr v-if="!detail.by_model.length"><td colspan="4" class="empty-cell">No model breakdown available.</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section class="panel sessions-panel" aria-labelledby="sessions-title">
        <header class="section-heading">
          <div><p class="eyebrow">Indexed history</p><h2 id="sessions-title">Project sessions</h2></div>
        </header>
        <p v-if="sessionsError" class="inline-error" role="alert">{{ sessionsError }}</p>
        <p v-else-if="loadingSessions" class="panel-state" role="status">Loading sessions…</p>
        <p v-else-if="!sessions.length" class="panel-state">No indexed sessions for this project.</p>
        <div v-else class="session-list">
          <router-link v-for="session in sessions" :key="session.id" class="session-row" :to="`/history/${encodeURIComponent(session.id)}`">
            <div class="session-main">
              <strong :title="session.thread_id">{{ sessionTitle(session) }}</strong>
              <span class="muted mono">{{ session.model || "model unknown" }}</span>
            </div>
            <div class="session-meta">
              <span>{{ number(session.total_tokens) }} tokens</span>
              <span>{{ number(session.turn_count) }} turns</span>
              <span
                v-if="session.workspace_kind"
                class="badge workspace-kind-badge"
                :data-kind="session.workspace_kind"
                :title="session.workspace_confidence ? `Classification confidence: ${session.workspace_confidence}` : undefined"
              >{{ workspaceKindLabel(session.workspace_kind) }}</span>
              <time :title="formatTime(sessionActivityAt(session))">{{ relativeTime(sessionActivityAt(session)) }}</time>
            </div>
          </router-link>
        </div>
        <div v-if="sessionsCursor" class="sessions-more">
          <button
            class="load-more"
            type="button"
            :disabled="loadingMoreSessions"
            @click="loadMoreSessions"
          >
            {{ loadingMoreSessions ? "Loading…" : "Load more sessions" }}
          </button>
        </div>
      </section>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ArrowLeft, Check, Copy, RefreshCw } from "@lucide/vue";
import type { EChartsCoreOption } from "echarts/core";
import { ApiError } from "../api/client";
import {
  getProject,
  getProjectTimeseries,
  listProjectSessions,
  type ProjectDetail as ProjectDetailPayload,
  type ProjectTimeseries,
  type ProjectTimeseriesRange,
} from "../api/projects";
import {
  sessionActivityAt,
  sessionTitle,
  type HistorySession,
  type WorkspaceKind,
} from "../api/history";
import BaseChart from "../components/BaseChart.vue";

const route = useRoute();
const router = useRouter();
const detail = ref<ProjectDetailPayload | null>(null);
const timeseries = ref<ProjectTimeseries | null>(null);
const sessions = ref<HistorySession[]>([]);
const loadingProject = ref(true);
const loadingTrend = ref(false);
const loadingSessions = ref(false);
const loadingMoreSessions = ref(false);
const projectError = ref("");
const trendError = ref("");
const sessionsError = ref("");
const sessionsCursor = ref<string | null>(null);
const notFound = ref(false);
const copiedPath = ref(false);

const trendRanges: ReadonlyArray<{ value: ProjectTimeseriesRange; label: string }> = [
  { value: "day", label: "24h" },
  { value: "week", label: "7d" },
  { value: "month", label: "30d" },
];
const requestedTrendRange =
  typeof route.query.trend === "string" ? route.query.trend : "week";
const trendRange = ref<ProjectTimeseriesRange>(
  trendRanges.some((item) => item.value === requestedTrendRange)
    ? (requestedTrendRange as ProjectTimeseriesRange)
    : "week",
);

const totalCards = computed(() => {
  if (!detail.value) return [];
  return [
    { key: "all_time", label: "All time", value: detail.value.totals.all_time },
    { key: "day", label: "Last 24 hours", value: detail.value.totals.day },
    { key: "week", label: "Last 7 days", value: detail.value.totals.week },
    { key: "month", label: "Last 30 days", value: detail.value.totals.month },
  ];
});

const trendOption = computed<EChartsCoreOption>(() => {
  const points = timeseries.value?.points || [];
  return {
    grid: { top: 30, right: 38, bottom: 42, left: 60, containLabel: true },
    tooltip: { trigger: "axis" },
    legend: { data: ["Tokens", "Turns"] },
    xAxis: { type: "category", boundaryGap: false, data: points.map((point) => formatChartDate(point.timestamp)) },
    yAxis: [
      { type: "value", name: "Tokens" },
      { type: "value", name: "Turns", minInterval: 1, splitLine: { show: false } },
    ],
    series: [
      { name: "Tokens", type: "line", yAxisIndex: 0, smooth: true, showSymbol: false, data: points.map((point) => point.total_tokens), areaStyle: { opacity: 0.08 } },
      { name: "Turns", type: "line", yAxisIndex: 1, smooth: true, showSymbol: false, data: points.map((point) => point.turn_count) },
    ],
  };
});

function workspaceId(): string {
  const value = route.params.id;
  return typeof value === "string" ? value : Array.isArray(value) ? value[0] || "" : "";
}

function number(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString();
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function formatChartDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return trendRange.value === "day"
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function relativeTime(value: string | null): string {
  if (!value) return "—";
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "—";
  const diff = Math.max(0, Date.now() - timestamp);
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return "just now";
  if (diff < hour) return `${Math.round(diff / minute)}m ago`;
  if (diff < day) return `${Math.round(diff / hour)}h ago`;
  if (diff < 30 * day) return `${Math.round(diff / day)}d ago`;
  return new Date(value).toLocaleDateString();
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

async function copyPath() {
  const path = detail.value?.project.path;
  if (!path) return;
  try {
    await navigator.clipboard.writeText(path);
    copiedPath.value = true;
    window.setTimeout(() => (copiedPath.value = false), 1400);
  } catch {
    // Path text remains selectable when clipboard permissions are unavailable.
  }
}

async function loadTrend() {
  const id = workspaceId();
  if (!id) return;
  loadingTrend.value = true;
  trendError.value = "";
  try {
    timeseries.value = await getProjectTimeseries(id, trendRange.value);
  } catch (cause) {
    trendError.value = cause instanceof Error ? cause.message : "Unable to load project trend";
  } finally {
    loadingTrend.value = false;
  }
}

async function loadSessions() {
  const id = workspaceId();
  if (!id) return;
  loadingSessions.value = true;
  sessionsError.value = "";
  sessionsCursor.value = null;
  try {
    const response = await listProjectSessions(id, { limit: 100 });
    sessions.value = response.sessions;
    sessionsCursor.value = response.next_cursor;
  } catch (cause) {
    sessionsError.value = cause instanceof Error ? cause.message : "Unable to load project sessions";
  } finally {
    loadingSessions.value = false;
  }
}

async function loadMoreSessions() {
  const id = workspaceId();
  const cursor = sessionsCursor.value;
  if (!id || !cursor || loadingMoreSessions.value) return;
  loadingMoreSessions.value = true;
  sessionsError.value = "";
  try {
    const response = await listProjectSessions(id, { limit: 100, cursor });
    sessions.value = [...sessions.value, ...response.sessions];
    sessionsCursor.value = response.next_cursor;
  } catch (cause) {
    sessionsError.value =
      cause instanceof Error ? cause.message : "Unable to load more project sessions";
  } finally {
    loadingMoreSessions.value = false;
  }
}

async function load() {
  const id = workspaceId();
  if (!id) {
    notFound.value = true;
    loadingProject.value = false;
    return;
  }
  loadingProject.value = true;
  projectError.value = "";
  notFound.value = false;
  try {
    detail.value = await getProject(id);
  } catch (cause) {
    detail.value = null;
    notFound.value = cause instanceof ApiError && cause.status === 404;
    projectError.value = notFound.value ? "" : cause instanceof Error ? cause.message : "Unable to load project";
    loadingProject.value = false;
    return;
  }
  loadingProject.value = false;
  void Promise.all([loadTrend(), loadSessions()]);
}

function selectTrendRange(value: ProjectTimeseriesRange) {
  if (trendRange.value === value) return;
  trendRange.value = value;
  void router.replace({
    query: {
      ...route.query,
      trend: value,
    },
  });
  void loadTrend();
}

onMounted(() => void load());
watch(() => route.params.id, () => void load());
watch(
  () => route.query.trend,
  (value) => {
    const nextRange = trendRanges.some((item) => item.value === value)
      ? (value as ProjectTimeseriesRange)
      : "week";
    if (nextRange === trendRange.value) return;
    trendRange.value = nextRange;
    void loadTrend();
  },
);
</script>

<style scoped>
.project-detail { max-width: 1240px; margin: 0 auto; padding: 28px 24px 52px; }
.detail-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 22px; }
.back-link, .text-link { display: inline-flex; align-items: center; gap: 5px; color: var(--fg-muted); font-size: 12px; text-decoration: none; }
.back-link:hover, .text-link:hover { color: var(--fg); }
.eyebrow { margin: 13px 0 4px; color: var(--fg-muted); font-size: 10px; text-transform: uppercase; letter-spacing: .08em; }
h1 { margin: 0; font-size: 27px; letter-spacing: 0; }
.path-line { display: flex; align-items: flex-start; gap: 8px; max-width: min(920px, 100%); margin-top: 8px; }
.path-line code { min-width: 0; overflow-wrap: anywhere; color: var(--fg-muted); font-size: 12px; user-select: text; }
.icon-btn, .refresh { display: inline-flex; align-items: center; justify-content: center; gap: 7px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); cursor: pointer; }
.icon-btn { width: 27px; height: 27px; flex: 0 0 auto; padding: 0; }
.icon-btn:hover { border-color: var(--border-strong); }
.refresh { height: 34px; padding: 0 11px; }
.refresh:disabled { cursor: wait; opacity: .65; }
.activity { display: flex; flex-wrap: wrap; gap: 5px 14px; margin: 10px 0 0; color: var(--fg-muted); font-size: 12px; }
.activity span + span { position: relative; padding-left: 14px; }
.activity span + span::before { position: absolute; left: 0; content: "·"; color: var(--fg-subtle); }
.workspace-kind-badge { padding: 1px 7px; border-radius: 999px; font-size: 10px; text-transform: uppercase; border: 1px solid transparent; }
.workspace-kind-badge[data-kind="persistent_project"] { background: var(--ok-weak); color: var(--ok); }
.workspace-kind-badge[data-kind="ephemeral_task"], .workspace-kind-badge[data-kind="test_fixture"] { background: var(--warn-weak); color: var(--warn); }
.workspace-kind-badge[data-kind="system_directory"], .workspace-kind-badge[data-kind="deleted_path"] { background: var(--panel-3); color: var(--fg-muted); }
.mono { font-family: var(--mono); }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; }
.metric { min-width: 0; padding: 15px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
.metric-label { display: block; color: var(--fg-muted); font-size: 12px; }
.metric-value { display: block; margin-top: 7px; font-size: 22px; font-variant-numeric: tabular-nums; }
.metric small { display: block; margin-top: 5px; color: var(--fg-muted); font-size: 11px; overflow-wrap: anywhere; }
.counts { display: flex; gap: 22px; margin: 13px 0 20px; }
.counts div { display: flex; gap: 7px; }
.counts dt { color: var(--fg-muted); font-size: 13px; }
.counts dd { margin: 0; font-weight: 600; font-variant-numeric: tabular-nums; }
.panel { margin-top: 16px; padding: 17px 18px 18px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 11px; }
.section-heading .eyebrow { margin: 0 0 3px; }
h2 { margin: 0; font-size: 17px; }
.range-tabs { display: inline-flex; gap: 2px; padding: 2px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-2); }
.range-tab { border: 0; border-radius: 4px; padding: 6px 10px; background: transparent; color: var(--fg-muted); cursor: pointer; font-size: 12px; }
.range-tab.active, .range-tab:hover { background: var(--panel-3); color: var(--fg); }
.inline-error { color: var(--danger); font-size: 13px; }
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 5px; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 9px 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { background: var(--panel-2); color: var(--fg-muted); font-weight: 500; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: color-mix(in srgb, var(--panel-2) 65%, transparent); }
.empty-cell { padding: 22px 10px; color: var(--fg-muted); text-align: center; }
.trend-table-wrap { margin-top: 11px; }
.split-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.breakable { overflow-wrap: anywhere; }
.sessions-panel { margin-top: 16px; }
.session-list { display: grid; border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }
.sessions-more { display: flex; justify-content: center; margin-top: 12px; }
.load-more { min-height: 32px; padding: 0 11px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); cursor: pointer; font-size: 12px; }
.load-more:disabled { cursor: wait; opacity: .65; }
.session-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px; padding: 11px 12px; border-bottom: 1px solid var(--border); color: inherit; text-decoration: none; }
.session-row:last-child { border-bottom: 0; }
.session-row:hover { background: var(--panel-2); }
.session-main { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.session-main strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.muted { color: var(--fg-muted); font-size: 11px; }
.session-meta { display: flex; align-items: center; justify-content: flex-end; gap: 13px; color: var(--fg-muted); font-size: 11px; white-space: nowrap; }
.panel-state, .state { padding: 40px 18px; color: var(--fg-muted); text-align: center; }
.state.error { color: var(--danger); }
.state.error a { display: block; margin-top: 8px; }
.spinning { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 900px) { .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } .split-grid { grid-template-columns: 1fr; } }
@media (max-width: 640px) { .project-detail { padding: 22px 14px 38px; } .detail-header { flex-direction: column; } .refresh { align-self: flex-start; } .section-heading { align-items: stretch; flex-direction: column; } .range-tabs { align-self: flex-start; } .session-row { grid-template-columns: 1fr; } .session-meta { justify-content: flex-start; flex-wrap: wrap; } }
@media (max-width: 440px) { .metrics { grid-template-columns: 1fr; } .metric-value { font-size: 20px; } }
</style>
