<template>
  <main class="projects-page" aria-labelledby="projects-title">
    <header class="page-header">
      <div>
        <p class="eyebrow">Directory-level usage</p>
        <h1 id="projects-title">Projects</h1>
        <p class="subtitle">Token usage grouped by exact workspace directory.</p>
      </div>
      <button
        class="refresh"
        type="button"
        title="Refresh projects"
        :disabled="loading"
        @click="load()"
      >
        <RefreshCw :size="15" :class="{ spinning: loading }" aria-hidden="true" />
        Refresh
      </button>
    </header>

    <section class="controls" aria-label="Project filters">
      <div class="search-box">
        <Search :size="15" aria-hidden="true" />
        <input
          v-model.trim="query"
          type="search"
          placeholder="Search project or directory"
          aria-label="Search projects"
          @keydown.enter="applyFilters"
        />
      </div>
      <div class="range-tabs" role="tablist" aria-label="Project token range">
        <button
          v-for="item in ranges"
          :key="item.value"
          class="range-tab"
          type="button"
          role="tab"
          :aria-selected="selectedRange === item.value"
          :class="{ active: selectedRange === item.value }"
          @click="selectRange(item.value)"
        >
          {{ item.label }}
        </button>
      </div>
    </section>

    <p v-if="error" class="state error" role="alert">{{ error }}</p>
    <p v-else-if="loading && !projects.length" class="state" role="status">
      Loading projects…
    </p>
    <p v-else-if="!projects.length" class="state" role="status">
      No indexed projects match these filters.
    </p>
    <section v-else class="project-table-wrap" aria-label="Projects">
      <div class="project-table" role="table">
        <div class="project-row project-row--header" role="row">
          <span role="columnheader">Project</span>
          <span role="columnheader">Last active</span>
          <span role="columnheader">Sessions</span>
          <span role="columnheader">Turns</span>
          <span role="columnheader">All-time tokens</span>
          <span role="columnheader">Selected range</span>
        </div>
        <article v-for="project in projects" :key="project.id" class="project-row" role="row">
          <router-link
            class="project-main"
            role="cell"
            :to="`/projects/${encodeURIComponent(project.id)}`"
          >
            <span class="project-title">
              <FolderOpen :size="16" aria-hidden="true" />
              <strong :title="project.display_name">{{ project.display_name }}</strong>
              <span
                v-if="project.workspace_kind"
                class="badge workspace-kind-badge"
                :data-kind="project.workspace_kind"
                :title="project.workspace_confidence ? `Classification confidence: ${project.workspace_confidence}` : undefined"
              >{{ workspaceKindLabel(project.workspace_kind) }}</span>
              <span v-if="!project.is_allowed" class="badge" data-tone="warn">restricted</span>
            </span>
            <span class="project-path mono" :title="project.path">{{ project.path }}</span>
          </router-link>
          <div class="project-meta" role="cell">
            <time :title="absoluteTime(project.last_active_at)">
              {{ relativeTime(project.last_active_at) }}
            </time>
          </div>
          <div class="project-meta" role="cell">{{ number(project.session_count) }}</div>
          <div class="project-meta" role="cell">{{ number(project.turn_count) }}</div>
          <div class="project-meta project-meta--emphasis" role="cell">
            {{ number(project.all_time.total_tokens) }}
          </div>
          <div class="project-meta project-meta--emphasis" role="cell">
            {{ number(project.selected_range.total_tokens) }}
          </div>
          <button
            class="copy-btn"
            type="button"
            :title="copiedPath === project.path ? 'Copied' : 'Copy directory path'"
            :aria-label="copiedPath === project.path ? 'Directory path copied' : 'Copy directory path'"
            @click="copyPath(project.path)"
          >
            <Check v-if="copiedPath === project.path" :size="14" aria-hidden="true" />
            <Copy v-else :size="14" aria-hidden="true" />
          </button>
        </article>
      </div>
    </section>
    <div v-if="page && page.total > projects.length" class="pagination">
      <p class="result-count">
        Showing {{ number(projects.length) }} of {{ number(page.total) }} projects
      </p>
      <button class="load-more" type="button" :disabled="loadingMore" @click="loadMore">
        {{ loadingMore ? "Loading…" : "Load more projects" }}
      </button>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Check, Copy, FolderOpen, RefreshCw, Search } from "@lucide/vue";
