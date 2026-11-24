import { defineConfig } from "@playwright/test";

/*
 * The onboarding walkthrough against a real server (no mocks): see
 * e2e/demo/README.md. DEMO_URL defaults to the demo instance on :8811.
 * It records a video of the whole run.
 */
const BASE = process.env.DEMO_URL ?? "http://127.0.0.1:8811";

export default defineConfig({
  testDir: "./e2e/demo",
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  timeout: 20 * 60_000,
  outputDir: process.env.DEMO_OUT ?? "test-results/demo",
  use: {
    baseURL: `${BASE}/ui/`,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    actionTimeout: 15_000,
    video: { mode: "on", size: { width: 1440, height: 900 } },
    launchOptions: { executablePath: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium", slowMo: Number(process.env.SLOWMO ?? 0) },
  },
});
