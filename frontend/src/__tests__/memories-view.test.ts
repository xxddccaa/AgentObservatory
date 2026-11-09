import { createApp, nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const memoriesApi = vi.hoisted(() => ({
  listMemories: vi.fn(),
  getMemory: vi.fn(),
  getMemorySources: vi.fn(),
  getMemoryContent: vi.fn(),
  exportMemories: vi.fn(),
}));

vi.mock("../api/memories", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/memories")>();
  return { ...actual, ...memoriesApi };
});

import { useAuthStore } from "../stores/auth";
import Memories from "../views/Memories.vue";

const entry = {
  id: "memory-one",
  agent: "claude",
  source_kind: "claude_memory_files",
  source_path: "/root/.claude/projects/demo/memory/deploy.md",
  thread_id: null,
  scope: "project",
  title: "Deploy notes",
  summary: "Use the audited restart command.",
  content_bytes: 128,
  source_updated_at: "2026-09-01T12:00:00Z",
  last_seen_at: "2026-09-02T12:00:00Z",
  is_present: true,
  sensitivity: "sensitive",
  workspace: { id: "w1", name: "Demo", path: "/workspace/demo" },
  project_key: "demo",
  is_index: false,
};

async function flush() {
  await Promise.resolve();
  await nextTick();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  await nextTick();
}

async function mountView() {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const pinia = createPinia();
  setActivePinia(pinia);
  const auth = useAuthStore();
  auth.$patch({
    status: "authed",
    principal: {
      id: "admin",
      username: "admin",
      roles: ["admin"],
    },
  });
  const app = createApp(Memories);
  app.use(pinia);
  app.mount(root);
  await flush();
  return { app, root };
}

describe("Memories view", () => {
  beforeEach(() => {
    memoriesApi.listMemories.mockReset();
    memoriesApi.getMemory.mockReset();
    memoriesApi.getMemorySources.mockReset();
    memoriesApi.getMemoryContent.mockReset();
    memoriesApi.exportMemories.mockReset();
    memoriesApi.listMemories.mockResolvedValue({
      entries: [entry],
      next_cursor: null,
    });
    memoriesApi.getMemorySources.mockResolvedValue({
      sources: [
        {
          agent: "claude",
          source_kind: "claude_memory_files",
          status: "indexed",
          source_path: "/root/.claude/projects",
          record_count: 1,
          last_success_at: "2026-09-02T12:00:00Z",
        },
        {
          agent: "dsh",
          source_kind: null,
          status: "unsupported",
          source_path: null,
          record_count: 0,
          last_success_at: null,
        },
      ],
    });
    memoriesApi.getMemory.mockResolvedValue({
      ...entry,
      content: "api_key=[REDACTED]",
      content_redacted: true,
      content_hash: "hash-one",
      source_record_id: "demo/memory/deploy.md",
      metadata: {},
    });
    memoriesApi.getMemoryContent.mockResolvedValue({
      id: entry.id,
      format: "raw",
      content: "api_key=real-value",
      content_hash: "hash-one",
    });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("renders source status, memory metadata, and a redacted detail", async () => {
    const mounted = await mountView();
    expect(mounted.root.textContent).toContain("Memories");
    expect(mounted.root.textContent).toContain("Deploy notes");
    expect(mounted.root.textContent).toContain("not detected");
    expect(mounted.root.textContent).toContain("Sensitive values are hidden");
    expect(memoriesApi.getMemory).toHaveBeenCalledWith("memory-one");
    mounted.app.unmount();
  });

  it("reveals and copies full content through audited endpoints", async () => {
    const mounted = await mountView();
    const reveal = [...mounted.root.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Reveal",
    );
    reveal?.click();
    await flush();
    expect(memoriesApi.getMemoryContent).toHaveBeenCalledWith(
      "memory-one",
      "raw",
      "reveal",
    );
    expect(mounted.root.textContent).toContain("Original sensitive content is visible");

    const copy = [...mounted.root.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Copy raw",
    );
    copy?.click();
    await flush();
    expect(memoriesApi.getMemoryContent).toHaveBeenCalledWith(
      "memory-one",
      "raw",
      "copy",
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("api_key=real-value");
    mounted.app.unmount();
  });
});
