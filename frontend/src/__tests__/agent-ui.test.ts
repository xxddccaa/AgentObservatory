import { createApp, defineComponent, nextTick } from "vue";
import { createPinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const historyApi = vi.hoisted(() => ({
  listHistory: vi.fn(),
  listStepGoRuns: vi.fn(),
}));
const insightsApi = vi.hoisted(() => ({
  getInsightsOverview: vi.fn(),
  getInsightsTimeseries: vi.fn(),
  getInsightsLatency: vi.fn(),
  getInsightsTools: vi.fn(),
  getInsightsSkills: vi.fn(),
  getInsightsAgents: vi.fn(),
  getInsightsSources: vi.fn(),
  getInsightsExport: vi.fn(),
  getInsightEvidence: vi.fn(),
  listInsightEvidence: vi.fn(),
}));

vi.mock("../api/history", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/history")>();
  return {
    ...actual,
    listHistory: historyApi.listHistory,
    listStepGoRuns: historyApi.listStepGoRuns,
  };
});
vi.mock("../api/insights", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/insights")>();
  return { ...actual, ...insightsApi };
});
vi.mock("../components/BaseChart.vue", () => ({
  default: defineComponent({
    props: { label: { type: String, required: true } },
    template: '<div class="chart-stub" :aria-label="label"></div>',
  }),
}));

import History from "../views/History.vue";
import Insights from "../views/Insights.vue";

function jsonWindow() {
  return {
    since: "2026-08-20T00:00:00Z",
    until: "2026-08-27T00:00:00Z",
  };
}

async function mountAt(component: object, path: string) {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/:pathMatch(.*)*", component: { template: "<div />" } }],
  });
  await router.push(path);
  await router.isReady();
  const app = createApp(component);
  app.use(createPinia());
  app.use(router);
  app.mount(root);
  await flush();
  return { app, root, router };
}

async function flush() {
  await Promise.resolve();
  await nextTick();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  await Promise.resolve();
  await nextTick();
}

beforeEach(() => {
  localStorage.clear();
  historyApi.listHistory.mockReset();
  historyApi.listStepGoRuns.mockReset();
  Object.values(insightsApi).forEach((mock) => mock.mockReset());
  historyApi.listStepGoRuns.mockResolvedValue({ runs: [], next_cursor: null });
  historyApi.listHistory.mockResolvedValue({
    sessions: [{
      id: "session-1",
      agent: "stepcode",
      thread_id: "thread-1",
      rollout_path: "/tmp/rollout",
      cwd: "/workspace/demo",
      workspace_id: null,
      ai_title: "Multi-agent work",
      model: "gpt-test",
      turn_count: 4,
      total_tokens: 1200,
      is_ongoing: false,
      index_status: "indexed",
      launch_sources: ["stepgo"],
      stepgo_run_count: 1,
      updated_at: "2026-08-27T00:00:00Z",
    }],
    next_cursor: null,
  });
  insightsApi.getInsightsOverview.mockResolvedValue({
    ...jsonWindow(),
    totals: {
      session_count: 3,
      turn_count: 9,
      total_tokens: 5000,
      input_tokens: 0,
      cached_input_tokens: 0,
      output_tokens: 0,
      reasoning_output_tokens: 0,
    },
    quality: { outcomes: {}, validation: {}, confidence: {} },
    by_agent: [
      { agent: "codex", session_count: 2, turn_count: 6, total_tokens: 3200, tool_call_count: 8, failure_count: 1 },
      { agent: "claude", session_count: 1, turn_count: 3, total_tokens: 1800, tool_call_count: 4, failure_count: 0 },
    ],
  });
  insightsApi.getInsightsTimeseries.mockResolvedValue({
    ...jsonWindow(),
    bucket: "day",
    points: [{
      timestamp: "2026-08-26T00:00:00Z",
      session_count: 3,
      turn_count: 9,
      total_tokens: 5000,
      success_count: 6,
      failure_count: 2,
      unknown_count: 1,
    }],
  });
  insightsApi.getInsightsSources.mockResolvedValue({
    sources: [{
      id: "source-1",
      agent: "opencode",
      source_kind: "sqlite",
      source_path: "/tmp/opencode.db",
      source_format: "sqlite",
      parser_version: "1",
      status: "indexed",
      record_count: 10,
      byte_size: 100,
      coverage_started_at: null,
      coverage_ended_at: null,
      backfill_cutoff_at: null,
      last_attempt_at: null,
      last_success_at: null,
      last_error_kind: null,
      last_error: null,
      updated_at: null,
    }],
  });
});

