<template>
  <div class="system-page">
    <header class="system-page__header">
      <div>
        <p class="eyebrow">Administration</p>
        <h1>System</h1>
        <p class="subhead">Runtime health, recovery guidance, and sanitized application logs.</p>
      </div>
      <button class="ghost-btn" type="button" :disabled="refreshing" @click="refresh">
        <RefreshCw :size="15" :class="{ spin: refreshing }" />
        Refresh
      </button>
    </header>

    <p v-if="statusError" class="notice notice--danger" role="alert">
      {{ statusError }}
    </p>

    <section class="summary" :data-tone="overallTone">
      <div>
        <span class="summary__label">Overall status</span>
        <strong>{{ status?.status ?? (refreshing ? "checking" : "unavailable") }}</strong>
      </div>
      <span v-if="status?.generated_at" class="muted">
        Updated {{ formatTime(status.generated_at) }}
      </span>
    </section>

    <section class="component-grid" aria-label="System components">
      <article v-if="status?.supervisor" class="component-card" :data-tone="tone(status.supervisor.status)">
        <header>
          <strong>Supervisor</strong>
          <span class="status-chip" :data-tone="tone(status.supervisor.status)">
            {{ label(status.supervisor.status) }}
          </span>
        </header>
        <dl>
          <template v-if="status.supervisor.pid">
            <dt>PID</dt>
            <dd>{{ status.supervisor.pid }}</dd>
          </template>
          <template v-if="status.supervisor.started_at || status.supervisor.updated_at">
            <dt>{{ status.supervisor.started_at ? "Started" : "Updated" }}</dt>
            <dd>{{ formatTime(status.supervisor.started_at || status.supervisor.updated_at || "") }}</dd>
          </template>
          <template v-if="status.supervisor.restart_count != null">
            <dt>Restarts</dt>
            <dd>{{ status.supervisor.restart_count }}</dd>
          </template>
        </dl>
        <p v-if="status.supervisor.message" class="component-card__message">
          {{ status.supervisor.message }}
        </p>
        <p v-if="status.supervisor.last_error" class="component-card__error">
          {{ status.supervisor.last_error }}
        </p>
      </article>

      <article
        v-for="[name, component] in components"
        :key="name"
        class="component-card"
        :data-tone="tone(component.runtime_status || component.status)"
      >
        <header>
          <strong>{{ prettyName(name) }}</strong>
          <span class="status-chip" :data-tone="tone(component.runtime_status || component.status)">
            {{ label(component.runtime_status || component.status) }}
          </span>
        </header>
        <dl>
          <template v-if="component.pid">
            <dt>PID</dt>
            <dd>{{ component.pid }}</dd>
          </template>
          <template v-if="component.last_seen_at">
            <dt>Last seen</dt>
            <dd>{{ formatTime(component.last_seen_at) }}</dd>
          </template>
          <template v-if="component.last_success_at">
            <dt>Last success</dt>
            <dd>{{ formatTime(component.last_success_at) }}</dd>
          </template>
          <template v-if="component.restart_count != null">
            <dt>Restarts</dt>
            <dd>{{ component.restart_count }}</dd>
          </template>
          <template v-if="component.next_restart_at">
            <dt>Next restart</dt>
            <dd>{{ formatTime(component.next_restart_at) }}</dd>
          </template>
        </dl>
        <p v-if="component.message" class="component-card__message">{{ component.message }}</p>
        <p v-if="component.last_error" class="component-card__error">
          {{ component.last_error }}
        </p>
      </article>
    </section>

    <section v-if="status?.recovery_actions.length" class="panel recovery">
      <header class="panel__head">
        <div>
          <p class="eyebrow">Suggested actions</p>
          <h2>Recovery</h2>
        </div>
      </header>
      <ol>
        <li v-for="action in status.recovery_actions" :key="action">
          <code>{{ action }}</code>
          <button type="button" title="Copy recovery action" @click="copy(action)">
            <Copy :size="14" />
            Copy
          </button>
        </li>
      </ol>
    </section>

    <section class="panel logs">
      <header class="panel__head logs__head">
        <div>
          <p class="eyebrow">Sanitized output</p>
          <h2>Logs</h2>
        </div>
        <label>
          <span>Component</span>
          <select v-model="logComponent" @change="loadLogs(true)">
            <option value="all">All</option>
            <option v-for="name in logComponents" :key="name" :value="name">
              {{ prettyName(name) }}
            </option>
          </select>
        </label>
      </header>

      <p v-if="logsError" class="notice notice--danger" role="alert">{{ logsError }}</p>
      <p v-if="logsLoading && logs.length === 0" class="logs__empty">Loading logs…</p>
      <p v-else-if="logs.length === 0" class="logs__empty">No log entries.</p>
      <ol v-else class="log-list">
        <li v-for="(entry, index) in logs" :key="`${entry.timestamp}-${index}`">
          <time :title="entry.timestamp">{{ formatTime(entry.timestamp) }}</time>
          <span class="log-list__component">{{ entry.component }}</span>
          <span class="status-chip" :data-tone="logTone(entry.level)">{{ entry.level }}</span>
          <pre>{{ entry.message }}</pre>
          <button type="button" title="Copy full log message" @click="copy(entry.message)">
            <Copy :size="14" />
          </button>
        </li>
      </ol>
      <button
        v-if="nextCursor"
        class="load-more"
        type="button"
        :disabled="logsLoading"
        @click="loadLogs(false)"
      >
        {{ logsLoading ? "Loading…" : "Load older logs" }}
      </button>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Copy, RefreshCw } from "@lucide/vue";
