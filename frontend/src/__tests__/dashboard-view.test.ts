import { createApp, nextTick } from "vue";
import { createPinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const historyApi = vi.hoisted(() => ({ listHistory: vi.fn() }));
const auditApi = vi.hoisted(() => ({ listAudit: vi.fn() }));
const healthApi = vi.hoisted(() => ({ getReadyHealth: vi.fn() }));

vi.mock("../api/history", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/history")>();
  return { ...actual, listHistory: historyApi.listHistory };
});
vi.mock("../api/audit", () => ({ listAudit: auditApi.listAudit }));
vi.mock("../api/health", () => ({ getReadyHealth: healthApi.getReadyHealth }));

import Dashboard from "../views/Dashboard.vue";

async function flush() {
  await Promise.resolve();
  await nextTick();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  await Promise.resolve();
  await nextTick();
}

async function mountDashboard() {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/:pathMatch(.*)*", component: { template: "<div />" } }],
  });
  await router.push("/");
  await router.isReady();
  const app = createApp(Dashboard);
  app.use(createPinia());
  app.use(router);
  app.mount(root);
  await flush();
  return { app, root };
}

beforeEach(() => {
  historyApi.listHistory.mockReset();
  auditApi.listAudit.mockReset();
  healthApi.getReadyHealth.mockReset();
  historyApi.listHistory.mockResolvedValue({ sessions: [], next_cursor: null });
  auditApi.listAudit.mockResolvedValue({ items: [], next_cursor: null });
  healthApi.getReadyHealth.mockResolvedValueOnce({
    status: "ok",
    environment: "test",
    components: {
      postgresql: { status: "up" },
      indexer: { status: "up" },
    },
  });
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("Dashboard health display", () => {
  it("resets service counts when a refresh cannot reach the API", async () => {
    const mounted = await mountDashboard();
    expect(mounted.root.textContent).toContain("2/2 services online");

    healthApi.getReadyHealth.mockRejectedValueOnce(new Error("offline"));
    mounted.root
      .querySelector<HTMLButtonElement>('button[aria-label="Refresh dashboard"]')
      ?.click();
    await flush();

    expect(mounted.root.textContent).toContain("0/2 services online");
    expect(mounted.root.textContent).toContain("offline");
    mounted.app.unmount();
  });
});
