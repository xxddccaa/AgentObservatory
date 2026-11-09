import { expect, test } from "@playwright/test";
import { login, VIEWER } from "./util";

test.beforeEach(async ({ page }) => {
  await page.context().clearCookies();
});

test("unauthenticated visit redirects to the login page", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});

test("bad credentials surface an inline error", async ({ page }) => {
  await page.goto("/login");
  await page.locator('input[name="username"]').fill("admin");
  await page.locator('input[name="password"]').fill("definitely-wrong");
  await page.locator("button.login__submit").click();
  await expect(page.getByText("Incorrect username or password.")).toBeVisible();
});

test("login lands on the dashboard, logout returns to login", async ({ page }) => {
  await login(page);
  await expect(page).toHaveURL("http://127.0.0.1:18124/");

  await page.locator("button.usermenu__trigger").click();
  await page.locator("button.usermenu__item--danger").click();
  await expect(page).toHaveURL(/\/login/);
});

test("a viewer does not see operator-only actions", async ({ page }) => {
  await login(page, VIEWER);
  await expect(page.locator("button.shell__new")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "System" })).toHaveCount(0);
  await page.goto("/system");
  await expect(page).toHaveURL("http://127.0.0.1:18124/");
});