import {
  componentEntries,
  getSystemLogs,
  getSystemStatus,
  type SystemLogEntry,
  type SystemStatus,
} from "../api/system";

const status = ref<SystemStatus | null>(null);
const statusError = ref("");
const refreshing = ref(false);
const logs = ref<SystemLogEntry[]>([]);
const logsError = ref("");
const logsLoading = ref(false);
const nextCursor = ref<string | null>(null);
const logComponent = ref("all");

const components = computed(() => componentEntries(status.value));
const logComponents = computed(() => {
  return ["api", "frontend", "supervisor", "worker"];
});
const overallTone = computed(() =>
  status.value?.status === "ok" ? "ok" : status.value ? "warn" : "danger"
);

function tone(value: string): "ok" | "warn" | "danger" | "muted" {
  if (value === "up" || value === "running") return "ok";
  if (value === "degraded" || value === "stale" || value === "starting") return "warn";
  if (value === "down" || value === "stopped" || value === "crash_loop") return "danger";
  return "muted";
}

function logTone(level: string): "ok" | "warn" | "danger" | "muted" {
  const normalized = level.toLowerCase();
  if (normalized === "error" || normalized === "critical") return "danger";
  if (normalized === "warning" || normalized === "warn") return "warn";
  if (normalized === "info") return "ok";
  return "muted";
}

function label(value: string): string {
  return value.replace(/_/g, " ");
}

