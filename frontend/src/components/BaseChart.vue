<template>
  <div
    class="base-chart"
    :style="{ '--chart-height': normalizedHeight }"
    :aria-busy="loading"
  >
    <div
      ref="chartElement"
      class="base-chart__canvas"
      role="img"
      :aria-label="label"
    />
    <div v-if="error" class="base-chart__state base-chart__state--error" role="alert">
      {{ error }}
    </div>
    <div v-else-if="empty && !loading" class="base-chart__state">
      {{ emptyText }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  BarChart,
  CustomChart,
  GraphChart,
  LineChart,
} from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { init, use, type ECharts, type EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

use([
  AriaComponent,
  BarChart,
  CanvasRenderer,
  CustomChart,
  DataZoomComponent,
  GraphChart,
  GridComponent,
  LegendComponent,
  LineChart,
  TooltipComponent,
]);

const props = withDefaults(
  defineProps<{
    option: EChartsCoreOption;
    loading?: boolean;
    empty?: boolean;
    error?: string;
    emptyText?: string;
    label: string;
    height?: number | string;
  }>(),
  {
    loading: false,
    empty: false,
    error: "",
    emptyText: "No data in this range.",
    height: 300,
  }
);

const emit = defineEmits<{
  chartClick: [params: unknown];
}>();

const chartElement = ref<HTMLDivElement | null>(null);
const normalizedHeight = computed(() =>
  typeof props.height === "number" ? `${props.height}px` : props.height
);
let chart: ECharts | null = null;
let observer: ResizeObserver | null = null;
let media: MediaQueryList | null = null;

function cssColor(name: string, fallback: string): string {
  if (!chartElement.value) return fallback;
  return getComputedStyle(chartElement.value).getPropertyValue(name).trim() || fallback;
}

function themedOption(): EChartsCoreOption {
  const muted = cssColor("--fg-muted", "#98a19a");
  const border = cssColor("--border", "#2a302c");
  const fg = cssColor("--fg", "#f1f3ef");
  const panel = cssColor("--panel", "#151816");
  return {
    animation: !(media?.matches ?? false),
    color: [
      cssColor("--accent", "#6e9ff8"),
      cssColor("--ok", "#6fcf97"),
      cssColor("--warn", "#e7b75f"),
      cssColor("--danger", "#f07872"),
      "#8c9bab",
    ],
    textStyle: { color: fg, fontFamily: "Inter, ui-sans-serif, sans-serif" },
    aria: { enabled: true, decal: { show: true } },
    tooltip: {
      backgroundColor: panel,
      borderColor: border,
      textStyle: { color: fg },
    },
    legend: { textStyle: { color: muted } },
    xAxis: {
      axisLine: { lineStyle: { color: border } },
      axisLabel: { color: muted },
      splitLine: { lineStyle: { color: border } },
    },
    yAxis: {
      axisLine: { lineStyle: { color: border } },
      axisLabel: { color: muted },
      splitLine: { lineStyle: { color: border } },
    },
    ...props.option,
  };
}

function render() {
  if (!chartElement.value) return;
  if (!chart) {
    chart = init(chartElement.value, undefined, { renderer: "canvas" });
    chart.on("click", (params: unknown) => emit("chartClick", params));
  }
  chart.setOption(themedOption(), { notMerge: true });
  if (props.loading) chart.showLoading("default", { text: "Loading", color: cssColor("--accent", "#6e9ff8") });
  else chart.hideLoading();
}

function resize() {
  chart?.resize();
}

function downloadPng(filename = "agent-observatory-insight.png") {
  if (!chart) return;
  const anchor = document.createElement("a");
  anchor.download = filename;
  anchor.href = chart.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: cssColor("--bg", "#0d0f0e") });
  anchor.click();
}

defineExpose({ downloadPng, resize });

watch(
  () => [props.option, props.loading, props.empty, props.error],
  () => void nextTick(render),
  { deep: true }
);

onMounted(() => {
  media = window.matchMedia("(prefers-reduced-motion: reduce)");
  media.addEventListener?.("change", render);
  observer = new ResizeObserver(resize);
  if (chartElement.value) observer.observe(chartElement.value);
  render();
});

onBeforeUnmount(() => {
  media?.removeEventListener?.("change", render);
  observer?.disconnect();
  chart?.dispose();
  chart = null;
});
</script>

<style scoped>
.base-chart {
  position: relative;
  min-width: 0;
  height: var(--chart-height);
}
.base-chart__canvas {
  width: 100%;
  height: 100%;
}
.base-chart__state {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: color-mix(in srgb, var(--panel) 88%, transparent);
  color: var(--fg-muted);
  text-align: center;
}
.base-chart__state--error {
  color: var(--danger);
}
</style>
