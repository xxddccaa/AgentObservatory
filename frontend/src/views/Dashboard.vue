<template>
  <div class="dash">
    <header class="dash__header">
      <div>
        <p class="eyebrow">Workspace overview</p>
        <h1>Dashboard</h1>
        <p class="dash__intro">Monitor indexed history and system activity.</p>
      </div>
      <button class="icon-btn" title="Refresh dashboard" aria-label="Refresh dashboard" @click="loadAll">
        <RefreshCw :size="16" />
      </button>
    </header>

    <div class="tiles">
      <router-link class="tile" to="/history">
        <span class="tile__icon" data-tone="ok"><HistoryIcon :size="17" /></span>
        <span class="tile__copy">
          <span class="tile__label">History indexed</span>
          <span class="tile__sub">Agent sessions</span>
        </span>
        <span class="tile__value">{{ history.length }}</span>
      </router-link>
      <router-link class="tile" to="/audit">
        <span class="tile__icon" data-tone="warn"><ShieldCheck :size="17" /></span>
        <span class="tile__copy">
          <span class="tile__label">Recent activity</span>
          <span class="tile__sub">Audit events loaded</span>
        </span>
        <span class="tile__value">{{ audit.length }}</span>
      </router-link>
      <router-link v-if="auth.isAdmin" class="tile" to="/system">
        <span class="tile__icon" :data-tone="health.online ? 'ok' : 'danger'"><Activity :size="17" /></span>
        <span class="tile__copy">
          <span class="tile__label">System health</span>
          <span class="tile__sub">{{ healthyServices }}/2 services online</span>
        </span>
        <span class="tile__status" :data-tone="health.online ? 'ok' : 'danger'">
          {{ health.label }}
        </span>
      </router-link>
      <div v-else class="tile">
        <span class="tile__icon" :data-tone="health.online ? 'ok' : 'danger'"><Activity :size="17" /></span>
        <span class="tile__copy">
          <span class="tile__label">System health</span>
          <span class="tile__sub">{{ healthyServices }}/2 services online</span>
        </span>
        <span class="tile__status" :data-tone="health.online ? 'ok' : 'danger'">{{ health.label }}</span>
      </div>
    </div>

    <section class="panel">
      <header class="panel__head">
        <div>
          <p class="panel__eyebrow">Indexed records</p>
          <h2>Recent history</h2>
        </div>
        <router-link class="panel__more" to="/history" aria-label="View all history">
          View all <ArrowRight :size="14" />
        </router-link>
      </header>
      <div v-if="loading.history" class="panel__empty">Loading…</div>
      <div v-else-if="errors.history" class="panel__empty error">{{ errors.history }}</div>
      <div v-else-if="history.length === 0" class="panel__empty">No indexed Agent sessions yet.</div>
      <ul v-else class="rows history-rows">
        <li v-for="h in history.slice(0, 6)" :key="h.id">
          <router-link class="row row--history" :to="`/history/${h.id}`">
            <div class="row__main">
              <strong class="history-title" :title="sessionTitle(h)">{{ sessionTitle(h) }}</strong>
              <span class="muted mono workspace-path" :title="h.cwd || 'Unknown workspace'">{{ h.cwd || "Unknown workspace" }}</span>
              <span class="muted mono history-id" :title="h.thread_id">id: {{ h.thread_id.slice(0, 18) }}</span>
            </div>
            <div class="row__meta">
              <span class="muted">{{ h.model || "model?" }}</span>
              <span class="muted">{{ h.turn_count }} turns</span>
              <span class="badge" :data-tone="h.index_status === 'indexed' ? 'ok' : 'muted'">
                {{ h.index_status }}
              </span>
            </div>
          </router-link>
        </li>
      </ul>
    </section>

    <section class="panel">
      <header class="panel__head">
        <div>
          <p class="panel__eyebrow">Security trail</p>
          <h2>Recent audit</h2>
        </div>
        <router-link class="panel__more" to="/audit" aria-label="View all audit events">
          View all <ArrowRight :size="14" />
        </router-link>
      </header>
      <div v-if="loading.audit" class="panel__empty">Loading…</div>
      <div v-else-if="errors.audit" class="panel__empty error">{{ errors.audit }}</div>
      <div v-else-if="audit.length === 0" class="panel__empty">No audit events yet.</div>
      <ul v-else class="audit-rows">
        <li v-for="a in audit.slice(0, 8)" :key="a.id" class="audit-row">
          <span class="badge" :data-tone="a.result === 'success' ? 'ok' : 'danger'">{{ a.result }}</span>
          <strong>{{ a.action }}</strong>
          <span class="muted">{{ a.actor_id || a.actor_type }}</span>
          <time>{{ formatTime(a.created_at) }}</time>
        </li>
      </ul>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import {
  Activity,
  ArrowRight,
  History as HistoryIcon,
  RefreshCw,
  ShieldCheck,
} from "@lucide/vue";
import { useAuthStore } from "../stores/auth";
import { listHistory, sessionTitle, type HistorySession } from "../api/history";
import { listAudit, type AuditItem } from "../api/audit";
import { getReadyHealth } from "../api/health";

