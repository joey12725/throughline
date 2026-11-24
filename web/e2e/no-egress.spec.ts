import { test, expect } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * Private by construction: the browser talks only to its own origin. This test
 * fails if any request leaves the app origin — a bundled font that slipped a
 * CDN link, a map tile, an analytics beacon, an <img> pointed at a URL found in
 * data. data:/blob: and the dev server's HMR websocket are allowed.
 */
test.describe("no egress", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("every request stays same-origin", async ({ page }, testInfo) => {
    const origin = new URL(testInfo.project.use.baseURL as string).origin;
    const offOrigin: string[] = [];

    page.on("request", (req) => {
      const url = req.url();
      if (url.startsWith("data:") || url.startsWith("blob:")) return;
      let reqOrigin: string;
      try {
        reqOrigin = new URL(url).origin;
      } catch {
        return;
      }
      // Dev-only: Vite's HMR client and websocket live on the same origin; a
      // ws:// URL to the same host is fine.
      if (reqOrigin !== origin && !url.startsWith(`ws://${new URL(origin).host}`) && !url.startsWith(`wss://${new URL(origin).host}`)) {
        offOrigin.push(url);
      }
    });

    await seed(page);
    await page.goto("/ui/");
    await page.waitForTimeout(500);

    // Exercise the surfaces most likely to fetch: palette (channels/people),
    // settings (colours), styleguide (source palette, fonts).
    await page.keyboard.press("Control+k");
    await page.keyboard.type("a");
    await page.waitForTimeout(400);
    await page.keyboard.press("Escape");
    await page.goto("/ui/settings");
    await page.waitForTimeout(400);
    await page.goto("/ui/styleguide");
    await page.waitForTimeout(600);

    expect(offOrigin, `off-origin requests: ${offOrigin.join(", ")}`).toEqual([]);
  });
});
