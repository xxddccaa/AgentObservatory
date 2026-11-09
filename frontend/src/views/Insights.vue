<template>
  <main class="insights" aria-labelledby="insights-title">
    <header class="page-header">
      <div>
        <p class="eyebrow">Local multi-agent evidence</p>
        <h1 id="insights-title">Insights</h1>
        <p class="subtitle">Operational evidence for performance, quality, skills, and indexing.</p>
      </div>
      <div class="header-actions">
        <button class="action" type="button" :disabled="exporting" title="Export filtered data as CSV" @click="downloadExport('csv')">
          <Download :size="15" aria-hidden="true" />
          CSV
        </button>
        <button class="action" type="button" :disabled="exporting" title="Export filtered data as JSON" @click="downloadExport('json')">
          <Braces :size="15" aria-hidden="true" />
          JSON
        </button>
        <button class="action" type="button" :disabled="loading" title="Refresh insights" @click="refresh">
          <RefreshCw :size="15" :class="{ spinning: loading }" aria-hidden="true" />
          Refresh
        </button>
      </div>
    </header>

    <section class="filters" aria-label="Insight filters">
      <div class="range-control" role="group" aria-label="Time range">
        <button
          v-for="item in ranges"
          :key="item.value"
          type="button"
          :class="{ active: range === item.value }"
          :aria-pressed="range === item.value"
          @click="setRange(item.value)"
        >
          {{ item.label }}
        </button>
      </div>
      <label class="agent-control">
        <span>Agent</span>
        <select v-model="selectedAgent" aria-label="Filter insights by agent" @change="changeAgent">
          <option v-for="option in AGENT_OPTIONS" :key="option.value || 'all'" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </label>
      <div v-if="range === 'custom'" class="custom-range">
        <label>
          <span>From</span>
          <input v-model="customSince" type="datetime-local" />
        </label>
        <label>
          <span>Until</span>
          <input v-model="customUntil" type="datetime-local" />
        </label>
        <button type="button" :disabled="!customRangeValid" @click="applyCustomRange">
          Apply
        </button>
      </div>
      <p class="window">{{ windowLabel }}</p>
    </section>

    <nav class="tabs" role="tablist" aria-label="Insights sections">
      <button
        v-for="item in tabs"
        :id="`insights-tab-${item.key}`"
        :key="item.key"
        type="button"
        role="tab"
        :aria-selected="tab === item.key"
        :aria-controls="`insights-panel-${item.key}`"
        :class="{ active: tab === item.key }"
        @click="selectTab(item.key)"
      >
        {{ item.label }}
      </button>
    </nav>

    <p v-if="error" class="page-state error" role="alert">{{ error }}</p>
    <p v-if="drilldownNote" class="drilldown-note" role="status">
      <span>{{ drilldownNote }}</span>
      <button type="button" title="Clear selection" @click="clearDrilldown"><X :size="14" /></button>
    </p>

    <section
      v-show="tab === 'overview'"
      id="insights-panel-overview"
      role="tabpanel"
      aria-labelledby="insights-tab-overview"
    >
      <div class="metrics" aria-label="Overview totals">
        <article v-for="metric in overviewMetrics" :key="metric.label" class="metric">
          <span>{{ metric.label }}</span>
          <strong>{{ metric.value }}</strong>
          <small>{{ metric.detail }}</small>
        </article>
      </div>
      <section v-if="!selectedAgent" class="comparison-section" aria-labelledby="agent-comparison-title">
        <div class="section-heading">
          <div>
            <h2 id="agent-comparison-title">Cross-agent comparison</h2>
            <p>Sessions, turns, tokens, tool calls, and failures across indexed agents</p>
          </div>
        </div>
        <div class="comparison-grid">
          <BaseChart
            :option="agentComparisonOption"
            :loading="loading"
            :empty="!agentComparison.length"
            :error="sectionError('overview')"
            height="300px"
            label="Cross-agent session, turn, token, tool call, and failure comparison"
          />
          <div class="table-wrap">
            <table class="comparison-table">
              <thead>
                <tr><th>Agent</th><th>Sessions</th><th>Turns</th><th>Tokens</th><th>Tool calls</th><th>Failures</th></tr>
              </thead>
              <tbody>
                <tr v-for="item in agentComparison" :key="item.agent">
                  <td><AgentBadge :agent="item.agent" /></td>
                  <td>{{ number(item.session_count) }}</td>
                  <td>{{ number(item.turn_count) }}</td>
                  <td>{{ number(item.total_tokens) }}</td>
                  <td>{{ number(item.tool_call_count) }}</td>
                  <td>{{ number(item.failure_count) }}</td>
                </tr>
                <tr v-if="!agentComparison.length">
                  <td colspan="6" class="empty-cell">No cross-agent aggregate is available for this range.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>
      <div class="chart-grid">
        <section class="chart-section">
          <div class="section-heading">
            <div><h2>Activity and token volume</h2><p>Backend aggregate by {{ timeseries?.bucket || "time bucket" }}</p></div>
          </div>
          <BaseChart
            :option="activityOption"
            :loading="loading"
            :empty="!timeseries?.points.length"
            :error="sectionError('overview')"
            label="Turns and token volume over time"
          />
        </section>
        <section class="chart-section">
          <div class="section-heading">
            <div><h2>Turn outcomes</h2><p>Successful versus failed or interrupted turns</p></div>
          </div>
          <BaseChart
            :option="outcomeOption"
            :loading="loading"
            :empty="!timeseries?.points.length"
            :error="sectionError('overview')"
            label="Turn outcomes over time"
          />
        </section>
      </div>
      <section class="table-section">
        <div class="section-heading"><div><h2>Time buckets</h2><p>Exact aggregate values behind the charts</p></div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Bucket (UTC)</th><th>Sessions</th><th>Turns</th><th>Tokens</th><th>Successful</th><th>Failed / interrupted</th><th>Unknown / running</th></tr></thead>
            <tbody>
              <tr v-for="point in timeseries?.points || []" :key="point.timestamp">
                <td>{{ formatDate(point.timestamp) }}</td>
                <td>{{ number(point.session_count) }}</td>
                <td>{{ number(point.turn_count) }}</td>
                <td>{{ number(point.total_tokens) }}</td>
                <td>{{ number(point.success_count) }}</td>
                <td>{{ number(point.failure_count) }}</td>
                <td>{{ number(point.unknown_count) }}</td>
              </tr>
              <tr v-if="!timeseries?.points.length"><td colspan="7" class="empty-cell">No indexed activity in this range.</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </section>

    <section
      v-show="tab === 'performance'"
      id="insights-panel-performance"
      role="tabpanel"
      aria-labelledby="insights-tab-performance"
    >
      <div class="metrics metrics--compact" aria-label="Latency summary">
        <article v-for="metric in latencyMetrics" :key="metric.label" class="metric">
          <span>{{ metric.label }}</span><strong>{{ metric.value }}</strong><small>{{ metric.detail }}</small>
        </article>
      </div>
      <div class="chart-grid">
        <section class="chart-section">
          <div class="section-heading"><div><h2>Latency percentiles</h2><p>Turn, first agent event, and active tool aggregates</p></div></div>
          <BaseChart
            :option="latencyTrendOption"
            :loading="loading"
            :empty="!latency?.summary.turn_duration_ms.count"
            :error="sectionError('performance')"
            label="P50 and P95 turn duration over time"
          />
        </section>
        <section class="chart-section">
          <div class="section-heading"><div><h2>Duration distribution</h2><p>{{ number(latency?.summary.turn_duration_ms.count || 0) }} measured turns</p></div></div>
          <BaseChart
            :option="distributionOption"
            :loading="loading"
            :empty="!latency?.distribution.length"
            :error="sectionError('performance')"
            label="Turn duration histogram"
          />
        </section>
      </div>
      <section class="table-section">
        <div class="section-heading"><div><h2>Duration buckets</h2><p>Counts supplied by the Insights API</p></div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Range</th><th>Turns</th></tr></thead>
            <tbody>
              <tr v-for="bucket in latency?.distribution || []" :key="bucket.range">
                <td>{{ durationRange(bucket.range) }}</td><td>{{ number(bucket.count) }}</td>
              </tr>
              <tr v-if="!latency?.distribution.length"><td colspan="2" class="empty-cell">No latency evidence in this range.</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </section>

    <section
      v-show="tab === 'tools'"
      id="insights-panel-tools"
      role="tabpanel"
      aria-labelledby="insights-tab-tools"
    >
      <section class="chart-section chart-section--full">
        <div class="section-heading"><div><h2>Tool latency and failures</h2><p>P95 duration with failure rate in the audit table</p></div></div>
        <BaseChart
          :option="toolsOption"
          :loading="loading"
          :empty="!tools?.items.length"
          :error="sectionError('tools')"
          height="360px"
          label="Tool P95 duration comparison"
          @chart-click="chartDrilldown('tools', $event)"
        />
      </section>
      <section class="table-section">
        <div class="section-heading"><div><h2>Tool evidence</h2><p>Calls, failures, validation signals, and confidence</p></div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Tool</th><th>Kind</th><th>Calls</th><th>Failures</th><th>Failure rate</th><th>Average</th><th>P50</th><th>P95</th><th>Measured</th></tr></thead>
            <tbody>
              <tr v-for="item in tools?.items || []" :key="`${item.tool_name}:${item.tool_kind}`">
                <td class="primary-cell">{{ item.tool_name }}</td><td>{{ item.tool_kind }}</td><td>{{ number(item.call_count) }}</td><td>{{ number(item.failure_count) }}</td><td>{{ percent(item.failure_rate) }}</td><td>{{ duration(item.average_ms) }}</td><td>{{ duration(item.p50_ms) }}</td><td>{{ duration(item.p95_ms) }}</td>
                <td>{{ number(item.duration_count) }}</td>
              </tr>
              <tr v-if="!tools?.items.length"><td colspan="9" class="empty-cell">No tool evidence in this range.</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </section>

    <section
      v-show="tab === 'skills'"
      id="insights-panel-skills"
      role="tabpanel"
      aria-labelledby="insights-tab-skills"
    >
      <div class="chart-grid">
        <section class="chart-section">
          <div class="section-heading"><div><h2>Skill evidence</h2><p>Availability is not treated as usage</p></div></div>
          <BaseChart
            :option="skillsOption"
            :loading="loading"
            :empty="!skills?.items.length"
            :error="sectionError('skills')"
            label="Skill evidence levels by session"
          />
        </section>
        <section class="chart-section">
          <div class="section-heading"><div><h2>Agent relationships</h2><p>{{ number(agents?.nodes.length || 0) }} threads · {{ number(agents?.edges.length || 0) }} links</p></div></div>
          <BaseChart
            :option="agentsOption"
            :loading="loading"
            :empty="!agents?.nodes.length"
            :error="sectionError('skills')"
            label="Parent and child agent thread graph"
            @chart-click="chartDrilldown('agents', $event)"
          />
        </section>
      </div>
      <section class="table-section">
        <div class="section-heading"><div><h2>Skill evidence table</h2><p>Distinct session counts by evidence strength</p></div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Skill</th><th>Scope</th><th>Sessions</th><th>Available</th><th>Candidate</th><th>Loaded</th><th>Inferred</th><th>Confidence</th><th>Last seen</th></tr></thead>
            <tbody>
              <tr v-for="item in skills?.items || []" :key="item.skill_name">
                <td><strong>{{ item.skill_name }}</strong><small v-if="item.description" class="description">{{ item.description }}</small></td><td>{{ item.scope || "unknown" }}</td><td>{{ number(item.session_count) }}</td><td>{{ number(item.evidence.available) }}</td><td>{{ number(item.evidence.candidate) }}</td><td>{{ number(item.evidence.loaded) }}</td><td>{{ number(item.evidence.inferred) }}</td>
                <td><span class="confidence">{{ dominantConfidence(item.confidence) }}</span></td><td>{{ item.last_seen_at ? formatDate(item.last_seen_at) : "—" }}</td>
              </tr>
              <tr v-if="!skills?.items.length"><td colspan="9" class="empty-cell">No skill evidence in this range.</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </section>

    <section
      v-show="tab === 'sources'"
      id="insights-panel-sources"
      role="tabpanel"
      aria-labelledby="insights-tab-sources"
    >
      <section class="source-summary" aria-label="Data source status">
        <div><Database :size="17" aria-hidden="true" /><strong>{{ number(sources?.sources.length || 0) }}</strong><span>sources</span></div>
        <div><CircleCheck :size="17" aria-hidden="true" /><strong>{{ number(healthySources) }}</strong><span>healthy</span></div>
        <div><TriangleAlert :size="17" aria-hidden="true" /><strong>{{ number(problemSources) }}</strong><span>need attention</span></div>
      </section>
      <section class="table-section">
        <div class="section-heading"><div><h2>Local data catalog</h2><p>Read-only source coverage, indexing state, and parser errors</p></div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Agent</th><th>Source</th><th>Status</th><th>Records</th><th>Size</th><th>Coverage (UTC)</th><th>Last indexed</th><th>Error</th><th>Evidence</th></tr></thead>
            <tbody>
              <tr v-for="source in sources?.sources || []" :key="source.id">
                <td><AgentBadge v-if="source.agent" :agent="source.agent" /><span v-else>—</span></td>
                <td><strong>{{ source.source_kind }}</strong><small class="path">{{ source.source_path }}</small></td>
                <td><span class="status" :data-tone="sourceTone(source.status)">{{ source.status }}</span></td>
                <td>{{ number(source.record_count) }}</td><td>{{ bytes(source.byte_size) }}</td>
                <td>{{ coverage(source.coverage_started_at, source.coverage_ended_at) }}</td><td>{{ source.last_success_at ? formatDate(source.last_success_at) : "Never" }}</td>
                <td class="error-detail"><strong v-if="source.last_error_kind">{{ source.last_error_kind }}</strong>{{ source.last_error || "—" }}</td>
                <td><button class="inspect-btn" type="button" @click="inspectSource(source)"><Eye :size="14" />Inspect</button></td>
              </tr>
              <tr v-if="!sources?.sources.length"><td colspan="9" class="empty-cell">No local sources discovered.</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section v-if="selectedSource" class="table-section evidence-browser">
        <div class="section-heading">
          <div><h2>Evidence · {{ selectedSource.source_kind }}</h2><p>{{ selectedSource.source_path }}</p></div>
          <button class="icon-btn" type="button" title="Close evidence browser" @click="closeEvidenceBrowser"><X :size="15" /></button>
        </div>
        <p v-if="evidenceLoading" class="page-state">Loading evidence…</p>
        <p v-else-if="evidenceError" class="page-state error">{{ evidenceError }}</p>
        <div v-else class="table-wrap">
          <table>
            <thead><tr><th>Time (UTC)</th><th>Category</th><th>Level</th><th>Target / summary</th><th>Size</th><th>Raw</th></tr></thead>
            <tbody>
              <tr v-for="item in evidenceItems" :key="item.id">
                <td>{{ item.recorded_at ? formatDate(item.recorded_at) : "—" }}</td>
                <td>{{ item.category }}</td><td>{{ item.level || "—" }}</td>
                <td><strong v-if="item.target">{{ item.target }}</strong><small class="description">{{ item.summary || item.source_record_id }}</small></td>
                <td>{{ bytes(item.raw_size) }}</td>
                <td><button class="inspect-btn" type="button" @click="openEvidence(item.id)"><Eye :size="14" />Open</button></td>
              </tr>
              <tr v-if="!evidenceItems.length"><td colspan="6" class="empty-cell">No evidence in the active time range.</td></tr>
            </tbody>
          </table>
        </div>
        <section v-if="selectedEvidence" class="raw-evidence" aria-labelledby="raw-evidence-title">
          <header>
            <div><h3 id="raw-evidence-title">{{ selectedEvidence.category }}</h3><p>{{ selectedEvidence.summary || selectedEvidence.id }}</p></div>
            <button class="inspect-btn" type="button" @click="copyEvidence"><Copy :size="14" />Copy full evidence</button>
          </header>
          <pre>{{ selectedEvidenceText }}</pre>
        </section>
      </section>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Braces, CircleCheck, Copy, Database, Download, Eye, RefreshCw, TriangleAlert, X } from "@lucide/vue";