afterEach(() => {
  window.getSelection()?.removeAllRanges();
  document.body.innerHTML = "";
});

describe("history workspace navigation", () => {
  it.each([".group-header", ".group-title", ".group-path"])(
    "opens the workspace through %s and preserves filters when opening a session",
    async (selector) => {
      const mounted = await mountAt(History, "/history?agent=stepcode&q=existing");
      const header = mounted.root.querySelector<HTMLElement>(".group-header")!;
      const rows = mounted.root.querySelector<HTMLElement>(".group-rows")!;
      expect(header.getAttribute("aria-expanded")).toBe("false");
      expect(rows.style.display).toBe("none");

      mounted.root.querySelector<HTMLElement>(selector)!.click();
      await flush();
      expect(header.getAttribute("aria-expanded")).toBe("true");
      expect(rows.style.display).not.toBe("none");

      rows.querySelector<HTMLElement>(".session-title")!.click();
      await flush();
      expect(mounted.router.currentRoute.value.path).toBe("/history/session-1");
      expect(mounted.router.currentRoute.value.query).toEqual({
        agent: "stepcode",
        q: "existing",
      });
      mounted.app.unmount();
    }
  );

  it("does not toggle the workspace when selecting its directory path", async () => {
    const mounted = await mountAt(History, "/history");
    const header = mounted.root.querySelector<HTMLElement>(".group-header")!;
    const path = mounted.root.querySelector<HTMLElement>(".group-path")!;
    const selection = window.getSelection()!;
    const range = document.createRange();
    range.selectNodeContents(path);
    selection.addRange(range);

    path.click();
    await flush();
    expect(selection.toString()).toBe("/workspace/demo");
    expect(header.getAttribute("aria-expanded")).toBe("false");

    selection.removeAllRanges();
    path.click();
    await flush();
    expect(header.getAttribute("aria-expanded")).toBe("true");
    mounted.app.unmount();
  });

  it("toggles with the header keyboard without intercepting the copy button", async () => {
    const mounted = await mountAt(History, "/history");
    const header = mounted.root.querySelector<HTMLElement>(".group-header")!;
    const copy = mounted.root.querySelector<HTMLButtonElement>(".group-copy")!;

    copy.click();
    copy.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await flush();
    expect(header.getAttribute("aria-expanded")).toBe("false");

    header.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await flush();
    expect(header.getAttribute("aria-expanded")).toBe("true");

    header.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    await flush();
    expect(header.getAttribute("aria-expanded")).toBe("false");
    mounted.app.unmount();
  });
});

