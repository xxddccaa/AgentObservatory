import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";

// E2E uses the same stable topology as `agent-observatory up`: FastAPI serves
// frontend/dist and the API on one origin. The command seeds only the guarded
// disposable CI database and launches the real combined worker.
const cachedChromium =
  "/root/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell";
const chromiumPath =
  process.env.PW_CHROMIUM_PATH ??
  (existsSync(cachedChromium) ? cachedChromium : undefined);

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:18124",
    viewport: { width: 1280, height: 800 },
    ...(chromiumPath ? { launchOptions: { executablePath: chromiumPath } } : {}),
  },
  webServer: {
    command:
      "cd ../backend && " +
      "PYTHON_BIN=.venv/bin/python " +
      "ALEMBIC_BIN=.venv/bin/alembic " +
      "bash tests/run_e2e_server.sh",
    url: "http://127.0.0.1:18124/api/health/live",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
