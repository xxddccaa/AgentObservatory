import { expect, test } from "@playwright/test";
import { login } from "./util";

test.beforeEach(async ({ page }) => {
  await page.context().clearCookies();
  await login(page);
});

test("dashboard shows real data, not scaffold placeholders", async ({ page }) => {
  // The old scaffold hardcoded these; they must be gone.
  await expect(page.getByText("Terminal ready")).toHaveCount(0);
  await expect(page.getByText("Open terminal workspace")).toHaveCount(0);
  // Real, data-driven tiles.
  await expect(page.getByText("History indexed")).toBeVisible();
  await expect(page.getByText("Recent activity")).toBeVisible();
});

test("insights renders aggregate charts and the local data catalog", async ({ page }) => {
  await page.goto("/insights");
  await expect(page.getByRole("heading", { name: "Insights" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Activity and token volume" })).toBeVisible();
  const chart = page.locator(".base-chart canvas").first();
  await expect(chart).toBeVisible();
  await expect
    .poll(() =>
      chart.evaluate((canvas: HTMLCanvasElement) => {
        const context = canvas.getContext("2d");
        if (!context || canvas.width === 0 || canvas.height === 0) return false;
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
        for (let index = 3; index < pixels.length; index += 16) {
          if (pixels[index] !== 0) return true;
        }
        return false;
      })
    )
    .toBe(true);

  await page.getByRole("tab", { name: "Performance" }).click();
  await expect(page.getByRole("heading", { name: "Latency percentiles" })).toBeVisible();
  await page.getByRole("tab", { name: "Data Sources" }).click();
  const physicalSource = page.getByRole("row").filter({
    has: page.getByText("codex_jsonl", { exact: true }),
  });
  await expect(physicalSource).toBeVisible();
  await expect(physicalSource.getByText("rollout-playwright.jsonl")).toBeVisible();
});

test("memories exposes portable CLI entries and source status", async ({ page }) => {
  await page.goto("/memories");
  await expect(page.getByRole("heading", { name: "Memories" })).toBeVisible();
  await expect(page.getByText("playwright-memory")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Claude browser memory" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy raw" })).toBeVisible();
  await expect(page.getByLabel("Memory sources").getByText("DSH")).toBeVisible();
});

test("desktop app shell keeps navigation in a stable side rail", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/");

  const rail = await page.locator(".shell__rail").boundingBox();
  const content = await page.locator(".shell__content").boundingBox();
  expect(rail?.x).toBe(0);
  expect(rail?.width).toBe(224);
  expect(rail?.height).toBe(800);
  expect(content?.x).toBe(224);
});

