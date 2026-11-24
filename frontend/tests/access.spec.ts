import { expect, test } from "@playwright/test";
import { openDashboard } from "./util";

test("the dashboard opens directly, without a login page", async ({ page }) => {
  await openDashboard(page);
  await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const label of ["History", "Storage", "System", "Audit"]) {
    await expect(nav.getByRole("link", { name: label })).toBeVisible();
  }
});

test("account pages no longer exist", async ({ page }) => {
  for (const path of ["/login", "/settings"]) {
    await page.goto(path);
    await expect(page).toHaveURL(new RegExp(`${path}$`));
    await expect(page.getByRole("heading", { name: /not found/i })).toBeVisible();
  }
});

test("cross-origin writes and foreign hosts are refused", async ({ request }) => {
  const crossOrigin = await request.post("/api/memories/export", {
    data: { ids: ["x"] },
    headers: { Origin: "http://attacker.example" },
  });
  expect(crossOrigin.status()).toBe(403);
  expect((await crossOrigin.json()).error.code).toBe("cross_origin_refused");

  const foreignHost = await request.get("/api/health/live", {
    headers: { Host: "attacker.example" },
  });
  expect(foreignHost.status()).toBe(403);
  expect((await foreignHost.json()).error.code).toBe("host_not_allowed");
});
