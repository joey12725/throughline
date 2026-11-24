import { test, expect, type Page } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * The channel page, one channel per archetype. Invariants:
 *   1. no request leaves the app origin (no CDN, tiles, fonts, analytics);
 *   2. no renderer throws and no uncaught page error;
 *   3. the archetype reader keyset-paginates (scrolling loads more records);
 *   4. a record opens in the drawer.
 * Channels are chosen by archetype from the dev installation's known shapes.
 */
const CHANNELS: Record<string, string> = {
  conversation: "chat.sms",
  transcript: "transcripts.wave",
  measurement: "health.oura.daily",
  interval: "health.oura.sleep",
  document: "email.message",
  feed: "youtube.uploads",
  gauge: "t3code.usage",
  snapshot: "morning.events",
  registry: "t3code.threads",
  geo_track: "location.timeline",
  zero_row: "youtube.watch",
};

function watch(page: Page, origin: string) {
  const offOrigin: string[] = [];
  const errors: string[] = [];
  const host = new URL(origin).host;
  page.on("request", (req) => {
    const url = req.url();
    if (url.startsWith("data:") || url.startsWith("blob:")) return;
    let o = origin;
    try {
      o = new URL(url).origin;
    } catch {
      return;
    }
    if (o !== origin && !url.startsWith(`ws://${host}`) && !url.startsWith(`wss://${host}`)) offOrigin.push(url);
  });
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const t = msg.text();
    if (/data:font/.test(t) || /Failed to load resource/.test(t) || /404/.test(t)) return;
    errors.push(t);
  });
  page.on("pageerror", (e) => errors.push(String(e)));
  return { offOrigin, errors };
}

test.describe("channel page", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  for (const [arch, channel] of Object.entries(CHANNELS)) {
    test(`${arch} (${channel}) renders cleanly with no egress`, async ({ page }, info) => {
      const origin = new URL(info.project.use.baseURL as string).origin;
      const { offOrigin, errors } = watch(page, origin);
      await seed(page);
      await page.goto(`c/${encodeURIComponent(channel)}`, { waitUntil: "networkidle" });
      // The header and the always-present spine mount for every channel.
      await expect(page.getByRole("heading", { name: channel, level: 1 })).toBeVisible();
      await page.waitForTimeout(4000);
      expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
      expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
    });
  }

  test("the reader keyset-paginates and a record opens", async ({ page }) => {
    // A div scroll container (not the lucide-scroll-text nav icon, which is an SVG).
    const scroller = () => page.locator('div[class*="scroll"]').last();
    await seed(page);
    await page.goto(`c/${encodeURIComponent("chat.sms")}`, { waitUntil: "networkidle" });
    await page.waitForTimeout(3000);
    const bubbles = page.locator('[class*="bubbleRow"]');
    const before = await bubbles.count();
    expect(before).toBeGreaterThan(0);
    // Scroll the records stream to the bottom a few times to pull more pages.
    for (let i = 0; i < 4; i++) {
      await scroller().evaluate((el) => (el.scrollTop = el.scrollHeight));
      await page.waitForTimeout(900);
    }
    const after = await bubbles.count();
    expect(after, "scrolling should load more records").toBeGreaterThan(before);

    // Open a record in the drawer.
    await page.locator('[class*="bubble"][role="button"]').first().click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByText("Query", { exact: false }).first()).toBeVisible();
  });
});