import type { EChartsCoreOption } from "echarts/core";
import {
  getInsightsAgents,
  getInsightsExport,
  getInsightsLatency,
  getInsightsOverview,
  getInsightsSkills,
  getInsightsSources,
  getInsightsTimeseries,
  getInsightsTools,
  getInsightEvidence,
  listInsightEvidence,
  type InsightEvidence,
  type AgentComparison,
  type InsightEvidenceSummary,
  type InsightFilters,
  type InsightRange,
  type InsightsAgents,
  type InsightsLatency,
  type InsightsOverview,
  type InsightsSkills,
  type InsightsSources,
  type InsightsTimeseries,
  type InsightsTools,
  type LocalDataSourceInsight,
} from "../api/insights";
import BaseChart from "../components/BaseChart.vue";
import AgentBadge from "../components/AgentBadge.vue";
import { AGENT_OPTIONS, agentLabel, type AgentFilter } from "../constants/agents";

type TabKey = "overview" | "performance" | "tools" | "skills" | "sources";
const router = useRouter();
const route = useRoute();
const ranges: ReadonlyArray<{ value: InsightRange; label: string }> = [
  { value: "24h", label: "24h" }, { value: "7d", label: "7d" }, { value: "30d", label: "30d" }, { value: "custom", label: "Custom" },
];
const tabs: ReadonlyArray<{ key: TabKey; label: string }> = [
  { key: "overview", label: "Overview" },
  { key: "performance", label: "Performance" },
  { key: "tools", label: "Tools & Quality" },
  { key: "skills", label: "Skills & Agents" },
  { key: "sources", label: "Data Sources" },
];

