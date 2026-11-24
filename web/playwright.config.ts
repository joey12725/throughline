import { defineConfig, devices } from "@playwright/test";

/*
 * E2E runs the built dev server (which proxies /v1 to the API) and drives the
 * system Chromium. Set THROUGHLINE_TOKEN (owner) and, if the API is not on the
 * default, THROUGHLINE_URL before running `pnpm e2e`. Tests that need a token
 * skip cleanly when it is absent, so the suite is safe to run anywhere.
 */
const PORT = Number(process.env.PW_PORT ?? 5173);
const BASE = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [["list"]],
  timeout: 30_000,
  use: {
    baseURL: `${BASE}/ui/`,
    trace: "on-first-retry",
    launchOptions: { executablePath: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium" },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: `pnpm dev --port ${PORT} --strictPort`,
    url: `${BASE}/ui/`,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
