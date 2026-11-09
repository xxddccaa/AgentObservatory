import { expect, test } from "@playwright/test";
import { VIEWER, login } from "./util";

test.beforeEach(async ({ page }) => {
  await page.context().clearCookies();
});

test("storage catalogues local data and previews without deleting", async ({ page }) => {
  await login(page);
  await page.goto("/storage");

  await expect(page.getByRole("heading", { name: "Storage" })).toBeVisible();
  await expect(page.getByText("Codex rollout sessions")).toBeVisible();
  await expect(page.getByText("Agent 数据占用")).toBeVisible();
  await expect(page.getByText("数据时间")).toBeVisible();
  await expect(page.getByRole("heading", { name: "① 选择要清理的位置" })).toBeVisible();

  await page.getByRole("button", { name: "② 预览清理（只读）" }).click();
  await expect(page.getByRole("heading", { name: "③ 确认并执行" })).toBeVisible();
  // The fixtures are freshly written, so the default selection may hold nothing
  // old enough; on a machine with old data the delete button is offered
  // instead. Accept either — but only one of them, hence `.first()`.
  const emptyPlan = page.getByText("当前选择下没有超过保留窗口的数据，无需清理。");
  const deleteButton = page.getByRole("button", { name: /立即清理/ });
  await expect(emptyPlan.or(deleteButton).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "最近的清理记录" })).toBeVisible();
  await expect(page.getByText("还没有执行过清理。")).toBeVisible();
});

test("viewers cannot reach the storage page", async ({ page }) => {
  await login(page, VIEWER);
  await page.goto("/storage");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
});