const tab = ref<TabKey>("overview");
const range = ref<InsightRange>("7d");
const requestedAgent = typeof route.query.agent === "string" ? route.query.agent : "";
const selectedAgent = ref<AgentFilter>(
  AGENT_OPTIONS.some((option) => option.value === requestedAgent)
    ? (requestedAgent as AgentFilter)
    : ""
);
const customSince = ref(localInput(new Date(Date.now() - 24 * 60 * 60 * 1000)));
const customUntil = ref(localInput(new Date()));
const appliedCustom = ref<{ since: string; until: string } | null>(null);
const loading = ref(false);
const exporting = ref(false);
const error = ref("");
const loaded = new Set<TabKey>();
const overview = ref<InsightsOverview | null>(null);
const timeseries = ref<InsightsTimeseries | null>(null);
const latency = ref<InsightsLatency | null>(null);
const tools = ref<InsightsTools | null>(null);
const skills = ref<InsightsSkills | null>(null);
const agents = ref<InsightsAgents | null>(null);
const sources = ref<InsightsSources | null>(null);
const selectedSource = ref<LocalDataSourceInsight | null>(null);
const evidenceItems = ref<InsightEvidenceSummary[]>([]);
const selectedEvidence = ref<InsightEvidence | null>(null);
const evidenceLoading = ref(false);
const evidenceError = ref("");
const drilldownNote = ref("");
const activeTool = ref("");

