import { test, expect } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * The chart gallery draws every lens against the dev server's real channels. This
 * test opens it and asserts the two invariants that matter for the chart layer:
 *   1. no request leaves the app origin (no CDN, tiles, fonts or analytics), and
 *   2. no chart renderer throws (no "[charts] … failed to render", no uncaught).
 * Pre-existing shell issues unrelated to charts (a data: font blocked by the
 * shell's own CSP, and benign 404s for optional assets) are tolerated so the test
 * stays about the charts, not the shell.
 */
test.describe("chart gallery", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("renders every lens with no egress and no renderer errors", async ({ page }, testInfo) => {
    test.setTimeout(70_000);
    const origin = new URL(testInfo.project.use.baseURL as string).origin;
    const host = new URL(origin).host;
    const offOrigin: string[] = [];
    const errors: string[] = [];
    const chartErrors: string[] = [];

    page.on("request", (req) => {
      const url = req.url();
      if (url.startsWith("data:") || url.startsWith("blob:")) return;
      let reqOrigin = origin;
      try {
        reqOrigin = new URL(url).origin;
      } catch {
        return;
      }
      if (reqOrigin !== origin && !url.startsWith(`ws://${host}`) && !url.startsWith(`wss://${host}`)) {
        offOrigin.push(url);
      }
    });
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      const text = msg.text();
      if (/\[charts\]/.test(text) || /failed to render/i.test(text)) chartErrors.push(text);
      // Known, chart-unrelated shell noise: a data: font blocked by the shell CSP
      // and optional-asset 404s. Everything else is a real error.
      const benign = /data:font/.test(text) || /Failed to load resource/.test(text) || /404/.test(text);
      if (!benign) errors.push(text);
    });
    page.on("pageerror", (err) => errors.push(String(err)));

    await seed(page);
    await page.goto("styleguide/charts", { waitUntil: "networkidle" });
    // Let the panels fetch and the lenses paint.
    await page.locator("#charts-gallery").waitFor({ state: "visible" });
    await expect(page.locator("#charts-gallery canvas, #charts-gallery svg").first()).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(12_000);

    expect(offOrigin, `off-origin requests:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(chartErrors, `chart renderer errors:\n${chartErrors.join("\n")}`).toEqual([]);
    expect(errors, `unexpected console/page errors:\n${errors.join("\n")}`).toEqual([]);
  });
});
