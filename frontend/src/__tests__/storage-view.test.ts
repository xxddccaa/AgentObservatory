import { createApp, nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const retentionApi = vi.hoisted(() => ({
  getRetentionOverview: vi.fn(),
  getRetentionCatalog: vi.fn(),
  previewRetention: vi.fn(),
  createRetentionRun: vi.fn(),
  listRetentionRuns: vi.fn(),
  getRetentionRun: vi.fn(),
}));

vi.mock("../api/retention", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/retention")>();
  return { ...actual, ...retentionApi };
});

import { useAuthStore } from "../stores/auth";
import Storage from "../views/Storage.vue";

const overview = {
  generated_at: "2026-09-11T12:00:00Z",
  cached: false,
  enabled: true,
  min_hours: 24,
  large_run_bytes: 5 * 1024 ** 3,
  protected_roots: [],
  entries: [
    {
      id: "codex.sessions",
      agent: "codex",
      category: "sessions",
      label: "Codex rollout sessions",
      root: "/root/.codex/sessions",
      risk: "history",
      unit: "file",
      description: "Rollout transcripts.",
      status: "ok",
      files: 815,
      bytes: 2.2 * 1024 ** 3,
      newest_mtime: 1789000000,
    },
    {
      id: "codex.logs",
      agent: "codex",
      category: "logs",
      label: "Codex service logs",
      root: "/root/.codex",
      risk: "regenerable",
      unit: "file",
      description: "Diagnostic log database.",
      status: "ok",
      files: 3,
      bytes: 310 * 1024 ** 2,
      newest_mtime: 1789000000,
    },
  ],
  index: {
    database_bytes: 7_710 * 1024 ** 2,
    database_pretty: "7710 MB",
    tables: [{ name: "evidence_records", bytes: 3_770 * 1024 ** 2, estimated_rows: 120 }],
  },
};

const preview = {
  floor_at: "2026-09-04T12:00:00Z",
  generated_at: "2026-09-11T12:00:00Z",
  keep_days: 7,
  totals: { bytes: 2_000 * 1024 ** 2, files: 710, entries: 1, sessions: 700, sources: 700 },
  by_agent: {},
  by_category: {},
  by_risk: {},
  index: { sessions: 700, sources: 700, sessions_by_entry: {} },
  entries: [
    {
      id: "codex.sessions",
      agent: "codex",
      category: "sessions",
      label: "Codex rollout sessions",
      root: "/root/.codex/sessions",
      risk: "history",
      unit: "file",
      description: "Rollout transcripts.",
      status: "planned",
      files: 710,
      bytes: 2_000 * 1024 ** 2,
    },
    {
      id: "codex.logs",
      agent: "codex",
      category: "logs",
      label: "Codex service logs",
      root: "/root/.codex",
      risk: "regenerable",
      unit: "file",
      description: "Diagnostic log database.",
      status: "planned",
      files: 0,
      bytes: 0,
    },
  ],
  skipped: [
    { entry_id: "codex.sessions", agent: "codex", reason: "in_use", files: 44, bytes: 130 * 1024 ** 2 },
  ],
  truncated: false,
  plan_token: "signed-plan",
  selection: { keep_days: 7 },
  protected_roots: [],
  large_run_bytes: 5 * 1024 ** 3,
  items: [],
  items_truncated: false,
};

async function flush() {
  await Promise.resolve();
  await nextTick();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  await nextTick();
}

async function mountView(roles: string[] = ["admin"]) {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const pinia = createPinia();
  setActivePinia(pinia);
  const auth = useAuthStore();
  auth.$patch({ status: "authed", principal: { id: "admin", username: "admin", roles } });
  const app = createApp(Storage);
  app.use(pinia);
  app.mount(root);
  await flush();
  return { app, root };
}

function button(root: HTMLElement, label: string): HTMLButtonElement | undefined {
  return [...root.querySelectorAll("button")].find((item) =>
    item.textContent?.includes(label),
  );
}