import {
  listProjects,
  type ProjectRange,
  type ProjectSummary,
  type ProjectsPage,
} from "../api/projects";
import type { WorkspaceKind } from "../api/history";

const route = useRoute();
const router = useRouter();
const ranges: ReadonlyArray<{ value: ProjectRange; label: string }> = [
  { value: "all", label: "All time" },
  { value: "day", label: "Last 24 hours" },
  { value: "week", label: "Last 7 days" },
  { value: "month", label: "Last 30 days" },
];

const initialRange = typeof route.query.range === "string" ? route.query.range : "month";
const selectedRange = ref<ProjectRange>(
  ranges.some((item) => item.value === initialRange) ? (initialRange as ProjectRange) : "month"
);
const query = ref(typeof route.query.q === "string" ? route.query.q : "");
const projects = ref<ProjectSummary[]>([]);
const page = ref<ProjectsPage | null>(null);
const loading = ref(false);
const loadingMore = ref(false);
const error = ref("");
const copiedPath = ref<string | null>(null);
const offset = ref(0);
const hasMore = computed(() => Boolean(page.value && projects.value.length < page.value.total));

function normalizeRange(value: unknown): ProjectRange {
  return ranges.some((item) => item.value === value) ? (value as ProjectRange) : "month";
}

function number(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString();
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

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const diff = Math.max(0, Date.now() - then);
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return "just now";
  if (diff < hour) return `${Math.round(diff / minute)}m ago`;
  if (diff < day) return `${Math.round(diff / hour)}h ago`;
  if (diff < 30 * day) return `${Math.round(diff / day)}d ago`;
  return new Date(iso).toLocaleDateString();
}

function absoluteTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleString();
}

async function copyPath(path: string) {
  try {
    await navigator.clipboard.writeText(path);
    copiedPath.value = path;
    window.setTimeout(() => {
      if (copiedPath.value === path) copiedPath.value = null;
    }, 1400);
  } catch {
    // The full path remains selectable if clipboard access is unavailable.
  }
}

async function syncQuery() {
  const nextQuery: Record<string, string> = { range: selectedRange.value };
  if (query.value) nextQuery.q = query.value;
  await router.replace({ query: nextQuery });
}

async function applyFilters() {
  await syncQuery();
  offset.value = 0;
  await load();
}

function selectRange(range: ProjectRange) {
  if (selectedRange.value === range) return;
  selectedRange.value = range;
  void applyFilters();
}

async function load(append = false) {
  if (!append) offset.value = 0;
  if (append) loadingMore.value = true;
  else loading.value = true;
  error.value = "";
  try {
    const response = await listProjects({
      limit: 50,
      offset: offset.value,
      q: query.value || undefined,
      range: selectedRange.value,
    });
    page.value = response;
    projects.value = append ? [...projects.value, ...response.items] : response.items;
    offset.value += response.items.length;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load projects";
  } finally {
    if (append) loadingMore.value = false;
    else loading.value = false;
  }
}

function loadMore() {
  if (loading.value || loadingMore.value || !hasMore.value) return;
  void load(true);
}

// Keep browser Back/Forward navigation and externally-triggered route changes
// authoritative.  Filter actions update the refs before replacing the route,
// so this watcher does not issue a duplicate request for the same state.
watch(
  () => [route.query.range, route.query.q] as const,
  ([nextRange, nextQuery]) => {
    const normalizedRange = normalizeRange(nextRange);
    const normalizedQuery = typeof nextQuery === "string" ? nextQuery : "";
    if (
      normalizedRange === selectedRange.value &&
      normalizedQuery === query.value
    ) {
      return;
    }
    selectedRange.value = normalizedRange;
    query.value = normalizedQuery;
    void load();
  },
);

