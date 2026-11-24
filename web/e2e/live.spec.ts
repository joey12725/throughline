import { test, expect, type Route } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * Live is driven by a long poll over /v1/web/arrivals. The dev database is
 * static, so to prove the fire hose actually moves we stub the arrivals route
 * with a synthetic stream (per the brief's "simulate in a Playwright test by
 * stubbing the arrivals route"). Channel names come from the real bootstrap so
 * the archetype cards and source colours resolve exactly as in production.
 */

const SHOTS = process.env.SHOTS_DIR ?? "test-results/shots";

async function pickChannels(page: import("@playwright/test").Page): Promise<string[]> {
  const boot = await page.evaluate(async (tok) => {
    const r = await fetch("/v1/web/bootstrap", { headers: { Authorization: `Bearer ${tok}` } });
    return r.json();
  }, TOKEN);
  const byPlugin = new Map<string, string>();
  for (const c of boot.channels) if (!byPlugin.has(c.plugin)) byPlugin.set(c.plugin, c.name);
  return [...byPlugin.values()].slice(0, 4);
}

/** Install a synthetic arrivals stream. */
async function stubArrivals(page: import("@playwright/test").Page, channels: string[]): Promise<void> {
  let id = 900000;
  let tick = 0;
  await page.route("**/v1/web/arrivals*", async (route: Route) => {
    const url = new URL(route.request().url());
    const hasAfter = url.searchParams.has("after");
    const nowIso = new Date().toISOString();
    const n = hasAfter ? 1 + (tick % 3) : 4; // seed page vs. incremental polls
    tick += 1;
    const rows = Array.from({ length: n }, (_, i) => {
      id += 1;
      const ch = channels[(id + i) % channels.length] ?? channels[0] ?? "unknown.channel";
      return {
        id,
        channel: ch,
        thread_id: null,
        happened_at: nowIso,
        ingested_at: nowIso,
        author: id % 2 ? "Alex" : null,
        person_id: null,
        excerpt: `synthetic arrival ${id} on ${ch}`,
        version: 1,
        settled: id % 5 !== 0, // a few unsettled rows above the settle line
      };
    });
    if (hasAfter) await new Promise((r) => setTimeout(r, 250)); // mimic the long poll
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        cursor: `syn:${id}`,
        rows,
        counts: {},
        sampled: false,
        plugins: [],
        reads: [],
        horizon: new Date(Date.now() - 120_000).toISOString(),
        settle_s: 120,
      }),
    });
  });
}

test.describe("Live (fire hose)", () => {
  test.skip(!TOKEN, "needs THROUGHLINE_TOKEN");

  test("streams synthetic arrivals: cards, rate strip, settle line, pause", async ({ page }, testInfo) => {
    const consoleErrors: string[] = [];
    page.on("console", (m) => {
      if (m.type() === "error" && !/Failed to load resource/i.test(m.text())) consoleErrors.push(m.text());
    });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror ${e.message}`));

    const origin = new URL(testInfo.project.use.baseURL as string).origin;
    const host = new URL(origin).host;
    const offOrigin: string[] = [];
    page.on("request", (req) => {
      const u = req.url();
      if (u.startsWith("data:") || u.startsWith("blob:")) return;
      let o: string;
      try { o = new URL(u).origin; } catch { return; }
      if (o !== origin && !u.startsWith(`ws://${host}`) && !u.startsWith(`wss://${host}`)) offOrigin.push(u);
    });

    await seed(page);
    await page.goto("/ui/", { waitUntil: "domcontentloaded" });
    const channels = await pickChannels(page);
    await stubArrivals(page, channels);

    await page.goto("/ui/live", { waitUntil: "domcontentloaded" });

    // Arrivals accrue.
    await expect.poll(() => page.getByTestId("arrival").count(), { timeout: 10_000 }).toBeGreaterThan(5);
    // The rate strip lights up for the synthetic plugins.
    await expect.poll(() => page.getByTestId("rate-row").count()).toBeGreaterThan(0);
    // The settle hairline is present (some rows are unsettled).
    await expect(page.getByText(/settle horizon/i)).toBeVisible();

    // Motion screenshot in both themes.
    await page.screenshot({ path: `${SHOTS}/live-motion-nocturne.png` });

    // Pause stops the stream and the status reflects it.
    await page.getByRole("button", { name: /pause/i }).click();
    await expect(page.getByRole("button", { name: /resume/i })).toBeVisible();
    // Let any render already queued before the click settle, then measure.
    await page.waitForTimeout(300);
    const countBefore = await page.getByTestId("arrival").count();
    await page.waitForTimeout(1200);
    const countAfter = await page.getByTestId("arrival").count();
    expect(countAfter).toBe(countBefore); // no new arrivals while paused

    expect(offOrigin, `off-origin: ${offOrigin.join(", ")}`).toEqual([]);
    expect(consoleErrors).toEqual([]);
  });

  test("resume restarts the stream", async ({ page }) => {
    await seed(page);
    await page.goto("/ui/", { waitUntil: "domcontentloaded" });
    const channels = await pickChannels(page);
    await stubArrivals(page, channels);
    await page.goto("/ui/live", { waitUntil: "domcontentloaded" });

    await expect.poll(() => page.getByTestId("arrival").count(), { timeout: 10_000 }).toBeGreaterThan(2);
    await page.getByRole("button", { name: /pause/i }).click();
    const paused = await page.getByTestId("arrival").count();
    await page.getByRole("button", { name: /resume/i }).click();
    await expect.poll(() => page.getByTestId("arrival").count(), { timeout: 10_000 }).toBeGreaterThan(paused);
  });
});