function prettyName(value: string): string {
  return value
    .replace(/[_-]/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "Request failed.";
}

async function loadStatus() {
  statusError.value = "";
  try {
    status.value = await getSystemStatus();
  } catch (cause) {
    statusError.value = errorMessage(cause);
  }
}

async function loadLogs(reset: boolean) {
  if (logsLoading.value) return;
  logsLoading.value = true;
  logsError.value = "";
  if (reset) {
    logs.value = [];
    nextCursor.value = null;
  }
  try {
    const page = await getSystemLogs({
      component: logComponent.value,
      cursor: reset ? undefined : (nextCursor.value ?? undefined),
      limit: 100,
    });
    logs.value = reset ? page.items : [...logs.value, ...page.items];
    nextCursor.value = page.next_cursor;
  } catch (cause) {
    logsError.value = errorMessage(cause);
  } finally {
    logsLoading.value = false;
  }
}

async function refresh() {
  if (refreshing.value) return;
  refreshing.value = true;
  try {
    await Promise.all([loadStatus(), loadLogs(true)]);
  } finally {
    refreshing.value = false;
  }
}

async function copy(value: string) {
  await navigator.clipboard.writeText(value);
}

onMounted(refresh);
</script>

<style scoped>
.system-page {
  max-width: 1180px;
  margin: 0 auto;
  padding: 28px 22px 52px;
}
.system-page__header,
.panel__head,
.logs__head,
.summary,
.component-card header,
.recovery li,
.log-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.system-page__header {
  align-items: flex-end;
  margin-bottom: 20px;
}
h1,
h2,
p {
  margin: 0;
}
h1 {
  font-size: 25px;
}
h2 {
  font-size: 17px;
}
.eyebrow {
  margin-bottom: 4px;
  color: var(--fg-muted);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.subhead,
.muted {
  color: var(--fg-muted);
}
.subhead {
  margin-top: 4px;
}
.ghost-btn,
.load-more,
.recovery button,
.log-list button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--panel);
  color: var(--fg);
  cursor: pointer;
}
.ghost-btn {
  height: 34px;
  padding: 0 12px;
}
button:disabled {
  opacity: 0.55;
  cursor: default;
}
.spin {
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.notice {
  margin-bottom: 14px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.notice--danger {
  border-color: var(--danger);
  background: var(--danger-weak);
  color: var(--danger);
}
.summary {
  margin-bottom: 14px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: var(--radius);
  background: var(--panel);
}
.summary[data-tone="ok"] {
  border-left-color: var(--ok);
}
.summary[data-tone="warn"] {
  border-left-color: var(--warn);
}
.summary[data-tone="danger"] {
  border-left-color: var(--danger);
}
.summary div {
  display: flex;
  flex-direction: column;
}
.summary__label {
  color: var(--fg-muted);
  font-size: 12px;
}
.summary strong {
  font-size: 21px;
  text-transform: capitalize;
}
.component-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.component-card {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--border);
  border-top-width: 3px;
  border-radius: var(--radius);
  background: var(--panel);
}
.component-card[data-tone="ok"] {
  border-top-color: var(--ok);
}
.component-card[data-tone="warn"] {
  border-top-color: var(--warn);
}
.component-card[data-tone="danger"] {
  border-top-color: var(--danger);
}
.component-card dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 3px 12px;
  margin: 12px 0 0;
  font-size: 12px;
}
.component-card dt {
  color: var(--fg-muted);
}
.component-card dd {
  min-width: 0;
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.component-card__message,
.component-card__error {
  margin-top: 10px;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.component-card__message {
  color: var(--fg-muted);
}
.component-card__error {
  color: var(--danger);
  user-select: text;
}
.status-chip {
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--fg-muted);
  font-size: 11px;
  text-transform: uppercase;
}
.status-chip[data-tone="ok"] {
  background: var(--ok-weak);
  color: var(--ok);
}
.status-chip[data-tone="warn"] {
  background: var(--warn-weak);
  color: var(--warn);
}
.status-chip[data-tone="danger"] {
  background: var(--danger-weak);
  color: var(--danger);
}
.panel {
  margin-top: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--panel);
}
.panel__head {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}
.recovery ol,
.log-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.recovery li {
  justify-content: flex-start;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
}
.recovery li:last-child {
  border-bottom: 0;
}
.recovery code {
  flex: 1;
  min-width: 0;
  font-family: var(--mono);
  overflow-wrap: anywhere;
  user-select: all;
}
.recovery button,
.log-list button {
  flex: none;
  height: 28px;
  padding: 0 8px;
}
.logs__head label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fg-muted);
  font-size: 12px;
}
.logs__head select {
  height: 32px;
  padding: 0 28px 0 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--fg);
}
.logs__empty {
  padding: 28px 16px;
  color: var(--fg-muted);
  text-align: center;
}
.log-list li {
  display: grid;
  grid-template-columns: 160px 100px 68px minmax(0, 1fr) 32px;
  justify-content: initial;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.log-list time,
.log-list__component {
  color: var(--fg-muted);
}
.log-list__component {
  font-family: var(--mono);
}
.log-list pre {
  min-width: 0;
  margin: 0;
  font: inherit;
  font-family: var(--mono);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  user-select: text;
}
.log-list button {
  width: 28px;
  padding: 0;
}
.load-more {
  margin: 12px auto;
  height: 34px;
  padding: 0 14px;
}
@media (max-width: 850px) {
  .component-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .log-list li {
    grid-template-columns: 1fr auto auto;
  }
  .log-list time,
  .log-list__component {
    grid-column: span 1;
  }
  .log-list pre {
    grid-column: 1 / -2;
  }
}
@media (max-width: 560px) {
  .system-page__header {
    align-items: flex-start;
  }
  .component-grid {
    grid-template-columns: 1fr;
  }
  .summary {
    align-items: flex-start;
    flex-direction: column;
  }
  .logs__head {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