const selectedEvidenceText = computed(() => {
  const evidence = selectedEvidence.value;
  if (!evidence) return "";
  if (evidence.raw_text) return evidence.raw_text;
  return JSON.stringify(evidence.raw_json ?? evidence.metadata, null, 2);
});

const filters = computed<InsightFilters>(() => {
  const agent = selectedAgent.value || undefined;
  if (range.value === "custom" && appliedCustom.value) {
    return { range: "custom", ...appliedCustom.value, agent };
  }
  return { range: range.value, agent };
});
const customRangeValid = computed(() => Date.parse(customSince.value) < Date.parse(customUntil.value));
const windowLabel = computed(() => {
  if (range.value === "custom" && !appliedCustom.value) return "Choose a valid start and end time, then apply.";
  const value = currentWindow();
  return value ? `${formatDate(value.since)} to ${formatDate(value.until)} UTC` : "UTC time range";
});
const healthySources = computed(() => sources.value?.sources.filter((source) => ["indexed", "ready", "healthy"].includes(source.status)).length || 0);
const problemSources = computed(() => sources.value?.sources.filter((source) => ["failed", "error", "degraded"].includes(source.status)).length || 0);
const agentComparison = computed<AgentComparison[]>(() => {
  const items = overview.value?.by_agent?.length
    ? overview.value.by_agent
    : timeseries.value?.by_agent?.length
      ? timeseries.value.by_agent
      : tools.value?.by_agent || [];
  return [...items].sort((left, right) => {
    if (left.session_count !== right.session_count) return right.session_count - left.session_count;
    return left.agent.localeCompare(right.agent);
  });
});

const overviewMetrics = computed(() => {
  const totals = overview.value?.totals;
  const outcomes = overview.value?.quality.outcomes || {};
  const validations = overview.value?.quality.validation || {};
  const validationTotal = Object.values(validations).reduce((sum, value) => sum + value, 0);
  const validationPassed = (validations.passed || 0) + (validations.pass || 0) + (validations.success || 0);
  return [
    { label: "Sessions", value: number(totals?.session_count || 0), detail: `${number(totals?.turn_count || 0)} turns` },
    { label: "Total tokens", value: number(totals?.total_tokens || 0), detail: "Indexed usage" },
    { label: "Completed", value: number(outcomes.completed || 0), detail: `${number((outcomes.failed || 0) + (outcomes.error || 0))} failed` },
    { label: "Interrupted", value: number(outcomes.interrupted || 0), detail: `${number(outcomes.unknown || 0)} unknown outcomes` },
    { label: "Validation pass", value: validationTotal ? percent(validationPassed / validationTotal) : "—", detail: `${number(validationTotal)} detected signals` },
  ];
});