test("history opens from the workspace path with messages, tools, and raw events", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  const healthResponse = await page.request.get("/api/health");
  expect(healthResponse.ok()).toBe(true);
  expect(await healthResponse.json()).toMatchObject({
    status: "ok",
    postgresql: true,
    workers: { indexer: true },
  });
  await page.goto("/history?agent=codex&q=playwright");
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
  await page.locator(".group-path").first().click();
  await expect(page.locator(".group-header").first()).toHaveAttribute("aria-expanded", "true");
  const session = page.locator(".group-rows .row").first();
  await expect(session).toBeVisible();
  const destination = await session.getAttribute("href");
  await session.locator(".session-title").click();
  await expect(page).toHaveURL(new URL(destination!, page.url()).toString());
  await expect(page.getByText("read-only")).toBeVisible();
  await expect(page.getByRole("button", { name: "Conversation" })).toBeVisible();
  const userMessage = page.locator('.message[data-role="user"]').first();
  await expect(userMessage).toContainText("Playwright history message");
  const assistantMessage = page.locator('.message[data-role="assistant"]').first();
  await expect(assistantMessage).toContainText("Indexed response for the browser test.");
  const richHeading = assistantMessage.getByRole("heading", { name: "Rich browser evidence" });
  await expect(richHeading).toBeVisible();
  const richSubheading = assistantMessage.getByRole("heading", { name: "Colored hierarchy" });
  await expect(richSubheading).toBeVisible();
  await expect(assistantMessage.locator("table")).toContainText("colored");
  await expect(assistantMessage.locator(".katex-display")).toHaveCount(1);
  await expect(assistantMessage.locator("pre.shiki")).toHaveCount(2);
  await expect(assistantMessage.locator("img")).toHaveCount(0);
  expect(await page.evaluate(() => (window as Window & { __rawHtmlExecuted?: boolean }).__rawHtmlExecuted)).toBeUndefined();

  const headingColor = await richHeading.evaluate((element) => getComputedStyle(element).color);
  expect(headingColor).toBe("rgb(139, 179, 255)");
  const subheadingColor = await richSubheading.evaluate((element) => getComputedStyle(element).color);
  expect(subheadingColor).toBe("rgb(210, 168, 255)");
  const strongColor = await assistantMessage.locator("li strong").first().evaluate(
    (element) => getComputedStyle(element).color
  );
  expect(strongColor).toBe("rgb(242, 204, 96)");
  const inlineCodeColor = await assistantMessage.locator("li code").first().evaluate(
    (element) => getComputedStyle(element).color
  );
  expect(inlineCodeColor).toBe("rgb(165, 214, 255)");
  const linkColor = await assistantMessage.locator("a").first().evaluate(
    (element) => getComputedStyle(element).color
  );
  expect(linkColor).toBe("rgb(139, 179, 255)");

  const firstCodeBlock = assistantMessage.locator("pre.shiki").first();
  const tokens = await firstCodeBlock.locator("span[style*='color']").evaluateAll((nodes) =>
    nodes.map((node) => ({
      text: node.textContent?.trim() ?? "",
      color: getComputedStyle(node).color,
    }))
  );
  const keywordColor = tokens.find((token) => token.text === "const")?.color;
  const stringColor = tokens.find((token) => token.text.includes("playwright"))?.color;
  expect(keywordColor).toBeTruthy();
  expect(stringColor).toBeTruthy();
  expect(keywordColor).not.toBe(stringColor);
  expect(await firstCodeBlock.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);

  await userMessage.getByRole("button", { name: "Copy", exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(
    "Playwright history message"
  );
  await assistantMessage.getByRole("button", { name: "Copy", exact: true }).click();
  const copiedAssistant = await page.evaluate(() => navigator.clipboard.readText());
  expect(copiedAssistant).toContain("## Rich browser evidence");
  expect(copiedAssistant).toContain('const keyword = "playwright";');
  expect(copiedAssistant).toContain('<img src=x onerror="window.__rawHtmlExecuted = true">');

  await page.getByRole("button", { name: "Tools", exact: true }).click();
  const tool = page.locator(".tool").first();
  await expect(tool).toContainText("exec_command");
  await expect(tool.locator(".tool-cmd")).toContainText("playwright insight");
  await expect(tool.locator(".tool-out")).toContainText("Process exited with code 0");
  await tool.getByRole("button", { name: "Copy", exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain(
    "playwright insight"
  );

  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  const event = page.locator(".event").first();
  await event.locator("summary").click();
  await expect(event.locator(".event-json pre")).toContainText("playwright-history");
  await event.getByRole("button", { name: "Copy JSON", exact: true }).click();
  await expect.poll(async () => JSON.parse(
    await page.evaluate(() => navigator.clipboard.readText())
  )).toMatchObject({ type: "session_meta", payload: { id: "playwright-history" } });

  await page.getByRole("button", { name: "Activity" }).click();
  await expect(page.getByRole("heading", { name: "Session activity" })).toBeVisible();
  await expect(page.locator(".waterfall canvas")).toBeVisible();
  // Crucially: a history session must NOT open a live terminal.
  await expect(page.locator(".terminal-canvas")).toHaveCount(0);

  await page.evaluate(() => {
    document.body.style.minHeight = "3000px";
  });
  await page.locator("body").press("End");
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  await page.locator("body").press("Home");
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await expect(page.locator(".empty.error")).toHaveCount(0);

  await page.locator(".back-link").click();
  await expect(page).toHaveURL(/\/history\?agent=codex&q=playwright$/);
  await expect(page.locator(".group-header").first()).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(".group-rows .row").first()).toBeVisible();

  await page.goto("/memories");
  await expect(page.getByRole("heading", { name: "Memories" })).toBeVisible();
  const memoryBody = page.locator(".content-body.markdown-body").first();
  await expect(memoryBody).toBeVisible();
  await expect(memoryBody.locator(".shiki")).toHaveCount(0);
  const memoryBackground = await memoryBody.evaluate((element) => getComputedStyle(element).backgroundColor);
  expect(memoryBackground).not.toBe("rgb(13, 17, 23)");
  expect(pageErrors).toEqual([]);
});

test("history groups default closed and remember expanded workspaces", async ({ page }) => {
  await page.goto("/history");
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();

  const headers = page.locator(".group-header");
  await expect(headers.first()).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".group-rows").first()).toBeHidden();

  const renderedPaths = await page.locator(".group-path").allTextContents();
  const renderedLatest = await page.locator(".history-group").evaluateAll((groups) =>
    groups.map((group) => {
      const value = group.getAttribute("data-latest-updated");
      const timestamp = value ? Date.parse(value) : Number.NEGATIVE_INFINITY;
      return Number.isNaN(timestamp) ? Number.NEGATIVE_INFINITY : timestamp;
    })
  );
  expect(renderedLatest).toEqual([...renderedLatest].sort((left, right) => right - left));

  await headers.first().click();
  await expect(headers.first()).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(".group-rows").first()).toBeVisible();

  const path = page.locator(".group-path").first();
  await path.selectText();
  await expect.poll(() => page.evaluate(() => window.getSelection()?.toString() ?? "")).toContain(renderedPaths[0]);
  await expect(page.locator(".group-copy").first()).toHaveAttribute("title", "Copy directory path");

  await page.locator(".group-rows .row-meta").first().evaluate((meta) => {
    const model = meta.firstElementChild;
    if (model) model.textContent = "very-long-model-name-".repeat(20);
  });
  const groupRight = await page.locator(".history-group").first().evaluate(
    (group) => group.getBoundingClientRect().right
  );
  const rowRights = await page.locator(".group-rows .row").evaluateAll(
    (rows) => rows.map((row) => row.getBoundingClientRect().right)
  );
  expect(rowRights.every((right) => right <= groupRight + 1)).toBe(true);

  await page.locator(".group-rows .row").first().click();
  await expect(page).toHaveURL(/\/history\/.+/);
  await page.locator(".back-link").click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(page.locator(".group-header").first()).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(".group-rows").first()).toBeVisible();
});

test("settings exposes profile, tokens, and admin users", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "API tokens" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
});

test("admin can inspect system status and sanitized logs", async ({ page }) => {
  await page.goto("/system");
  await expect(page.getByRole("heading", { name: "System" })).toBeVisible();
  await expect(page.getByText("Overall status")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Logs" })).toBeVisible();
  await expect(page.getByLabel("System components")).toBeVisible();
});

test("mobile viewport collapses the brand and nav labels", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.locator(".shell__brandname")).toBeHidden();
  const rail = await page.locator(".shell__rail").boundingBox();
  const nav = await page.locator(".shell__nav").boundingBox();
  expect(rail?.height).toBe(54);
  expect(nav?.y).toBe(742);
  expect(nav?.height).toBe(58);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth
  );
  expect(overflow).toBeLessThanOrEqual(1);
});