onMounted(() => void load());
</script>

<style scoped>
.projects-page { max-width: 1240px; margin: 0 auto; padding: 30px 24px 48px; }
.page-header { display: flex; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 22px; }
.eyebrow { margin: 0 0 5px; color: var(--fg-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
h1 { margin: 0; font-size: 26px; letter-spacing: 0; }
.subtitle { margin: 5px 0 0; color: var(--fg-muted); font-size: 13px; }
.refresh, .copy-btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); cursor: pointer; }
.refresh { height: 34px; padding: 0 11px; }
.refresh:disabled { cursor: wait; opacity: .65; }
.spinning { animation: spin 1s linear infinite; }
.controls { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 17px; }
.search-box { display: flex; align-items: center; gap: 7px; width: min(390px, 100%); height: 35px; padding: 0 10px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg-muted); }
.search-box input { min-width: 0; flex: 1; border: 0; outline: 0; background: transparent; color: var(--fg); }
.range-tabs { display: inline-flex; gap: 2px; padding: 2px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
.range-tab { border: 0; border-radius: 4px; padding: 7px 11px; background: transparent; color: var(--fg-muted); cursor: pointer; font: inherit; font-size: 12px; }
.range-tab:hover, .range-tab.active { background: var(--panel-3); color: var(--fg); }
.state { padding: 56px 20px; border: 1px dashed var(--border); color: var(--fg-muted); text-align: center; }
.state.error { color: var(--danger); }
.project-table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
.project-table { min-width: 860px; }
.project-row { position: relative; display: grid; grid-template-columns: minmax(280px, 1.8fr) minmax(100px, .8fr) 86px 74px minmax(120px, .85fr) minmax(120px, .85fr) 34px; align-items: center; gap: 10px; min-height: 68px; padding: 10px 12px; border-bottom: 1px solid var(--border); }
.project-row:last-child { border-bottom: 0; }
.project-row:not(.project-row--header):hover { background: color-mix(in srgb, var(--panel-2) 75%, transparent); }
.project-row--header { min-height: 38px; background: var(--panel-2); color: var(--fg-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.project-main { display: block; min-width: 0; color: inherit; text-decoration: none; }
.project-title { display: flex; align-items: center; gap: 7px; min-width: 0; }
.project-title svg { flex: 0 0 auto; color: var(--accent); }
.project-title strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-path { display: block; max-width: 100%; margin-top: 3px; overflow-wrap: anywhere; color: var(--fg-muted); font-size: 11px; line-height: 1.35; user-select: text; }
.mono { font-family: var(--mono); }
.project-meta { min-width: 0; color: var(--fg-muted); font-size: 12px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.project-meta--emphasis { color: var(--fg); font-weight: 600; }
.copy-btn { width: 26px; height: 26px; padding: 0; border-color: transparent; color: var(--fg-muted); }
.copy-btn:hover { border-color: var(--border); color: var(--fg); }
.badge { width: max-content; padding: 1px 6px; border: 1px solid transparent; border-radius: 999px; background: var(--warn-weak); color: var(--warn); font-size: 10px; font-weight: 500; text-transform: uppercase; }
.workspace-kind-badge[data-kind="persistent_project"] { background: var(--ok-weak); color: var(--ok); }
.workspace-kind-badge[data-kind="system_directory"], .workspace-kind-badge[data-kind="deleted_path"] { background: var(--panel-3); color: var(--fg-muted); }
.pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 9px; }
.result-count { margin: 0 2px; color: var(--fg-muted); font-size: 12px; }
.load-more { min-height: 32px; padding: 0 11px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); cursor: pointer; font-size: 12px; }
.load-more:disabled { cursor: wait; opacity: .65; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 840px) { .controls { align-items: stretch; flex-direction: column; } .search-box { width: 100%; } .range-tabs { justify-content: stretch; } .range-tab { flex: 1; } }
@media (max-width: 620px) { .projects-page { padding: 22px 14px 36px; } .page-header { align-items: stretch; flex-direction: column; } .refresh { align-self: flex-start; } }
</style>
