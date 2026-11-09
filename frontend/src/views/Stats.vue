<template>
  <main class="stats-page" aria-labelledby="stats-title">
    <header class="page-header">
      <div>
        <p class="eyebrow">Indexed agent history</p>
        <h1 id="stats-title">Token usage</h1>
      </div>
      <button class="refresh" type="button" title="Refresh token usage" :disabled="loading" @click="load">
        <RefreshCw :size="16" :class="{ spinning: loading }" aria-hidden="true" />
        Refresh
      </button>
    </header>

    <div class="range-tabs" role="tablist" aria-label="Token usage range">
      <button
        v-for="item in ranges"
        :key="item.value"
        class="range-tab"
        :class="{ active: selectedRange === item.value }"
        type="button"
        role="tab"
        :aria-selected="selectedRange === item.value"
        @click="selectRange(item.value)"
      >
        {{ item.label }}
      </button>
    </div>

    <p v-if="error" class="state error" role="alert">{{ error }}</p>
    <p v-else-if="loading && !usage" class="state" role="status">Loading token usage…</p>
    <template v-else-if="usage">
      <p class="window" aria-live="polite">{{ formatWindow(usage.since, usage.until) }}</p>
      <section class="metrics" aria-label="Token totals">
        <article v-for="metric in metrics" :key="metric.key" class="metric">
          <span class="metric-label">{{ metric.label }}</span>
          <strong class="metric-value">{{ formatNumber(usage.totals[metric.key]) }}</strong>
        </article>
      </section>
      <dl class="counts" aria-label="Session counts">
        <div><dt>Sessions</dt><dd>{{ formatNumber(usage.totals.session_count) }}</dd></div>
        <div><dt>Turns</dt><dd>{{ formatNumber(usage.totals.turn_count) }}</dd></div>
      </dl>
    </template>
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { RefreshCw } from "@lucide/vue";
import { getTokenUsage, type StatsRange, type TokenUsageResponse } from "../api/stats";

const ranges: ReadonlyArray<{ value: StatsRange; label: string }> = [
  { value: "day", label: "Last 24 hours" },
  { value: "week", label: "Last 7 days" },
  { value: "month", label: "Last 30 days" },
];
const metrics = [
  { key: "input_tokens", label: "Input tokens" },
  { key: "cached_input_tokens", label: "Cached input" },
  { key: "output_tokens", label: "Output tokens" },
  { key: "reasoning_output_tokens", label: "Reasoning output" },
  { key: "total_tokens", label: "Total tokens" },
] as const;

const selectedRange = ref<StatsRange>("day");
const usage = ref<TokenUsageResponse | null>(null);
const loading = ref(false);
const error = ref("");

function formatNumber(value: number): string {
  return (value ?? 0).toLocaleString();
}

function formatWindow(since: string, until: string): string {
  const start = new Date(since).toLocaleString();
  const end = new Date(until).toLocaleString();
  return `${start} – ${end} (UTC range)`;
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    usage.value = await getTokenUsage(selectedRange.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load token usage";
  } finally {
    loading.value = false;
  }
}

function selectRange(range: StatsRange) {
  if (selectedRange.value === range) return;
  selectedRange.value = range;
  void load();
}

onMounted(() => void load());
</script>

<style scoped>
.stats-page { max-width: 1120px; margin: 0 auto; padding: 30px 22px; }
.page-header { display: flex; align-items: end; justify-content: space-between; margin-bottom: 22px; }
.eyebrow { margin: 0 0 5px; color: var(--fg-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
h1 { margin: 0; font-size: 25px; letter-spacing: 0; }
.refresh { display: flex; align-items: center; gap: 7px; height: 34px; padding: 0 10px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); cursor: pointer; }
.refresh:disabled { opacity: .65; cursor: wait; }
.range-tabs { display: flex; gap: 3px; border-bottom: 1px solid var(--border); margin-bottom: 18px; }
.range-tab { border: 0; border-bottom: 2px solid transparent; padding: 10px 13px; background: transparent; color: var(--fg-muted); cursor: pointer; font: inherit; }
.range-tab:hover, .range-tab.active { color: var(--fg); border-bottom-color: var(--accent); }
.state { padding: 38px 0; color: var(--fg-muted); }
.state.error { color: var(--danger, #d14b4b); }
.window { margin: 0 0 15px; color: var(--fg-muted); font-size: 13px; }
.metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
.metric { min-width: 0; padding: 15px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); }
.metric-label { display: block; color: var(--fg-muted); font-size: 12px; }
.metric-value { display: block; margin-top: 8px; overflow-wrap: anywhere; font-size: 22px; font-variant-numeric: tabular-nums; }
.counts { display: flex; gap: 25px; margin: 17px 0 0; }
.counts div { display: flex; gap: 8px; }
.counts dt { color: var(--fg-muted); }
.counts dd { margin: 0; font-weight: 600; font-variant-numeric: tabular-nums; }
.spinning { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 760px) { .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } .metric:last-child { grid-column: 1 / -1; } }
@media (max-width: 480px) { .page-header { align-items: start; gap: 12px; flex-direction: column; } .refresh { align-self: stretch; justify-content: center; } .metrics { grid-template-columns: 1fr; } .metric:last-child { grid-column: auto; } .range-tab { flex: 1; padding-inline: 5px; font-size: 12px; } }
</style>