const latencyMetrics = computed(() => {
  const summary = latency.value?.summary;
  const confidence = summary ? dominantConfidence(summary.confidence) : "No confidence";
  return [
    { label: "Turn P50", value: duration(summary?.turn_duration_ms.p50), detail: `${number(summary?.turn_duration_ms.count || 0)} samples` },
    { label: "Turn P95", value: duration(summary?.turn_duration_ms.p95), detail: confidence },
    { label: "First agent P50", value: duration(summary?.first_agent_latency_ms.p50), detail: `P95 ${duration(summary?.first_agent_latency_ms.p95)}` },
    { label: "Active tool P50", value: duration(summary?.active_tool_ms.p50), detail: `P95 ${duration(summary?.active_tool_ms.p95)}` },
  ];
});

const activityOption = computed<EChartsCoreOption>(() => ({
  grid: compactGrid(), tooltip: { trigger: "axis" }, legend: { data: ["Turns", "Tokens"] },
  xAxis: { type: "category", data: chartDates(), boundaryGap: false },
  yAxis: [{ type: "value", name: "Turns" }, { type: "value", name: "Tokens", splitLine: { show: false } }],
  series: [
    { name: "Turns", type: "line", smooth: true, showSymbol: false, data: values("turn_count"), areaStyle: { opacity: 0.08 } },
    { name: "Tokens", type: "line", yAxisIndex: 1, showSymbol: false, data: values("total_tokens") },
  ],
}));
const outcomeOption = computed<EChartsCoreOption>(() => ({
  grid: compactGrid(), tooltip: { trigger: "axis" }, legend: { data: ["Successful", "Failed / interrupted", "Unknown / running"] },
  xAxis: { type: "category", data: chartDates() }, yAxis: { type: "value", minInterval: 1 },
  series: [
    { name: "Successful", type: "bar", stack: "outcomes", data: values("success_count") },
    { name: "Failed / interrupted", type: "bar", stack: "outcomes", data: values("failure_count"), itemStyle: { color: "#f07872" } },
    { name: "Unknown / running", type: "bar", stack: "outcomes", data: values("unknown_count"), itemStyle: { color: "#8c9bab" } },
  ],
}));
const agentComparisonOption = computed<EChartsCoreOption>(() => ({
  grid: compactGrid(54),
  tooltip: { trigger: "axis" },
  legend: { data: ["Sessions", "Turns", "Tool calls", "Failures", "Tokens"] },
  xAxis: {
    type: "category",
    data: agentComparison.value.map((item) => item.agent),
    axisLabel: { formatter: (value: string) => agentLabel(value) },
  },
  yAxis: [
    { type: "value", minInterval: 1, name: "Counts" },
    { type: "value", name: "Tokens", splitLine: { show: false } },
  ],
  series: [
    { name: "Sessions", type: "bar", data: agentComparison.value.map((item) => item.session_count) },
    { name: "Turns", type: "bar", data: agentComparison.value.map((item) => item.turn_count) },
    { name: "Tool calls", type: "bar", data: agentComparison.value.map((item) => item.tool_call_count) },
    { name: "Failures", type: "bar", data: agentComparison.value.map((item) => item.failure_count), itemStyle: { color: "#f07872" } },
    { name: "Tokens", type: "line", yAxisIndex: 1, data: agentComparison.value.map((item) => item.total_tokens) },
  ],
}));
const latencyTrendOption = computed<EChartsCoreOption>(() => ({
  grid: compactGrid(), tooltip: { trigger: "axis", valueFormatter: (value: unknown) => duration(Number(value)) }, legend: { data: ["P50", "P95", "Average"] },
  xAxis: { type: "category", data: ["Turn", "First agent", "Active tool"] }, yAxis: { type: "value", axisLabel: { formatter: (value: number) => duration(value) } },
  series: [
    { name: "P50", type: "bar", data: latencyMeasures("p50") },
    { name: "P95", type: "bar", data: latencyMeasures("p95") },
    { name: "Average", type: "bar", data: latencyMeasures("average") },
  ],
}));
const distributionOption = computed<EChartsCoreOption>(() => ({
  grid: compactGrid(), tooltip: { trigger: "axis" }, xAxis: { type: "category", data: latency.value?.distribution.map((bucket) => durationRange(bucket.range)) || [] }, yAxis: { type: "value", minInterval: 1 },
  series: [{ name: "Turns", type: "bar", data: latency.value?.distribution.map((bucket) => bucket.count) || [], barMaxWidth: 42 }],
}));
const toolsOption = computed<EChartsCoreOption>(() => {
  const items = [...(tools.value?.items || [])].sort((a, b) => b.p95_ms - a.p95_ms).slice(0, 15).reverse();
  return {
    grid: { top: 12, right: 42, bottom: 34, left: 128 }, tooltip: { trigger: "axis", valueFormatter: (value: unknown) => duration(Number(value)) },
    xAxis: { type: "value", axisLabel: { formatter: (value: number) => duration(value) } }, yAxis: { type: "category", data: items.map((item) => item.tool_name), axisLabel: { width: 110, overflow: "truncate" } },
    series: [{ name: "P95", type: "bar", data: items.map((item) => item.p95_ms), barMaxWidth: 18 }],
  };
});
const skillsOption = computed<EChartsCoreOption>(() => {
  const items = (skills.value?.items || []).slice(0, 12);
  return {
    grid: compactGrid(78), tooltip: { trigger: "axis" }, legend: { data: ["Available", "Candidate", "Loaded", "Inferred"] },
    xAxis: { type: "category", data: items.map((item) => item.skill_name), axisLabel: { rotate: 25, width: 90, overflow: "truncate" } }, yAxis: { type: "value", minInterval: 1 },
    series: [
      { name: "Available", type: "bar", stack: "evidence", data: items.map((item) => item.evidence.available) },
      { name: "Candidate", type: "bar", stack: "evidence", data: items.map((item) => item.evidence.candidate) },
      { name: "Loaded", type: "bar", stack: "evidence", data: items.map((item) => item.evidence.loaded) },
      { name: "Inferred", type: "bar", stack: "evidence", data: items.map((item) => item.evidence.inferred) },
    ],
  };
});
const agentsOption = computed<EChartsCoreOption>(() => ({
  tooltip: { formatter: (params: unknown) => String((params as { name?: string }).name || "") },
  series: [{
    type: "graph", layout: "force", roam: true, draggable: false, symbolSize: 13,
    label: { show: true, position: "right", formatter: "{b}", width: 100, overflow: "truncate" },
    force: { repulsion: 120, edgeLength: 70 },
    data: (agents.value?.nodes || []).map((node) => ({ id: node.thread_id, name: node.title || node.thread_id, value: node.outcome || "unknown", itemStyle: { color: statusColor(node.outcome || "unknown") } })),
    links: (agents.value?.edges || []).map((edge) => ({ source: edge.source, target: edge.target, value: edge.status })),
    lineStyle: { color: "#6f7871", opacity: 0.7 }, emphasis: { focus: "adjacency" },
  }],
}));

