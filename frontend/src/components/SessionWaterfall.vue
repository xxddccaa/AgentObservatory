<template>
  <section class="waterfall" aria-labelledby="waterfall-title">
    <header class="waterfall__header">
      <div>
        <h2 id="waterfall-title">Session activity</h2>
        <p>{{ spans.length }} timed spans · UTC</p>
      </div>
    </header>
    <BaseChart
      :option="option"
      :empty="spans.length === 0"
      :loading="loading"
      :error="error"
      :height="height"
      label="Session activity waterfall. Select a bar to inspect its evidence."
      @chart-click="onClick"
    />
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { EChartsCoreOption } from "echarts/core";
import type { ActivitySpan } from "../api/insights";
import BaseChart from "./BaseChart.vue";

const props = withDefaults(
  defineProps<{
    spans: ActivitySpan[];
    loading?: boolean;
    error?: string;
  }>(),
  { loading: false, error: "" }
);

const emit = defineEmits<{
  evidenceSelect: [evidenceId: string, span: ActivitySpan];
  spanSelect: [span: ActivitySpan];
}>();

const lanes = computed(() => {
  const preferred = ["turn", "reasoning", "tool", "patch", "file_change", "web_search", "subagent"];
  const present = new Set(props.spans.map((span) => span.type));
  return [...preferred.filter((lane) => present.has(lane)), ...[...present].filter((lane) => !preferred.includes(lane))];
});

const start = computed(() => {
  const values = props.spans.map((span) => Date.parse(span.started_at)).filter(Number.isFinite);
  return values.length ? Math.min(...values) : Date.now();
});

const height = computed(() => Math.max(280, lanes.value.length * 46 + 110));

interface RenderApi {
  value(index: number): number;
  coord(value: [number, number]): [number, number];
  size(value: [number, number]): [number, number];
  style(style: Record<string, unknown>): Record<string, unknown>;
}

interface RenderParams {
  coordSys: { x: number; y: number; width: number; height: number };
}

function clipRect(
  shape: { x: number; y: number; width: number; height: number },
  bounds: RenderParams["coordSys"]
) {
  const x = Math.max(shape.x, bounds.x);
  const right = Math.min(shape.x + shape.width, bounds.x + bounds.width);
  if (right <= x) return undefined;
  return { ...shape, x, width: right - x };
}

const option = computed<EChartsCoreOption>(() => {
  const data = props.spans.map((span) => {
    const started = Date.parse(span.started_at) - start.value;
    const ended = span.completed_at
      ? Date.parse(span.completed_at) - start.value
      : started + Math.max(span.duration_ms ?? 1, 1);
    return {
      value: [lanes.value.indexOf(span.type), started, Math.max(ended, started + 1), span.id],
      itemStyle: { color: spanColor(span.type, span.status) },
      span,
    };
  });
  return {
    grid: { top: 18, left: 94, right: 22, bottom: 64, containLabel: false },
    xAxis: {
      type: "value",
      min: 0,
      axisLabel: { formatter: (value: number) => formatDuration(value) },
      name: "Elapsed",
      nameLocation: "middle",
      nameGap: 42,
      splitLine: { show: true },
    },
    yAxis: {
      type: "category",
      data: lanes.value.map(prettyName),
      axisTick: { show: false },
      splitLine: { show: false },
    },
    dataZoom: [
      { type: "inside", xAxisIndex: 0, filterMode: "weakFilter" },
      { type: "slider", xAxisIndex: 0, height: 18, bottom: 8, filterMode: "weakFilter" },
    ],
    tooltip: {
      trigger: "item",
      formatter: (params: unknown) => {
        const item = params as { data?: { span?: ActivitySpan } };
        const span = item.data?.span;
        if (!span) return "";
        return [
          `<strong>${escapeHtml(span.name || prettyName(span.type))}</strong>`,
          `${escapeHtml(span.status)} · ${formatDuration(span.duration_ms ?? 0)}`,
          `${escapeHtml(span.confidence)} confidence`,
        ].join("<br>");
      },
    },
    series: [
      {
        type: "custom",
        name: "Activity",
        encode: { x: [1, 2], y: 0 },
        data,
        renderItem: (params: RenderParams, api: RenderApi) => {
          const lane = api.value(0);
          const startPoint = api.coord([api.value(1), lane]);
          const endPoint = api.coord([api.value(2), lane]);
          const rowHeight = Math.min(api.size([0, 1])[1] * 0.58, 22);
          const shape = clipRect(
            {
              x: startPoint[0],
              y: startPoint[1] - rowHeight / 2,
              width: Math.max(endPoint[0] - startPoint[0], 2),
              height: rowHeight,
            },
            params.coordSys
          );
          return shape ? { type: "rect", shape, style: api.style({ borderRadius: 2 }) } : undefined;
        },
      },
    ],
  };
});

function spanColor(type: string, status: string): string {
  if (["failed", "error"].includes(status)) return "#f07872";
  if (["interrupted", "cancelled"].includes(status)) return "#e7b75f";
  const colors: Record<string, string> = {
    turn: "#8c9bab",
    reasoning: "#6e9ff8",
    tool: "#6fcf97",
    patch: "#d59a62",
    file_change: "#c28bd6",
    web_search: "#5fbfc4",
    subagent: "#e7b75f",
  };
  return colors[type] ?? "#98a19a";
}

function prettyName(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter: string) => letter.toUpperCase());
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => {
    const entities: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return entities[character] ?? character;
  });
}

function onClick(params: unknown) {
  const selected = (params as { data?: { span?: ActivitySpan } }).data?.span;
  if (!selected) return;
  emit("spanSelect", selected);
  if (selected.evidence_id) emit("evidenceSelect", selected.evidence_id, selected);
}
</script>

<style scoped>
.waterfall {
  min-width: 0;
  border-top: 1px solid var(--border);
  padding-top: 16px;
}
.waterfall__header {
  display: flex;
  align-items: start;
  justify-content: space-between;
  margin-bottom: 8px;
}
.waterfall h2 {
  margin: 0;
  font-size: 15px;
  letter-spacing: 0;
}
.waterfall p {
  margin: 2px 0 0;
  color: var(--fg-muted);
  font-size: 12px;
}
</style>