describe("multi-agent filters and labels", () => {
  it("renders the History selector and session badge, then preserves search in the agent request", async () => {
    const mounted = await mountAt(History, "/history?q=existing");
    const select = mounted.root.querySelector<HTMLSelectElement>(
      'select[aria-label="Filter sessions by agent"]'
    );
    expect(select?.querySelectorAll("option")).toHaveLength(11);
    expect(mounted.root.textContent).toContain("StepCode");

    select!.value = "claude";
    select!.dispatchEvent(new Event("change"));
    await flush();

    expect(historyApi.listHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "existing", agent: "claude", cursor: null })
    );
    expect(mounted.router.currentRoute.value.query).toEqual({
      q: "existing",
      agent: "claude",
    });
    mounted.app.unmount();
  });

  it("renders StepGo host runs, including runs that cannot be linked", async () => {
    historyApi.listStepGoRuns.mockResolvedValue({
      runs: [
        {
          id: "run-linked",
          host_run_id: "host-linked",
          source_path: "/tmp/host-linked.jsonl",
          agent: "pi",
          module: "agent",
          status: "completed",
          link_status: "linked",
          link_reason: "native_session_id",
          linked_agent_session_id: "session-1",
          native_session_id: "thread-1",
          transcript_path: null,
          start_time: "2026-08-27T00:00:00Z",
          end_time: "2026-08-27T00:01:00Z",
          exit_code: 0,
          index_status: "indexed",
          event_count: 5,
          last_error: null,
          session: {
            id: "session-1",
            thread_id: "thread-1",
            display_title: "Multi-agent work",
            ai_title: null,
          },
        },
        {
          id: "run-unlinked",
          host_run_id: "host-unlinked",
          source_path: "/tmp/host-unlinked.jsonl",
          agent: "pi",
          module: "agent",
          status: "running",
          link_status: "unlinked",
          link_reason: "native_session_not_indexed",
          linked_agent_session_id: null,
          native_session_id: "pending-native",
          transcript_path: null,
          start_time: null,
          end_time: null,
          exit_code: null,
          index_status: "indexed",
          event_count: 2,
          last_error: null,
          session: null,
        },
      ],
      next_cursor: "stepgo-next-page",
    });
    const mounted = await mountAt(History, "/history");
    const select = mounted.root.querySelector<HTMLSelectElement>(
      'select[aria-label="Filter sessions by agent"]'
    );
    select!.value = "stepgo";
    select!.dispatchEvent(new Event("change"));
    await flush();

    expect(historyApi.listStepGoRuns).toHaveBeenCalledWith({ limit: 100 });
    expect(mounted.root.textContent).toContain("StepGo host runs");
    expect(mounted.root.textContent).toContain("1 unlinked / ambiguous");
    expect(mounted.root.textContent).toContain("native session not indexed");
    expect(mounted.root.textContent).toContain("via StepGo");
    expect(mounted.root.textContent).toContain("Multi-agent work");
    historyApi.listStepGoRuns.mockResolvedValueOnce({ runs: [], next_cursor: null });
    const more = [...mounted.root.querySelectorAll("button")]
      .find((button) => button.textContent?.trim() === "Load more host runs");
    expect(more).toBeDefined();
    more!.dispatchEvent(new MouseEvent("click"));
    await flush();
    expect(historyApi.listStepGoRuns).toHaveBeenLastCalledWith({ limit: 100, cursor: "stepgo-next-page" });
    expect(mounted.root.textContent).toContain("2 runs");
    mounted.app.unmount();
  });

  it("renders all-agent comparison metrics and source agent labels", async () => {
    const mounted = await mountAt(Insights, "/insights");
    const select = mounted.root.querySelector<HTMLSelectElement>(
      'select[aria-label="Filter insights by agent"]'
    );
    expect(select?.querySelectorAll("option")).toHaveLength(11);
    expect(mounted.root.textContent).toContain("Cross-agent comparison");
    expect(mounted.root.textContent).toContain("Codex");
    expect(mounted.root.textContent).toContain("Claude");
    expect(mounted.root.textContent).toContain("3,200");
    expect(
      mounted.root.querySelector('[aria-label^="Cross-agent session"]')
    ).not.toBeNull();

    const sourcesTab = [...mounted.root.querySelectorAll("button")]
      .find((button) => button.textContent?.trim() === "Data Sources");
    sourcesTab?.dispatchEvent(new MouseEvent("click"));
    await flush();
    expect(mounted.root.textContent).toContain("OpenCode");

    select!.value = "stepcode";
    select!.dispatchEvent(new Event("change"));
    await flush();
    expect(insightsApi.getInsightsSources).toHaveBeenLastCalledWith(
      expect.objectContaining({ range: "7d", agent: "stepcode" })
    );
    expect(mounted.root.textContent).not.toContain("Cross-agent comparison");
    mounted.app.unmount();
  });

  it("clears old insight values while a custom range is waiting to be applied", async () => {
    const mounted = await mountAt(Insights, "/insights");
    expect(mounted.root.textContent).toContain("5,000");

    const custom = [...mounted.root.querySelectorAll("button")]
      .find((button) => button.textContent?.trim() === "Custom");
    custom?.dispatchEvent(new MouseEvent("click"));
    await flush();

    expect(mounted.root.textContent).toContain(
      "Choose a valid start and end time, then apply."
    );
    expect(mounted.root.textContent).not.toContain("5,000");
    mounted.app.unmount();
  });

  it("does not leave stale insight values visible after a refresh fails", async () => {
    const mounted = await mountAt(Insights, "/insights");
    expect(mounted.root.textContent).toContain("5,000");
    insightsApi.getInsightsOverview.mockRejectedValueOnce(new Error("refresh failed"));

    const refresh = [...mounted.root.querySelectorAll("button")]
      .find((button) => button.textContent?.trim() === "Refresh");
    refresh?.dispatchEvent(new MouseEvent("click"));
    await flush();

    expect(mounted.root.textContent).toContain("refresh failed");
    expect(mounted.root.textContent).not.toContain("5,000");
    mounted.app.unmount();
  });
});
