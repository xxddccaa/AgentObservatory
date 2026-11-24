import { expect, type Page } from "@playwright/test";

/** There is no login: open the dashboard and wait for it to render. */
export async function openDashboard(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}
