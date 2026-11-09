<template>
  <div class="storage">
    <header class="storage__header">
      <div>
        <p class="eyebrow">本地数据</p>
        <h1>Storage 存储清理</h1>
        <p class="subhead">
          清理各 agent 的旧会话数据来释放磁盘空间。保留窗口内的文件永不删除，每次清理都会写入审计日志，
          实际删除由本机 Supervisor 执行。
        </p>
      </div>
      <div class="storage__actions">
        <span
          class="data-age muted mono"
          :title="
            overview
              ? `服务端扫描时间 ${formatTime(overview.generated_at)}；清理开始或结束时会立即重新扫描`
              : ''
          "
        >
          数据时间 {{ lastFetchedLabel }}<template v-if="overview?.cached"> · 服务端缓存</template>
        </span>
        <button class="ghost-btn" type="button" :disabled="loading" @click="refresh(true)">
          <RefreshCw :size="15" :class="{ spin: loading }" />
          刷新
        </button>
      </div>
    </header>

    <p v-if="error" class="notice notice--danger" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice notice--ok" role="status">{{ notice }}</p>
    <p v-if="overview && !overview.enabled" class="notice notice--warn" role="status">
      本实例已停用清理（<code class="mono">AGENT_OBS_RETENTION_ENABLED=false</code>），预览仍然可用。
    </p>

    <section class="cards" aria-label="存储概览">
      <article class="stat">
        <span class="stat__label">Agent 数据占用</span>
        <strong>{{ formatBytes(totalBytes) }}</strong>
        <span class="muted">{{ totalFiles.toLocaleString() }} 个文件（已纳入清单的目录）</span>
      </article>
      <article class="stat">
        <span class="stat__label">观测库索引</span>
        <strong>{{ overview?.index.database_pretty ?? "—" }}</strong>
        <span class="muted">
          PostgreSQL 中对应的行会一并清理；删除后数据库文件不会立刻变小，需 VACUUM 回收
        </span>
      </article>
      <article class="stat">
        <span class="stat__label">保护窗口</span>
        <strong>{{ overview?.min_hours ?? 24 }} 小时</strong>
        <span class="muted">
          {{
            overview?.protected_roots.length
              ? `${overview.protected_roots.length} 个受保护目录`
              : "未额外锁定目录"
          }}
        </span>
      </article>
    </section>

    <section class="card" aria-labelledby="selection-heading">
      <div class="card__head">
        <div>
          <h2 id="selection-heading">① 选择要清理的位置</h2>
          <p class="muted">每个勾选的位置只保留最近 N 天，更早的文件才会进入清理计划。</p>
        </div>
        <div class="window">
          <label for="keep-days">保留最近</label>
          <select id="keep-days" v-model.number="keepDays">
            <option v-for="option in windowOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </div>
      </div>

      <p class="hint">
        「清空勾选」只会取消下面的复选框，不会删除任何数据；真正的删除在第 ③ 步。
      </p>

      <div v-if="loading && !entries.length" class="empty-state">正在扫描本地数据目录…</div>
      <div v-else-if="!entries.length" class="empty-state">没有发现可清理的数据目录。</div>
      <div v-else class="agents">
        <div v-for="group in groupedEntries" :key="group.agent" class="agent">
          <header class="agent__head">
            <strong>{{ agentLabel(group.agent) }}</strong>
            <span class="muted">{{ formatBytes(group.bytes) }}</span>
          </header>
          <label v-for="entry in group.entries" :key="entry.id" class="entry">
            <input
              type="checkbox"
              :checked="selected[entry.id] === true"
              @change="toggle(entry.id, ($event.target as HTMLInputElement).checked)"
            />
            <span class="entry__body">
              <span class="entry__title">
                {{ entry.label }}
                <span class="chip" :data-tone="tone(entry.risk)">{{ riskLabel(entry.risk) }}</span>
                <span v-if="entry.protected" class="chip" data-tone="warn">已锁定</span>
                <span v-else-if="entry.status !== 'ok'" class="chip" data-tone="muted">
                  {{ statusLabel(entry.status) }}
                </span>
              </span>
              <span class="entry__meta mono">{{ entry.root }}</span>
              <span class="entry__meta">
                {{ formatBytes(entry.bytes) }} · {{ entry.files.toLocaleString() }} 个文件
                <template v-if="entry.newest_mtime"> · 最新 {{ formatTime(entry.newest_mtime) }}</template>
              </span>
            </span>
          </label>
        </div>
      </div>

      <div class="options">
        <label class="toggle">
          <input v-model="includeRaw" type="checkbox" />
          删除磁盘上的文件
        </label>
        <label class="toggle">
          <input v-model="includeIndex" type="checkbox" />
          同时清理观测库中对应的索引行
        </label>
        <div class="options__actions">
          <button class="ghost-btn" type="button" :disabled="loading || !hasSelection" @click="selectAll(true)">
            全选
          </button>
          <button class="ghost-btn" type="button" :disabled="loading || !hasSelection" @click="selectAll(false)">
            清空勾选
          </button>
          <button class="primary-btn" type="button" :disabled="previewing || !hasSelection" @click="runPreview">
            {{ previewing ? "计算中…" : "② 预览清理（只读）" }}
          </button>
        </div>
      </div>
    </section>

    <section v-if="preview" ref="previewCard" class="card card--preview" aria-labelledby="preview-heading">
      <div class="card__head">
        <div>
          <h2 id="preview-heading">③ 确认并执行</h2>
          <p class="muted">
            删除早于 {{ formatTime(preview.floor_at) }} 的数据 · 保留 {{ preview.keep_days }} 天
          </p>
        </div>
        <div class="preview__total">
          <strong>{{ formatBytes(preview.totals.bytes) }}</strong>
          <span class="muted">
            {{ preview.totals.files.toLocaleString() }} 个文件 ·
            {{ preview.totals.sessions.toLocaleString() }} 条会话索引 ·
            {{ preview.totals.sources.toLocaleString() }} 个数据源
          </span>
        </div>
      </div>

      <div v-if="preview.totals.files === 0 && preview.totals.sessions === 0" class="empty-state">
        当前选择下没有超过保留窗口的数据，无需清理。
      </div>

      <div v-else class="preview__table">
        <table>
          <thead>
            <tr>
              <th>位置</th>
              <th>Agent</th>
              <th class="num">文件</th>
              <th class="num">占用</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in plannedEntries" :key="entry.id">
              <td>{{ entry.label }}</td>
              <td>{{ agentLabel(entry.agent) }}</td>
              <td class="num">{{ entry.files.toLocaleString() }}</td>
              <td class="num">{{ formatBytes(entry.bytes) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <ul v-if="preview.skipped.length" class="skipped">
        <li v-for="row in preview.skipped" :key="`${row.entry_id}-${row.reason}`">
          <strong>{{ row.entry_id }}</strong> — {{ skipReasonLabel(row.reason) }}
          <span class="muted">（{{ row.files.toLocaleString() }} 个文件，{{ formatBytes(row.bytes) }}）</span>
        </li>
      </ul>

      <p v-if="preview.items_truncated" class="muted">
        列表只展示前 500 条路径，实际执行使用服务端完整计划。
      </p>

      <div class="confirm">
        <label v-if="needsAcknowledgement" class="toggle toggle--danger">
          <input v-model="acknowledged" type="checkbox" />
          本次超过 {{ formatBytes(preview.large_run_bytes) }}，我确认继续
        </label>
        <p v-if="!auth.isAdmin" class="muted">只有 admin 可以发起清理。</p>
        <p v-else class="muted">删除不可撤销，请确认上方数字无误。</p>
        <button
          v-if="auth.isAdmin"
          class="danger-btn"
          type="button"
          :disabled="!canDelete"
          @click="startRun"
        >
          <Trash2 :size="15" />
          {{ deleting ? "提交中…" : `立即清理 ${formatBytes(preview.totals.bytes)}` }}
        </button>
      </div>
    </section>

    <section v-if="activeRun" ref="activeCard" class="card" aria-labelledby="active-heading">
      <div class="card__head">
        <div>
          <h2 id="active-heading">清理任务</h2>
          <p class="muted mono">{{ activeRun.id }}</p>
        </div>
        <span class="status-chip" :data-tone="runTone(activeRun.status)">{{ runStatusLabel(activeRun.status) }}</span>
      </div>
      <dl class="run-facts">
        <dt>系统命令</dt>
        <dd class="mono">#{{ activeRun.command_id ?? "—" }}</dd>
        <dt>已释放</dt>
        <dd>{{ formatBytes(activeRun.bytes_freed) }}</dd>
        <dt>已删除文件</dt>
        <dd>{{ activeRun.files_deleted.toLocaleString() }}</dd>
        <dt>已清理索引会话</dt>
        <dd>{{ activeRun.sessions_deleted.toLocaleString() }}</dd>
        <dt v-if="activeRun.finished_at">完成时间</dt>
        <dd v-if="activeRun.finished_at">{{ formatTime(activeRun.finished_at) }}</dd>
      </dl>
      <p v-if="activeRun.error_message" class="notice notice--danger">{{ activeRun.error_message }}</p>
      <p v-if="isRunActive" class="muted" role="status">
        已写入 system_commands，等待本机 Supervisor 执行… 已运行 {{ elapsedSeconds }} 秒，
        页面每 2 秒自动检查一次，完成后自动刷新占用数字。
      </p>
    </section>

    <section class="card" aria-labelledby="history-heading">
      <div class="card__head">
        <h2 id="history-heading">最近的清理记录</h2>
        <button class="ghost-btn" type="button" :disabled="loading" @click="loadRuns">刷新</button>
      </div>
      <div v-if="!runs.length" class="empty-state">还没有执行过清理。</div>
      <div v-else class="preview__table">
        <table>
          <thead>
            <tr>
              <th>发起时间</th>
              <th>发起人</th>
              <th>范围</th>
              <th>保留</th>
              <th class="num">释放</th>
              <th class="num">文件</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="run in runs" :key="run.id">
              <td>{{ run.created_at ? formatTime(run.created_at) : "—" }}</td>
              <td class="mono">{{ run.requested_by }}</td>
              <td>{{ scopeLabel(run.scope) }}</td>
              <td>{{ run.keep_days }} 天</td>
              <td class="num">{{ formatBytes(run.bytes_freed) }}</td>
              <td class="num">{{ run.files_deleted.toLocaleString() }}</td>
              <td>
                <span class="status-chip" :data-tone="runTone(run.status)">{{ runStatusLabel(run.status) }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";
import { RefreshCw, Trash2 } from "@lucide/vue";
import { useAuthStore } from "../stores/auth";
import { agentLabel } from "../constants/agents";
import {
  createRetentionRun,
  formatBytes,
  getRetentionOverview,
  getRetentionRun,
  listRetentionRuns,
  previewRetention,
  riskLabel,
  skipReasonLabel,
  type RetentionEntryUsage,
  type RetentionOverview,
  type RetentionPlan,
  type RetentionRisk,
  type RetentionRun,
} from "../api/retention";

const auth = useAuthStore();

const loading = ref(false);
const previewing = ref(false);
const deleting = ref(false);
const error = ref("");
const notice = ref("");
const overview = ref<RetentionOverview | null>(null);
const preview = ref<RetentionPlan | null>(null);
const runs = ref<RetentionRun[]>([]);
const activeRun = ref<RetentionRun | null>(null);
const keepDays = ref(7);
const includeRaw = ref(true);
const includeIndex = ref(true);
const acknowledged = ref(false);
const selected = ref<Record<string, boolean>>({});
const lastFetchedAt = ref<Date | null>(null);
const clock = ref(Date.now());
const previewCard = ref<HTMLElement | null>(null);
const activeCard = ref<HTMLElement | null>(null);
let pollTimer: number | undefined;
let pollTicks = 0;

const windowOptions = [
  { value: 1, label: "1 天（下限）" },
  { value: 3, label: "3 天" },
  { value: 7, label: "7 天" },
  { value: 14, label: "14 天" },
  { value: 30, label: "30 天" },
  { value: 90, label: "90 天" },
];

const entries = computed<RetentionEntryUsage[]>(() => overview.value?.entries ?? []);
const totalBytes = computed(() => entries.value.reduce((sum, entry) => sum + entry.bytes, 0));
const totalFiles = computed(() => entries.value.reduce((sum, entry) => sum + entry.files, 0));
const hasSelection = computed(() => entries.value.some((entry) => selected.value[entry.id]));

const lastFetchedLabel = computed(() =>
  lastFetchedAt.value ? lastFetchedAt.value.toLocaleTimeString() : "—"
);

const isRunActive = computed(() =>
  ["pending", "running"].includes(activeRun.value?.status ?? "")
);

const elapsedSeconds = computed(() => {
  const started = activeRun.value?.started_at ?? activeRun.value?.created_at;
  if (!started) return 0;
  const startedAt = new Date(started).valueOf();
  if (Number.isNaN(startedAt)) return 0;
  return Math.max(0, Math.round((clock.value - startedAt) / 1000));
});

const groupedEntries = computed(() => {
  const groups = new Map<string, { agent: string; bytes: number; entries: RetentionEntryUsage[] }>();
  for (const entry of entries.value) {
    const group = groups.get(entry.agent) ?? { agent: entry.agent, bytes: 0, entries: [] };
    group.entries.push(entry);
    group.bytes += entry.bytes;
    groups.set(entry.agent, group);
  }
  return [...groups.values()].sort((a, b) => b.bytes - a.bytes);
});

const plannedEntries = computed(() =>
  (preview.value?.entries ?? [])
    .filter((entry) => entry.files > 0)
    .sort((a, b) => b.bytes - a.bytes)
);

const needsAcknowledgement = computed(() => {
  const plan = preview.value;
  if (!plan) return false;
  return plan.totals.bytes > plan.large_run_bytes;
});

const canDelete = computed(() => {
  if (!auth.isAdmin || !preview.value || deleting.value) return false;
  if (preview.value.totals.files === 0 && preview.value.totals.sessions === 0) return false;
  if (needsAcknowledgement.value && !acknowledged.value) return false;
  return true;
});

function tone(risk: RetentionRisk): string {
  if (risk === "regenerable") return "ok";
  if (risk === "history") return "warn";
  return "danger";
}

function runTone(status: string): string {
  if (status === "applied") return "ok";
  if (status === "failed" || status === "refused") return "danger";
  if (status === "running") return "warn";
  return "muted";
}

function runStatusLabel(status: string): string {
  switch (status) {
    case "pending":
      return "等待执行";
    case "running":
      return "执行中";
    case "applied":
      return "已完成";
    case "failed":
      return "部分失败";
    case "refused":
      return "已拒绝";
    default:
      return status;
  }
}

function scopeLabel(scope: string): string {
  switch (scope) {
    case "raw+index":
      return "文件 + 索引";
    case "raw":
      return "仅文件";
    case "index":
      return "仅索引";
    default:
      return scope;
  }
}

function statusLabel(status: string): string {
  switch (status) {
    case "ok":
      return "正常";
    case "missing":
      return "目录不存在";
    case "protected":
      return "已锁定";
    default:
      return status;
  }
}

function formatTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const date = typeof value === "number" ? new Date(value * (value < 1e12 ? 1000 : 1)) : new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
}

function toggle(entryId: string, checked: boolean) {
  selected.value = { ...selected.value, [entryId]: checked };
  preview.value = null;
  notice.value = "";
}

function selectAll(checked: boolean) {
  const next: Record<string, boolean> = {};
  for (const entry of entries.value) next[entry.id] = checked;
  selected.value = next;
  preview.value = null;
  notice.value = "";
}

async function refresh(force = false) {
  if (loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    overview.value = await getRetentionOverview(force);
    lastFetchedAt.value = new Date();
    if (!hasSelection.value) selectDefaults();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "加载存储占用失败。";
  } finally {
    loading.value = false;
  }
}

function selectDefaults() {
  const next: Record<string, boolean> = { ...selected.value };
  for (const entry of entries.value) {
    if (next[entry.id] === undefined) next[entry.id] = entry.category === "sessions";
  }
  selected.value = next;
}

async function loadRuns() {
  try {
    runs.value = (await listRetentionRuns(10)).runs;
    if (activeRun.value) {
      const match = runs.value.find((run) => run.id === activeRun.value?.id);
      if (match) activeRun.value = match;
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "加载清理记录失败。";
  }
}

async function runPreview() {
  if (previewing.value) return;
  previewing.value = true;
  error.value = "";
  notice.value = "";
  acknowledged.value = false;
  try {
    preview.value = await previewRetention({
      keep_days: keepDays.value,
      entry_ids: entries.value.filter((entry) => selected.value[entry.id]).map((entry) => entry.id),
      include_raw: includeRaw.value,
      include_index: includeIndex.value,
    });
    await nextTick();
    previewCard.value?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "预览失败。";
    preview.value = null;
  } finally {
    previewing.value = false;
  }
}

async function startRun() {
  const plan = preview.value;
  if (!plan || !canDelete.value) return;
  deleting.value = true;
  error.value = "";
  try {
    const created = await createRetentionRun({
      plan_token: plan.plan_token,
      confirm: true,
      acknowledge_large: acknowledged.value,
    });
    activeRun.value = created.run;
    notice.value = `已排入系统命令 #${created.command_id}，Supervisor 会立即执行。`;
    preview.value = null;
    await nextTick();
    activeCard.value?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    schedulePoll();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "提交清理失败。";
  } finally {
    deleting.value = false;
  }
}

function schedulePoll() {
  stopPoll();
  pollTicks = 0;
  pollTimer = window.setInterval(() => {
    void pollActiveRun();
  }, 2000);
}

function stopPoll() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

async function pollActiveRun() {
  const run = activeRun.value;
  if (!run) {
    stopPoll();
    return;
  }
  try {
    const updated = await getRetentionRun(run.id);
    activeRun.value = updated;
    clock.value = Date.now();
    pollTicks += 1;
    if (["applied", "failed", "refused"].includes(updated.status)) {
      stopPoll();
      notice.value = `清理${runStatusLabel(updated.status)}：释放 ${formatBytes(updated.bytes_freed)}，删除 ${updated.files_deleted.toLocaleString()} 个文件。占用数字已自动更新。`;
      await Promise.all([refresh(true), loadRuns()]);
    } else if (pollTicks % 8 === 0) {
      // A purge can run for a minute: re-scan every ~16s so the sizes on the
      // page shrink while the files disappear, instead of looking frozen.
      await refresh(true);
    }
  } catch {
    // 轮询失败不致命：下一次 tick 会重试。
  }
}

async function refreshAfterFocus() {
  if (document.visibilityState === "hidden") return;
  if (isRunActive.value) {
    // Background tabs throttle timers: re-align the poll and read once now.
    schedulePoll();
    await pollActiveRun();
    return;
  }
  // The server re-scans as soon as a run changed, so a normal fetch is enough.
  await refresh();
}

onMounted(async () => {
  await refresh();
  await loadRuns();
  const pending = runs.value.find((run) => run.status === "pending" || run.status === "running");
  if (pending) {
    activeRun.value = pending;
    schedulePoll();
  }
  document.addEventListener("visibilitychange", refreshAfterFocus);
  window.addEventListener("focus", refreshAfterFocus);
});

onUnmounted(() => {
  stopPoll();
  document.removeEventListener("visibilitychange", refreshAfterFocus);
  window.removeEventListener("focus", refreshAfterFocus);
});
</script>

<style scoped>
.storage {
  max-width: 1180px;
  margin: 0 auto;
  padding: 28px 22px 52px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.storage__header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
}
.storage__actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.data-age {
  font-size: 12px;
}
h1 {
  font-size: 25px;
  margin: 0;
}
h2 {
  font-size: 17px;
  margin: 0;
}
p {
  margin: 0;
}
.eyebrow {
  margin-bottom: 4px;
  color: var(--fg-muted);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.subhead {
  margin-top: 6px;
  max-width: 70ch;
}
.subhead,
.muted {
  color: var(--fg-muted);
}
.mono {
  font-family: var(--mono);
}
.hint {
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  padding: 8px 10px;
  color: var(--fg-muted);
  font-size: 12px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.card__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.stat {
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stat__label {
  color: var(--fg-muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.stat strong {
  font-size: 22px;
}
.agents {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
}
.agent {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--panel-2);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.agent__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.entry {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 8px;
  border-radius: var(--radius);
  cursor: pointer;
}
.entry:hover {
  background: var(--panel-3);
}
.entry input {
  margin-top: 3px;
}
.entry__body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.entry__title {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.entry__meta {
  color: var(--fg-muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.chip,
.status-chip {
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 11px;
  color: var(--fg-muted);
  white-space: nowrap;
}
.chip[data-tone="ok"],
.status-chip[data-tone="ok"] {
  color: var(--ok);
  border-color: var(--ok);
  background: var(--ok-weak);
}
.chip[data-tone="warn"],
.status-chip[data-tone="warn"] {
  color: var(--warn);
  border-color: var(--warn);
  background: var(--warn-weak);
}
.chip[data-tone="danger"],
.status-chip[data-tone="danger"] {
  color: var(--danger);
  border-color: var(--danger);
  background: var(--danger-weak);
}
.options {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.options__actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--fg-muted);
}
.toggle--danger {
  color: var(--danger);
}
.window {
  display: flex;
  align-items: center;
  gap: 8px;
}
select {
  background: var(--panel-2);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
}
.card--preview {
  border-color: var(--border-strong);
}
.preview__total {
  text-align: right;
  display: flex;
  flex-direction: column;
}
.preview__total strong {
  font-size: 22px;
}
.preview__table {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  text-align: left;
  padding: 7px 8px;
  border-bottom: 1px solid var(--border);
}
th {
  color: var(--fg-muted);
  font-weight: 500;
}
.num {
  text-align: right;
}
.skipped {
  margin: 0;
  padding-left: 18px;
  color: var(--fg-muted);
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.confirm {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.run-facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 6px 14px;
  margin: 0;
}
.run-facts dt {
  color: var(--fg-muted);
  font-size: 12px;
}
.run-facts dd {
  margin: 0;
}
.primary-btn,
.ghost-btn,
.danger-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 7px;
  padding: 7px 12px;
  cursor: pointer;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--fg);
}
.primary-btn {
  background: var(--accent);
  border-color: var(--accent);
  color: #0b0d0c;
  font-weight: 600;
}
.danger-btn {
  background: var(--danger);
  border-color: var(--danger);
  color: #150808;
  font-weight: 600;
}
.primary-btn:disabled,
.ghost-btn:disabled,
.danger-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.notice {
  border-radius: var(--radius);
  padding: 10px 12px;
  border: 1px solid var(--border);
}
.notice--danger {
  border-color: var(--danger);
  background: var(--danger-weak);
  color: var(--danger);
}
.notice--ok {
  border-color: var(--ok);
  background: var(--ok-weak);
  color: var(--ok);
}
.notice--warn {
  border-color: var(--warn);
  background: var(--warn-weak);
  color: var(--warn);
}
.empty-state {
  color: var(--fg-muted);
  padding: 12px 0;
}
.spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
