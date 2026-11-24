import { test, type Page } from "@playwright/test";
import { seedSetupMock, seedAuth } from "./fixtures/setupMock";

/*
 * Screenshot pass for visual review (not an assertion suite). Deep-links every
 * step in both themes at desktop and phone widths, into SHOTS_DIR (default test-results/shots).
 */
const SHOTS = process.env.SHOTS_DIR ?? "test-results/shots";
const STEPS = ["welcome", "account", "models", "photos", "sources", "connect", "companion", "import", "people", "lenses", "pages", "queries", "agent", "tour"] as const;

// A visual-review aid, not part of the normal suite. Run with SHOTS=1 pnpm e2e.
test.beforeEach(() => {
  test.skip(!process.env.SHOTS, "set SHOTS=1 (and optionally SHOTS_DIR) to capture screenshots");
});

async function prep(page: Page, theme: "nocturne" | "daybook", step: string): Promise<void> {
  await seedAuth(page, theme);
  seedSetupMock(page);
  await page.goto(`/ui/welcome/${step}`);
  await page.getByRole("heading", { level: 1 }).first().waitFor({ state: "visible" });
  if (step === "people") {
    // advance past "which is you" so the summary + a question are visible
    const me = page.getByRole("button", { name: /You \(Wave\)/ });
    if (await me.count()) await me.click();
  }
  await page.waitForTimeout(700);
}

for (const theme of ["nocturne", "daybook"] as const) {
  for (const step of STEPS) {
    test(`shot ${theme} desktop ${step}`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 1000 });
      await prep(page, theme, step);
      await page.screenshot({ path: `${SHOTS}/${theme}-desktop-${step}.png` });
    });
  }
}

for (const step of STEPS) {
  test(`shot nocturne phone ${step}`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await prep(page, "nocturne", step);
    await page.screenshot({ path: `${SHOTS}/nocturne-phone-${step}.png` });
  });
}

test("shot gate", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  seedSetupMock(page, { needsAccount: true });
  await page.addInitScript(() => { try { localStorage.setItem("tl.prefs", JSON.stringify({ theme: "nocturne", density: "observatory", veil: false, mapTiles: false, disclosure: "schema", tz: "UTC" })); } catch { /* ignore */ } });
  await page.goto("/ui/");
  await page.getByRole("heading", { level: 1 }).first().waitFor({ state: "visible" });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${SHOTS}/nocturne-desktop-gate.png` });
});