const auth = useAuthStore();

const history = ref<HistorySession[]>([]);
const audit = ref<AuditItem[]>([]);
const health = reactive({
  online: false,
  label: "…",
  pg: false,
  indexer: false,
});

const loading = reactive({ history: true, audit: true });
const errors = reactive<{ history: string; audit: string }>({
  history: "",
  audit: "",
});

const healthyServices = computed(
  () => [health.pg, health.indexer].filter(Boolean).length
);

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "";
}

async function loadAll() {
  loading.history = loading.audit = true;
  errors.history = errors.audit = "";

  void listHistory()
    .then((r) => (history.value = r.sessions))
    .catch((e: unknown) => (errors.history = message(e)))
    .finally(() => (loading.history = false));

  void listAudit({ limit: 100 })
    .then((r) => (audit.value = r.items))
    .catch((e: unknown) => (errors.audit = message(e)))
    .finally(() => (loading.audit = false));

  void getReadyHealth()
    .then((h) => {
      health.online = h.status === "ok";
      health.label = h.status;
      health.pg = h.components.postgresql?.status === "up";
      health.indexer = h.components.indexer?.status === "up";
    })
    .catch(() => {
      health.online = false;
      health.label = "offline";
      health.pg = false;
      health.indexer = false;
    });
}

function message(cause: unknown): string {
  return cause instanceof Error ? cause.message : "Failed to load";
}

onMounted(loadAll);
</script>

<style scoped>
.dash {
  max-width: 1240px;
  margin: 0 auto;
  padding: 34px 30px 52px;
}
.dash__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 26px;
}
.eyebrow {
  margin: 0 0 4px;
  color: var(--accent-strong);
  font-size: 10px;
  font-weight: 650;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  font-size: 27px;
  font-weight: 650;
}
.dash__intro {
  margin: 5px 0 0;
  color: var(--fg-muted);
  font-size: 13px;
}
.icon-btn {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel);
  color: var(--fg-muted);
  cursor: pointer;
}
.icon-btn:hover {
  border-color: var(--border-strong);
  color: var(--fg);
}