describe("Storage view", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    retentionApi.getRetentionOverview.mockResolvedValue(overview);
    retentionApi.listRetentionRuns.mockResolvedValue({ runs: [] });
    retentionApi.previewRetention.mockResolvedValue(preview);
    retentionApi.createRetentionRun.mockResolvedValue({
      run: {
        id: "ret_1",
        command_id: 12,
        requested_by: "admin",
        scope: "raw+index",
        status: "pending",
        keep_days: 7,
        floor_at: preview.floor_at,
        selection: { keep_days: 7 },
        plan: {},
        result: {},
        bytes_freed: 0,
        files_deleted: 0,
        sessions_deleted: 0,
        started_at: null,
        finished_at: null,
        error_kind: null,
        error_message: null,
        created_at: "2026-09-11T12:01:00Z",
      },
      command_id: 12,
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("lists catalogued roots with their size and risk", async () => {
    const mounted = await mountView();
    expect(mounted.root.textContent).toContain("Codex rollout sessions");
    expect(mounted.root.textContent).toContain("2.2 GB");
    expect(mounted.root.textContent).toContain("历史会话");
    expect(mounted.root.textContent).toContain("7710 MB");
    mounted.app.unmount();
  });

  it("previews the selected ids and the retention window", async () => {
    const mounted = await mountView();
    const previewButton = button(mounted.root, "预览清理");
    previewButton?.click();
    await flush();
    expect(retentionApi.previewRetention).toHaveBeenCalledWith({
      keep_days: 7,
      entry_ids: ["codex.sessions"],
      include_raw: true,
      include_index: true,
    });
    expect(mounted.root.textContent).toContain("立即清理 2.0 GB");
    expect(mounted.root.textContent).toContain("仍在保留窗口内");
    mounted.app.unmount();
  });

  it("queues an audited run for admins and reports the command id", async () => {
    const mounted = await mountView();
    button(mounted.root, "预览清理")?.click();
    await flush();
    button(mounted.root, "立即清理")?.click();
    await flush();
    expect(retentionApi.createRetentionRun).toHaveBeenCalledWith({
      plan_token: "signed-plan",
      confirm: true,
      acknowledge_large: false,
    });
    expect(mounted.root.textContent).toContain("系统命令 #12");
    mounted.app.unmount();
  });

  it("updates the sizes by itself once the queued run is applied", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const mounted = await mountView();
      button(mounted.root, "预览清理")?.click();
      await flush();
      button(mounted.root, "立即清理")?.click();
      await flush();

      const created = await retentionApi.createRetentionRun.mock.results[0].value;
      retentionApi.getRetentionRun.mockResolvedValue({
        ...created.run,
        status: "applied",
        bytes_freed: 512 * 1024 ** 2,
        files_deleted: 42,
        finished_at: "2026-09-11T12:02:00Z",
      });
      retentionApi.getRetentionOverview.mockClear();

      await vi.advanceTimersByTimeAsync(2000);
      await flush();

      expect(retentionApi.getRetentionRun).toHaveBeenCalledWith("ret_1");
      expect(retentionApi.getRetentionOverview).toHaveBeenCalledWith(true);
      expect(mounted.root.textContent).toContain("已完成");
      expect(mounted.root.textContent).toContain("占用数字已自动更新");
      mounted.app.unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps re-scanning while a long purge is still executing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const mounted = await mountView();
      button(mounted.root, "预览清理")?.click();
      await flush();
      button(mounted.root, "立即清理")?.click();
      await flush();

      const created = await retentionApi.createRetentionRun.mock.results[0].value;
      retentionApi.getRetentionRun.mockResolvedValue({
        ...created.run,
        status: "running",
        started_at: "2026-09-11T12:01:00Z",
      });
      retentionApi.getRetentionOverview.mockClear();

      await vi.advanceTimersByTimeAsync(16_000);
      await flush();

      expect(retentionApi.getRetentionOverview).toHaveBeenCalledWith(true);
      expect(mounted.root.textContent).toContain("已运行");
      mounted.app.unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it("re-reads the overview when the tab comes back to the foreground", async () => {
    const mounted = await mountView();
    retentionApi.getRetentionOverview.mockClear();
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(retentionApi.getRetentionOverview).toHaveBeenCalledTimes(1);
    mounted.app.unmount();
  });

  it("labels the scan time and flags cached payloads", async () => {
    retentionApi.getRetentionOverview.mockResolvedValue({ ...overview, cached: true });
    const mounted = await mountView();
    expect(mounted.root.textContent).toContain("数据时间");
    expect(mounted.root.textContent).toContain("服务端缓存");
    mounted.app.unmount();
  });

  it("never offers deletion to operators", async () => {
    const mounted = await mountView(["operator"]);
    button(mounted.root, "预览清理")?.click();
    await flush();
    expect(retentionApi.previewRetention).toHaveBeenCalled();
    expect(button(mounted.root, "立即清理")).toBeUndefined();
    expect(mounted.root.textContent).toContain("只有 admin 可以发起清理");
    mounted.app.unmount();
  });
});
