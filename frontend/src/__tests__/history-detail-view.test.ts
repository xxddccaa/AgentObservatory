import { createApp, nextTick } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderMarkdown } from "../markdown/render";
import type {
  SessionMarkdownRenderer,
} from "../markdown/session";

const historyApi = vi.hoisted(() => ({
  getHistory: vi.fn(),
  getMessages: vi.fn(),
  getEvents: vi.fn(),
  getTools: vi.fn(),
  exportSession: vi.fn(),
  searchSession: vi.fn(),
}));
const insightsApi = vi.hoisted(() => ({
  getInsightEvidence: vi.fn(),
  getSessionTimeline: vi.fn(),
}));
const sessionMarkdown = vi.hoisted(() => ({
  createSessionMarkdownRenderer: vi.fn(),
}));

vi.mock("../api/history", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/history")>();
  return { ...actual, ...historyApi };
});
vi.mock("../api/insights", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/insights")>();
  return { ...actual, ...insightsApi };
});
vi.mock("../markdown/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../markdown/session")>();
  return { ...actual, ...sessionMarkdown };
});

import HistoryDetail from "../views/HistoryDetail.vue";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function message(overrides: Record<string, unknown>) {
  return {
    id: "message-1",
    role: "assistant",
    channel: "final",
    text: "",
    markdown_text: "",
    copy_text: "",
    sequence: 0,
    timestamp: "2026-08-27T10:00:00Z",
    turn_id: "turn-1",
    ...overrides,
  };
}

async function flush() {
  await Promise.resolve();
  await nextTick();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  await nextTick();
}

async function mountDetail() {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/history", component: { template: "<div />" } },
      {
        path: "/history/:id",
        name: "history-detail",
        component: HistoryDetail,
        props: true,
      },
    ],
  });
  await router.push("/history/session-1");
  await router.isReady();
  const app = createApp({ template: "<router-view />" });
  app.use(router);
  app.mount(root);
  await flush();
  return { app, root };
}

function asyncRenderer(htmlMarker = "data-session-rendered"): SessionMarkdownRenderer & {
  clearCache: ReturnType<typeof vi.fn>;
} {
  return {
    render: (source, highlightTerm) =>
      `<div ${htmlMarker}="true">${renderMarkdown(source, highlightTerm)}</div>`,
    clearCache: vi.fn(),
  };
}

beforeEach(() => {
  historyApi.getHistory.mockReset();
  historyApi.getMessages.mockReset();
  sessionMarkdown.createSessionMarkdownRenderer.mockReset();
  historyApi.getHistory.mockResolvedValue({
    id: "session-1",
    thread_id: "session-1",
    rollout_path: "/tmp/rollout.jsonl",
    cwd: "/workspace/demo",
    model: "e2e-model",
    turn_count: 1,
    total_tokens: 10,
    is_ongoing: false,
    stepgo_runs: [],
  });
  historyApi.getMessages.mockResolvedValue({
    messages: [
      message({
        id: "user-1",
        role: "user",
        text: "Give me **colored** Markdown",
        markdown_text: "Give me **colored** Markdown",
      }),
      message({
        id: "assistant-1",
        role: "assistant",
        sequence: 1,
        text: "## Rich result\n\n- one\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n```javascript\nconst answer = \"yes\";\n```\n\n<script>alert(1)</script>",
        markdown_text: "## Rich result\n\n- one\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n```javascript\nconst answer = \"yes\";\n```\n\n<script>alert(1)</script>",
      }),
    ],
  });
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("HistoryDetail Markdown rendering", () => {
  it("shows safe Markdown first and upgrades it after the session renderer initializes", async () => {
    const renderer = asyncRenderer();
    const pending = deferred<SessionMarkdownRenderer>();
    sessionMarkdown.createSessionMarkdownRenderer.mockReturnValue(pending.promise);
    const mounted = await mountDetail();

    const assistant = mounted.root.querySelector<HTMLElement>('.message[data-role="assistant"]')!;
    expect(assistant.querySelector("h2")?.textContent).toBe("Rich result");
    expect(assistant.querySelector("table")).not.toBeNull();
    expect(assistant.querySelector("[data-session-rendered]")).toBeNull();
    expect(assistant.innerHTML).not.toContain("<script>");
    expect(assistant.innerHTML).toContain("&lt;script&gt;");
    expect(assistant.querySelector(".message-body")?.className).toContain("markdown-body");

    pending.resolve(renderer);
    await flush();

    expect(assistant.querySelector("[data-session-rendered]")).not.toBeNull();
    expect(assistant.querySelector("pre code")?.textContent).toContain('const answer = "yes";');
    mounted.app.unmount();
  });

  it("clears the cache and ignores a renderer that resolves after leaving the page", async () => {
    const renderer = asyncRenderer();
    const pending = deferred<SessionMarkdownRenderer>();
    sessionMarkdown.createSessionMarkdownRenderer.mockReturnValue(pending.promise);
    const mounted = await mountDetail();

    mounted.app.unmount();
    pending.resolve(renderer);
    await flush();

    expect(renderer.clearCache).toHaveBeenCalledOnce();
  });

  it("keeps the baseline renderer when session rendering fails", async () => {
    sessionMarkdown.createSessionMarkdownRenderer.mockRejectedValue(new Error("load failed"));
    const mounted = await mountDetail();

    const assistant = mounted.root.querySelector<HTMLElement>('.message[data-role="assistant"]')!;
    expect(assistant.querySelector("h2")?.textContent).toBe("Rich result");
    expect(assistant.querySelector("[data-session-rendered]")).toBeNull();
    expect(assistant.innerHTML).not.toContain("<script>");
    mounted.app.unmount();
  });

  it("renders reasoning summaries through the same Markdown path with the reasoning treatment", async () => {
    sessionMarkdown.createSessionMarkdownRenderer.mockResolvedValue(asyncRenderer());
    historyApi.getMessages.mockResolvedValue({
      messages: [
        message({
          id: "reasoning-1",
          channel: "reasoning",
          text: "### Thought\n\n- **checked** the plan",
          markdown_text: "### Thought\n\n- **checked** the plan",
        }),
      ],
    });
    const mounted = await mountDetail();

    const filter = [...mounted.root.querySelectorAll<HTMLButtonElement>("button")].find(
      (button) => button.textContent?.trim() === "Reasoning summary"
    );
    filter?.click();
    await flush();

    const reasoning = mounted.root.querySelector<HTMLElement>(".message--reasoning")!;
    expect(reasoning.querySelector("h3")?.textContent).toBe("Thought");
    expect(reasoning.querySelector(".message-body strong")?.textContent).toBe("checked");
    expect(reasoning.querySelector(".message-body")?.classList.contains("reasoning-text")).toBe(true);
    expect(reasoning.querySelector("[data-session-rendered]")).not.toBeNull();
    mounted.app.unmount();
  });
});