.tiles {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.tile {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 11px;
  min-height: 76px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--panel);
  color: inherit;
  text-decoration: none;
  transition: border-color 120ms ease, background-color 120ms ease;
}
.tile:hover {
  border-color: var(--border-strong);
  background: var(--panel-2);
}
.tile__icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 6px;
  background: var(--accent-weak);
  color: var(--accent-strong);
}
.tile__icon[data-tone="ok"] {
  background: var(--ok-weak);
  color: var(--ok);
}
.tile__icon[data-tone="warn"] {
  background: var(--warn-weak);
  color: var(--warn);
}
.tile__icon[data-tone="danger"] {
  background: var(--danger-weak);
  color: var(--danger);
}
.tile__copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.tile__label {
  color: var(--fg);
  font-size: 13px;
  font-weight: 600;
}
.tile__value {
  font-size: 24px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.tile__sub {
  overflow: hidden;
  color: var(--fg-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tile__status {
  padding: 3px 7px;
  border-radius: 4px;
  background: var(--panel-3);
  color: var(--fg-muted);
  font-size: 10px;
  font-weight: 650;
  text-transform: uppercase;
}
.tile__status[data-tone="ok"] {
  background: var(--ok-weak);
  color: var(--ok);
}
.tile__status[data-tone="danger"] {
  background: var(--danger-weak);
  color: var(--danger);
}

.panel {
  min-width: 0;
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--panel);
  overflow: hidden;
}
.panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 58px;
  padding: 12px 15px;
  border-bottom: 1px solid var(--border);
}
.panel__head h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
}
.panel__eyebrow {
  margin: 0 0 1px;
  color: var(--fg-subtle);
  font-size: 9px;
  font-weight: 650;
  text-transform: uppercase;
}
.panel__more {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--fg-muted);
  text-decoration: none;
}
.panel__more:hover {
  color: var(--accent);
}
.panel__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  min-height: 190px;
  justify-content: center;
  padding: 30px 16px;
  color: var(--fg-muted);
  text-align: center;
}
.panel__empty.error {
  color: var(--danger);
}
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  min-height: 61px;
  padding: 10px 15px;
  border-bottom: 1px solid var(--border);
  color: inherit;
  text-decoration: none;
}
.rows li:last-child .row {
  border-bottom: 0;
}
.row:hover {
  background: var(--panel-2);
}
.row__main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  flex: 1 1 auto;
  min-width: 0;
}
.row__main strong {
  font-size: 13px;
}
.row__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 1 auto;
  justify-content: flex-end;
  font-size: 12px;
  white-space: nowrap;
}
.history-rows .row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, auto);
  min-width: 0;
}
.history-rows .row__meta {
  min-width: 0;
  max-width: 100%;
  flex-wrap: wrap;
}
.history-rows .muted {
  max-width: 100%;
}
.workspace-path {
  overflow-wrap: anywhere;
  white-space: normal;
}
.history-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.history-id {
  font-size: 11px;
}
.muted {
  color: var(--fg-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 340px;
}
.mono {
  font-family: var(--mono);
}
.badge {
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 11px;
  background: var(--panel);
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
.audit-rows {
  list-style: none;
  margin: 0;
  padding: 0;
}
.audit-row {
  display: grid;
  grid-template-columns: 72px auto 1fr auto;
  align-items: center;
  gap: 10px;
  min-height: 46px;
  padding: 8px 15px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.audit-row:last-child {
  border-bottom: 0;
}
.audit-row:hover {
  background: var(--panel-2);
}
.audit-row time {
  color: var(--fg-muted);
  font-size: 12px;
}
@media (max-width: 860px) {
  .tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .audit-row {
    grid-template-columns: 68px 1fr auto;
  }
  .audit-row .muted {
    display: none;
  }
  .history-rows .row {
    grid-template-columns: minmax(0, 1fr);
  }
  .history-rows .row__meta {
    justify-content: flex-start;
  }
  .history-rows .row__meta {
    overflow: hidden;
  }
}
@media (max-width: 600px) {
  .dash {
    padding: 24px 14px 36px;
  }
  .dash__header {
    margin-bottom: 20px;
  }
  .dash__intro {
    max-width: 260px;
  }
  .tiles {
    grid-template-columns: 1fr;
  }
  .tile {
    min-height: 68px;
  }
  .audit-row {
    grid-template-columns: 66px minmax(0, 1fr);
  }
  .audit-row time {
    grid-column: 2;
  }
}
</style>
