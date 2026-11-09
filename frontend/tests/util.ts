import { expect, type Page } from "@playwright/test";

export const ADMIN = { username: "admin", password: "Synthetic-e2e-admin-42" };
export const VIEWER = { username: "viewer", password: "viewerpass123" };

/** Log in through the real login form and wait for the dashboard to render. */
export async function login(page: Page, user = ADMIN): Promise<void> {
  await page.goto("/login");
  await page.locator('input[name="username"]').fill(user.username);
  await page.locator('input[name="password"]').fill(user.password);
  await page.locator("button.login__submit").click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}