function compactGrid(bottom = 42) { return { top: 40, right: 42, bottom, left: 58, containLabel: true }; }
function chartDates() { return timeseries.value?.points.map((point) => formatChartDate(point.timestamp)) || []; }
function values(key: keyof NonNullable<InsightsTimeseries["points"]>[number]) { return timeseries.value?.points.map((point) => point[key] ?? null) || []; }
function latencyMeasures(key: "p50" | "p95" | "average") {
  const summary = latency.value?.summary;
  return summary ? [summary.turn_duration_ms[key], summary.first_agent_latency_ms[key], summary.active_tool_ms[key]] : [];
}
function currentWindow() { return [overview.value, timeseries.value, latency.value].find((value) => value?.since && value?.until) || appliedCustom.value; }
function sectionError(section: TabKey) { return tab.value === section ? error.value : ""; }

function clearTabData(key: TabKey) {
  if (key === "overview") {
    overview.value = null;
    timeseries.value = null;
  }
  if (key === "performance") {
    latency.value = null;
    timeseries.value = null;
  }
  if (key === "tools") tools.value = null;
  if (key === "skills") {
    skills.value = null;
    agents.value = null;
  }
  if (key === "sources") {
    sources.value = null;
    closeEvidenceBrowser();
  }
}

async function loadTab(key: TabKey, force = false) {
  if (loaded.has(key) && !force) return;
  loading.value = true;
  error.value = "";
  clearTabData(key);
  try {
    if (key === "overview") [overview.value, timeseries.value] = await Promise.all([getInsightsOverview(filters.value), getInsightsTimeseries(filters.value)]);
    if (key === "performance") [latency.value, timeseries.value] = await Promise.all([getInsightsLatency(filters.value), getInsightsTimeseries(filters.value)]);
    if (key === "tools") tools.value = await getInsightsTools(filters.value);
    if (key === "skills") [skills.value, agents.value] = await Promise.all([getInsightsSkills(filters.value), getInsightsAgents(filters.value)]);
    if (key === "sources") sources.value = await getInsightsSources(filters.value);
    loaded.add(key);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load local insights.";
  } finally {
    loading.value = false;
  }
}
function selectTab(key: TabKey) { tab.value = key; void loadTab(key); }
function setRange(value: InsightRange) {
  range.value = value;
  appliedCustom.value = null;
  loaded.clear();
  if (value === "custom") {
    error.value = "";
    clearTabData(tab.value);
    return;
  }
  void loadTab(tab.value, true);
}
async function setAgent() {
  const query = { ...route.query };
  if (selectedAgent.value) query.agent = selectedAgent.value;
  else delete query.agent;
  await router.replace({ query });
  loaded.clear();
  closeEvidenceBrowser();
  activeTool.value = "";
  drilldownNote.value = "";
  await loadTab(tab.value, true);
}
function changeAgent(event: Event) {
  selectedAgent.value = (event.target as HTMLSelectElement).value as AgentFilter;
  void setAgent();
}
function applyCustomRange() {
  if (!customRangeValid.value) return;
  appliedCustom.value = { since: new Date(customSince.value).toISOString(), until: new Date(customUntil.value).toISOString() };
  loaded.clear(); void loadTab(tab.value, true);
}
function refresh() { loaded.delete(tab.value); void loadTab(tab.value, true); }
async function chartDrilldown(chart: string, params: unknown) {
  const selected = params as {
    name?: string;
    value?: unknown;
    data?: { id?: string; name?: string };
  };
  if (chart === "agents" && selected.data?.id) {
    const node = agents.value?.nodes.find((item) => item.thread_id === selected.data?.id);
    if (node?.session_id) {
      await router.push(`/history/${encodeURIComponent(node.session_id)}`);
      return;
    }
  }
  if (chart === "tools" && selected.name) {
    activeTool.value = selected.name;
    drilldownNote.value = `tools: ${selected.name}`;
    tools.value = await getInsightsTools({ ...filters.value, tool: selected.name });
  }
}
function clearDrilldown() {
  const reloadTools = Boolean(activeTool.value);
  activeTool.value = "";
  drilldownNote.value = "";
  if (reloadTools) void loadTab("tools", true);
}
async function inspectSource(source: LocalDataSourceInsight) {
  selectedSource.value = source;
  selectedEvidence.value = null;
  evidenceLoading.value = true;
  evidenceError.value = "";
  try {
    evidenceItems.value = (await listInsightEvidence(source.id, filters.value)).items;
  } catch (cause) {
    evidenceError.value = cause instanceof Error ? cause.message : "Unable to load source evidence.";
  } finally {
    evidenceLoading.value = false;
  }
}
async function openEvidence(evidenceId: string) {
  evidenceLoading.value = true;
  evidenceError.value = "";
  try {
    selectedEvidence.value = await getInsightEvidence(evidenceId);
  } catch (cause) {
    evidenceError.value = cause instanceof Error ? cause.message : "Unable to load raw evidence.";
  } finally {
    evidenceLoading.value = false;
  }
}
function closeEvidenceBrowser() {
  selectedSource.value = null;
  selectedEvidence.value = null;
  evidenceItems.value = [];
  evidenceError.value = "";
}
async function copyEvidence() {
  try {
    await navigator.clipboard.writeText(selectedEvidenceText.value);
  } catch {
    evidenceError.value = "Clipboard access is unavailable.";
  }
}
async function downloadExport(format: "csv" | "json") {
  exporting.value = true; error.value = "";
  try {
    const exported = await getInsightsExport(format, filters.value);
    const content = typeof exported.content === "string" ? exported.content : JSON.stringify(exported.content, null, 2);
    const url = URL.createObjectURL(new Blob([content], { type: exported.media_type }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = exported.filename; anchor.click();
    URL.revokeObjectURL(url);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to export insights.";
  } finally {
    exporting.value = false;
  }
}

function number(value: number) { return (value ?? 0).toLocaleString(); }
function percent(value: number | null | undefined) { if (value == null) return "—"; const normalized = value <= 1 ? value * 100 : value; return `${normalized.toFixed(1)}%`; }
function duration(value: number | null | undefined) { if (value == null) return "—"; if (value < 1000) return `${Math.round(value)} ms`; if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)} s`; return `${(value / 60_000).toFixed(1)} min`; }
function durationRange(value: string) { const labels: Record<string, string> = { under_1s: "Under 1 s", "1s_5s": "1–5 s", "5s_30s": "5–30 s", "30s_2m": "30 s–2 min", over_2m: "Over 2 min" }; return labels[value] || value.replace(/_/g, " "); }
function bytes(value: number) { if (!value) return "0 B"; const units = ["B", "KB", "MB", "GB", "TB"]; const rank = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1); return `${(value / 1024 ** rank).toFixed(rank ? 1 : 0)} ${units[rank]}`; }
function formatDate(value: string) { return new Date(value).toLocaleString(undefined, { timeZone: "UTC", dateStyle: "medium", timeStyle: "short" }); }
function formatChartDate(value: string) { return new Date(value).toLocaleString(undefined, { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function coverage(start: string | null, end: string | null) { return start && end ? `${formatDate(start)} – ${formatDate(end)}` : "No indexed coverage"; }
function sourceTone(status: string) { if (["indexed", "ready", "healthy"].includes(status)) return "ok"; if (["failed", "error", "degraded"].includes(status)) return "danger"; return "muted"; }
function statusColor(status: string) { if (["completed", "ready", "running"].includes(status)) return "#6fcf97"; if (["failed", "error"].includes(status)) return "#f07872"; if (["interrupted", "cancelled"].includes(status)) return "#e7b75f"; return "#6e9ff8"; }
function dominantConfidence(counts: Record<string, number>) { return Object.entries(counts).sort((left, right) => right[1] - left[1])[0]?.[0] || "unknown"; }
function localInput(date: Date) { const offset = date.getTimezoneOffset() * 60_000; return new Date(date.getTime() - offset).toISOString().slice(0, 16); }

onMounted(() => void loadTab("overview"));
</script>

<style scoped>
.insights { max-width: 1320px; margin: 0 auto; padding: 28px 22px 54px; }
.page-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 18px; }
.eyebrow { margin: 0 0 4px; color: var(--fg-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0; }
h1 { margin: 0; font-size: 25px; letter-spacing: 0; }
.subtitle, .section-heading p { margin: 3px 0 0; color: var(--fg-muted); font-size: 12px; }
.header-actions { display: flex; gap: 6px; }
.action { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 34px; padding: 0 10px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); text-decoration: none; cursor: pointer; }
.action:disabled { opacity: .6; cursor: wait; }
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; margin-top: 20px; min-height: 38px; }
.agent-control { display: flex; align-items: center; gap: 7px; color: var(--fg-muted); font-size: 11px; text-transform: uppercase; }
.agent-control select { height: 34px; min-width: 126px; border: 1px solid var(--border); border-radius: 5px; padding: 0 28px 0 9px; background: var(--panel); color: var(--fg); font-size: 12px; text-transform: none; cursor: pointer; }
.range-control { display: inline-flex; padding: 2px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
.range-control button, .custom-range button { height: 29px; border: 0; border-radius: 4px; padding: 0 11px; background: transparent; color: var(--fg-muted); cursor: pointer; }
.range-control button.active { background: var(--panel-3); color: var(--fg); }
.custom-range { display: flex; flex-wrap: wrap; align-items: end; gap: 7px; }
.custom-range label { display: grid; gap: 2px; color: var(--fg-muted); font-size: 10px; text-transform: uppercase; }
.custom-range input { height: 31px; border: 1px solid var(--border); border-radius: 4px; padding: 0 7px; background: var(--panel); color: var(--fg); color-scheme: dark; }
.custom-range button { border: 1px solid var(--border); background: var(--panel); color: var(--fg); }
.custom-range button:disabled { opacity: .5; }
.window { margin: 0 0 0 auto; color: var(--fg-muted); font-size: 12px; }
.tabs { display: flex; gap: 3px; margin: 17px 0 20px; overflow-x: auto; border-bottom: 1px solid var(--border); }
.tabs button { flex: 0 0 auto; border: 0; border-bottom: 2px solid transparent; padding: 10px 13px; background: transparent; color: var(--fg-muted); cursor: pointer; }
.tabs button.active { border-bottom-color: var(--accent); color: var(--fg); }
.page-state { padding: 20px 0; color: var(--fg-muted); }
.page-state.error { color: var(--danger); }
.drilldown-note { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 16px; padding: 8px 10px; border-left: 3px solid var(--accent); background: var(--accent-weak); color: var(--fg); }
.drilldown-note button { display: inline-grid; place-items: center; width: 26px; height: 26px; border: 0; border-radius: 4px; background: transparent; color: var(--fg-muted); cursor: pointer; }
.metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
.metrics--compact { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.metric { min-width: 0; padding: 14px 16px; border-right: 1px solid var(--border); }
.metric:last-child { border-right: 0; }
.metric span, .metric small { display: block; color: var(--fg-muted); font-size: 11px; }
.metric strong { display: block; margin: 5px 0 2px; overflow-wrap: anywhere; font-size: 21px; font-variant-numeric: tabular-nums; }
.chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; margin-top: 24px; }
.comparison-section { min-width: 0; margin-top: 24px; padding-top: 14px; border-top: 1px solid var(--border); }
.comparison-grid { display: grid; grid-template-columns: minmax(360px, .9fr) minmax(520px, 1.1fr); gap: 20px; align-items: stretch; }
.comparison-table { min-width: 600px; }
.chart-section, .table-section { min-width: 0; border-top: 1px solid var(--border); padding-top: 14px; }
.chart-section--full { margin-top: 4px; }
.section-heading { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.section-heading h2 { margin: 0; font-size: 15px; letter-spacing: 0; }
.table-section { margin-top: 24px; }
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; }
table { width: 100%; min-width: 760px; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
th, td { padding: 9px 11px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { background: var(--panel-2); color: var(--fg-muted); font-weight: 500; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: color-mix(in srgb, var(--panel) 70%, transparent); }
.primary-cell, td strong { color: var(--fg); font-weight: 600; }
.empty-cell { height: 76px; color: var(--fg-muted); text-align: center; }
.confidence, .status { display: inline-block; border: 1px solid var(--border); border-radius: 999px; padding: 1px 7px; color: var(--fg-muted); font-size: 11px; }
.status[data-tone="ok"] { border-color: var(--ok); color: var(--ok); }
.status[data-tone="danger"] { border-color: var(--danger); color: var(--danger); }
.source-summary { display: flex; gap: 28px; padding: 12px 0 18px; border-bottom: 1px solid var(--border); }
.source-summary div { display: flex; align-items: center; gap: 7px; color: var(--fg-muted); }
.source-summary strong { color: var(--fg); font-size: 17px; }
.path { display: block; max-width: 360px; margin-top: 3px; overflow: hidden; color: var(--fg-muted); font-family: var(--mono); text-overflow: ellipsis; white-space: nowrap; }
.description { display: block; max-width: 360px; margin-top: 3px; color: var(--fg-muted); font-weight: 400; }
.error-detail { max-width: 320px; overflow-wrap: anywhere; color: var(--fg-muted); }
.inspect-btn, .icon-btn { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 30px; border: 1px solid var(--border); border-radius: 5px; padding: 0 8px; background: var(--panel); color: var(--fg); cursor: pointer; white-space: nowrap; }
.icon-btn { width: 30px; padding: 0; }
.evidence-browser .section-heading p { max-width: 760px; overflow-wrap: anywhere; font-family: var(--mono); }
.raw-evidence { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--border); }
.raw-evidence header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.raw-evidence h3 { margin: 0; font-size: 14px; letter-spacing: 0; }
.raw-evidence p { margin: 3px 0 0; color: var(--fg-muted); font-size: 12px; }
.raw-evidence pre { max-height: 520px; overflow: auto; padding: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.55 var(--mono); }
.error-detail strong { display: block; color: var(--danger); }
.spinning { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 960px) { .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); } .metric { border-bottom: 1px solid var(--border); } .chart-grid, .comparison-grid { grid-template-columns: 1fr; } }
@media (max-width: 620px) { .insights { padding-inline: 14px; } .page-header { align-items: start; flex-direction: column; } .header-actions { width: 100%; } .action { flex: 1; } .window { width: 100%; margin-left: 0; } .metrics, .metrics--compact { grid-template-columns: repeat(2, minmax(0, 1fr)); } .metric:nth-child(even) { border-right: 0; } .source-summary { flex-wrap: wrap; gap: 12px 20px; } }
</style>
