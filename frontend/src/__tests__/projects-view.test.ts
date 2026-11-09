import { createApp, nextTick } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Projects from "../views/Projects.vue";

const projectsApi = vi.hoisted(() => ({
  listProjects: vi.fn(),
}));

vi.mock("../api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/projects")>();
  return { ...actual, ...projectsApi };
});

const payload = {
  items: [
    {
      id: "workspace-1",
      display_name: "Demo",
      path: "/tmp/demo project",
      is_allowed: true,
      workspace_kind: "ephemeral_task",
      workspace_confidence: "high",
      last_active_at: "2026-08-31T10:00:00Z",
      session_count: 2,
      turn_count: 8,
      all_time: {
        input_tokens: 100,
        cached_input_tokens: 20,
        output_tokens: 80,
        reasoning_output_tokens: 10,
        total_tokens: 200,
      },
      selected_range: {
        input_tokens: 40,
        cached_input_tokens: 5,
        output_tokens: 30,
        reasoning_output_tokens: 4,
        total_tokens: 74,
      },
    },
  ],
  total: 1,
  range: "month",
  since: "2026-08-01T00:00:00Z",
  until: "2026-08-31T00:00:00Z",
};

async function mountAt(path = "/projects") {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/:pathMatch(.*)*", component: { template: "<div />" } }],
  });
  await router.push(path);
  await router.isReady();
  const app = createApp(Projects);
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

describe("Projects view", () => {
  beforeEach(() => {
    projectsApi.listProjects.mockReset();
    projectsApi.listProjects.mockResolvedValue(payload);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("renders project path and token metrics, then reloads on range selection", async () => {
    const mounted = await mountAt();
    expect(mounted.root.textContent).toContain("Demo");
    expect(mounted.root.textContent).toContain("/tmp/demo project");
    expect(mounted.root.textContent).toContain("200");
    expect(mounted.root.textContent).toContain("74");
    expect(mounted.root.textContent).toContain("ephemeral");
    expect(projectsApi.listProjects).toHaveBeenCalledWith(
      expect.objectContaining({ range: "month", limit: 50, offset: 0 })
    );

    const week = [...mounted.root.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Last 7 days"
    );
    week?.dispatchEvent(new MouseEvent("click"));
    await flush();
    expect(projectsApi.listProjects).toHaveBeenLastCalledWith(
      expect.objectContaining({ range: "week" })
    );
    expect(mounted.router.currentRoute.value.query).toEqual({ range: "week" });
    mounted.app.unmount();
  });

  it("applies search on Enter and copies the complete directory path", async () => {
    const mounted = await mountAt("/projects?range=all");
    const input = mounted.root.querySelector<HTMLInputElement>('input[aria-label="Search projects"]');
    expect(input).not.toBeNull();
    input!.value = "demo project";
    input!.dispatchEvent(new Event("input"));
    input!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    await flush();
    expect(projectsApi.listProjects).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "demo project", range: "all" })
    );
    expect(mounted.router.currentRoute.value.query).toEqual({ q: "demo project", range: "all" });

    const copy = mounted.root.querySelector<HTMLButtonElement>('button[aria-label="Copy directory path"]');
    copy?.click();
    await flush();
    expect((navigator.clipboard.writeText as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith(
      "/tmp/demo project"
    );
    mounted.app.unmount();
  });

  it("shows a useful empty state", async () => {
    projectsApi.listProjects.mockResolvedValue({ ...payload, items: [], total: 0 });
    const mounted = await mountAt();
    expect(mounted.root.textContent).toContain("No indexed projects match these filters.");
    mounted.app.unmount();
  });

  it("loads additional project pages without losing the first page", async () => {
    projectsApi.listProjects
      .mockResolvedValueOnce({ ...payload, total: 2 })
      .mockResolvedValueOnce({
        ...payload,
        total: 2,
        items: [{ ...payload.items[0], id: "workspace-2", display_name: "Second" }],
      });
    const mounted = await mountAt();
    const loadMore = mounted.root.querySelector<HTMLButtonElement>(".load-more");
    expect(loadMore).not.toBeNull();
    loadMore!.click();
    await flush();
    expect(projectsApi.listProjects).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 1, limit: 50 })
    );
    expect(mounted.root.textContent).toContain("Demo");
    expect(mounted.root.textContent).toContain("Second");
    mounted.app.unmount();
  });

  it("reloads filters when browser navigation changes the route query", async () => {
    const mounted = await mountAt("/projects?range=all&q=first");
    await mounted.router.push("/projects?range=day&q=second");
    await flush();
    expect(projectsApi.listProjects).toHaveBeenLastCalledWith(
      expect.objectContaining({ range: "day", q: "second", offset: 0 }),
    );
    const input = mounted.root.querySelector<HTMLInputElement>(
      'input[aria-label="Search projects"]',
    );
    expect(input?.value).toBe("second");

    await mounted.router.back();
    await flush();
    expect(projectsApi.listProjects).toHaveBeenLastCalledWith(
      expect.objectContaining({ range: "all", q: "first", offset: 0 }),
    );
    expect(input?.value).toBe("first");
    mounted.app.unmount();
  });
});
