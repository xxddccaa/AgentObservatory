import { createApp, defineComponent, nextTick } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProjectDetail from "../views/ProjectDetail.vue";
import { ApiError } from "../api/client";

const projectsApi = vi.hoisted(() => ({
  getProject: vi.fn(),
  getProjectTimeseries: vi.fn(),
  listProjectSessions: vi.fn(),
}));

vi.mock("../api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/projects")>();
  return { ...actual, ...projectsApi };
});

vi.mock("../components/BaseChart.vue", () => ({
  default: defineComponent({
    props: {
      label: { type: String, required: true },
      loading: { type: Boolean, default: false },
    },
    template: '<div class="chart-stub" role="img" :aria-label="label" :data-loading="loading"></div>',
  }),
}));

const detailPayload = {
  project: {
    id: "workspace-1",
    display_name: "Demo",
    path: "/tmp/demo",
    is_allowed: true,
    workspace_kind: "ephemeral_task",
    workspace_confidence: "high",
    first_active_at: "2026-08-01T00:00:00Z",
    last_active_at: "2026-08-31T10:00:00Z",
  },
  totals: {
    all_time: { input_tokens: 100, cached_input_tokens: 10, output_tokens: 80, reasoning_output_tokens: 5, total_tokens: 195 },
    day: { input_tokens: 10, cached_input_tokens: 1, output_tokens: 8, reasoning_output_tokens: 0, total_tokens: 18 },
    week: { input_tokens: 40, cached_input_tokens: 4, output_tokens: 30, reasoning_output_tokens: 1, total_tokens: 71 },
    month: { input_tokens: 70, cached_input_tokens: 7, output_tokens: 55, reasoning_output_tokens: 3, total_tokens: 128 },
  },
  counts: { session_count: 2, turn_count: 8 },
  by_agent: [{ agent: "codex", session_count: 2, turn_count: 8, total_tokens: 195 }],
  by_model: [{ model: "gpt-test", session_count: 2, turn_count: 8, total_tokens: 195 }],
};

const timeseriesPayload = {
  since: "2026-08-24T00:00:00Z",
  until: "2026-08-31T00:00:00Z",
  bucket: "day",
  points: [
    { timestamp: "2026-08-30T00:00:00Z", session_count: 1, turn_count: 4, total_tokens: 100, input_tokens: 50, output_tokens: 50 },
  ],
};

async function mountAt(path = "/projects/workspace-1") {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/projects/:id", component: ProjectDetail }, { path: "/:pathMatch(.*)*", component: { template: "<div />" } }],
  });
  await router.push(path);
  await router.isReady();
  const app = createApp(ProjectDetail);
  app.use(router);
  app.mount(root);
  await flush();
  return { app, root, router };
}

async function flush() {
  await Promise.resolve();
  await nextTick();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  await nextTick();
}

describe("Project detail view", () => {
  beforeEach(() => {
    projectsApi.getProject.mockReset().mockResolvedValue(detailPayload);
    projectsApi.getProjectTimeseries.mockReset().mockResolvedValue(timeseriesPayload);
    projectsApi.listProjectSessions.mockReset().mockResolvedValue({
      sessions: [{
        id: "session-1",
        thread_id: "thread-1",
        rollout_path: "/tmp/rollout",
        cwd: "/tmp/demo",
        workspace_id: "workspace-1",
        ai_title: "Demo session",
        model: "gpt-test",
        turn_count: 4,
        total_tokens: 100,
        is_ongoing: false,
        index_status: "indexed",
        updated_at: "2026-08-31T09:00:00Z",
        activity_at: "2026-08-31T09:30:00Z",
        workspace_kind: "ephemeral_task",
        workspace_confidence: "high",
      }],
      next_cursor: null,
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("renders totals, splits, trend fallback table, and history links", async () => {
    const mounted = await mountAt();
    expect(mounted.root.textContent).toContain("Demo");
    expect(mounted.root.textContent).toContain("/tmp/demo");
    expect(mounted.root.textContent).toContain("195");
    expect(mounted.root.textContent).toContain("Last 24 hours");
    expect(mounted.root.textContent).toContain("codex");
    expect(mounted.root.textContent).toContain("gpt-test");
    expect(mounted.root.textContent).toContain("Demo session");
    expect(mounted.root.textContent).toContain("ephemeral");
    expect(mounted.root.querySelector('a[href="/history/session-1"]')).not.toBeNull();
    expect(mounted.root.querySelector('[aria-label="Project token usage over time"]')).not.toBeNull();
    expect(mounted.root.textContent).toMatch(/8\/30\/2026|30\/08\/2026|2026-08-30/);

    const month = [...mounted.root.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "30d"
    );
    month?.dispatchEvent(new MouseEvent("click"));
    await flush();
    expect(projectsApi.getProjectTimeseries).toHaveBeenLastCalledWith("workspace-1", "month");
    mounted.app.unmount();
  });

  it("renders a dedicated not-found state for 404 responses", async () => {
    projectsApi.getProject.mockRejectedValue(new ApiError(404, "missing"));
    const mounted = await mountAt();
    expect(mounted.root.textContent).toContain("Project not found.");
    expect(mounted.root.textContent).toContain("Back to projects");
    mounted.app.unmount();
  });

  it("loads additional project sessions using the returned cursor", async () => {
    projectsApi.listProjectSessions
      .mockResolvedValueOnce({
        sessions: [{
          id: "session-1",
          thread_id: "thread-1",
          rollout_path: "/tmp/rollout",
          cwd: "/tmp/demo",
          workspace_id: "workspace-1",
          ai_title: "Demo session",
          model: "gpt-test",
          turn_count: 4,
          total_tokens: 100,
          is_ongoing: false,
          index_status: "indexed",
          updated_at: "2026-08-31T09:00:00Z",
        }],
        next_cursor: "cursor-1",
      })
      .mockResolvedValueOnce({
        sessions: [{
          id: "session-2",
          thread_id: "thread-2",
          rollout_path: "/tmp/rollout-2",
          cwd: "/tmp/demo",
          workspace_id: "workspace-1",
          ai_title: "Second session",
          model: "gpt-test",
          turn_count: 2,
          total_tokens: 50,
          is_ongoing: false,
          index_status: "indexed",
          updated_at: "2026-08-31T08:00:00Z",
        }],
        next_cursor: null,
      });
    const mounted = await mountAt();
    const loadMore = mounted.root.querySelector<HTMLButtonElement>(".load-more");
    expect(loadMore).not.toBeNull();
    loadMore!.click();
    await flush();
    expect(projectsApi.listProjectSessions).toHaveBeenLastCalledWith(
      "workspace-1",
      { limit: 100, cursor: "cursor-1" }
    );
    expect(mounted.root.textContent).toContain("Second session");
    mounted.app.unmount();
  });

  it("keeps trend range in browser history and reloads on Back/Forward", async () => {
    const mounted = await mountAt("/projects/workspace-1?trend=day");
    expect(projectsApi.getProjectTimeseries).toHaveBeenLastCalledWith("workspace-1", "day");

    await mounted.router.push("/projects/workspace-1?trend=month");
    await flush();
    expect(projectsApi.getProjectTimeseries).toHaveBeenLastCalledWith("workspace-1", "month");

    await mounted.router.back();
    await flush();
    expect(projectsApi.getProjectTimeseries).toHaveBeenLastCalledWith("workspace-1", "day");
    mounted.app.unmount();
  });
});
