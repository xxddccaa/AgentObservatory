import { createApp, nextTick } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Guide from "../views/Guide.vue";

async function mountGuide() {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/guide", component: Guide }],
  });
  await router.push("/guide");
  await router.isReady();
  const app = createApp(Guide);
  app.use(router);
  app.mount(root);
  await nextTick();
  return { app, root };
}

describe("Guide view", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("renders the Chinese usage sections and deployment references", async () => {
    const mounted = await mountGuide();
    expect(mounted.root.textContent).toContain("使用方法");
    expect(mounted.root.textContent).toContain("History：浏览会话");
    expect(mounted.root.textContent).toContain("Subagent 默认隐藏");
    expect(mounted.root.textContent).toContain("本地部署与运维");
    expect(mounted.root.textContent).toContain("uv run agent-observatory up");
    expect(mounted.root.querySelectorAll(".command-card")).toHaveLength(5);
    expect(mounted.root.textContent).toContain("Storage：清理旧数据");
    expect(mounted.root.querySelectorAll(".guide-index a")).toHaveLength(11);
    mounted.app.unmount();
  });

  it("copies a deployment command and exposes the copy action accessibly", async () => {
    const mounted = await mountGuide();
    const button = mounted.root.querySelector<HTMLButtonElement>(
      'button[aria-label="复制后台启动"]',
    );
    expect(button).not.toBeNull();
    button!.dispatchEvent(new MouseEvent("click"));
    await Promise.resolve();
    await nextTick();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "uv run agent-observatory up",
    );
    expect(button!.textContent).toContain("已复制");
    mounted.app.unmount();
  });
});
