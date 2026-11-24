import { test, expect, type Page } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * Responsive shell + cold drawer deep-link.
 *   1. On a phone viewport the left rail is replaced by a bottom tab bar, the
 *      header collapses its controls into an overflow menu, and navigation works.
 *   2. A cold deep-link to a record drawer resolves on a screen that never imports
 *      the record feature (eager registration) — no "No panel registered".
 *   3. No request leaves the origin and no renderer console error on either path.
 */
const PHONE = { width: 390, height: 844 };

function watch(page: Page, origin: string) {
  const offOrigin: string[] = [];
  const errors: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (url.startsWith("data:") || url.startsWith("blob:")) return;
    let o = origin;
    try {
      o = new URL(url).origin;
    } catch {
      return;
    }
    const host = new URL(origin).host;
    if (o !== origin && !url.startsWith(`ws://${host}`) && !url.startsWith(`wss://${host}`)) offOrigin.push(url);
  });
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (e) => errors.push(String(e)));
  return { offOrigin, errors };
}

test.describe("responsive shell", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("phone: bottom tab bar replaces the rail, header collapses, navigation works", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await page.setViewportSize(PHONE);
    await seed(page);
    await page.goto("./", { waitUntil: "domcontentloaded" });

    // The single primary-navigation landmark is the bottom bar (the rail is
    // display:none at this width, so it is absent from the a11y tree).
    const nav = page.getByRole("navigation", { name: "Primary" });
    await expect(nav).toBeVisible();
    const box = await nav.boundingBox();
    expect(box).not.toBeNull();
    // It sits in the lower portion of the viewport.
    expect(box!.y).toBeGreaterThan(PHONE.height / 2);

    // The header collapses its controls into an overflow menu.
    await expect(page.getByRole("button", { name: "More options" })).toBeVisible();

    // Navigate via the bottom bar.
    await nav.getByRole("link", { name: "Query" }).click();
    await expect(page).toHaveURL(/\/query/);

    expect(offOrigin, `off-origin: ${offOrigin.join(", ")}`).toEqual([]);
    expect(errors, `console: ${errors.join(" | ")}`).toEqual([]);
  });

  test("cold deep-link opens the record drawer on a screen that never imports it", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    // A real record id from a channel the record module would otherwise open.
    const res = await page.request.post(`${origin}/v1/sql`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      data: { statement: "SELECT id FROM api.ch_chat_sms ORDER BY happened_at DESC LIMIT 1" },
    });
    const id = String((await res.json()).rows[0].id);
    const d = encodeURIComponent(`record:${new URLSearchParams({ id, channel: "chat.sms" }).toString()}`);
    await page.goto(`sources?d=${d}`, { waitUntil: "domcontentloaded" });

    // The drawer resolves (eager registration): a dialog opens and the
    // "No panel registered" fallback never shows.
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByText("No panel registered")).toHaveCount(0);
    // It stays open (the URL->store / store->URL sync does not clobber it).
    await page.waitForTimeout(800);
    await expect(page.getByRole("dialog")).toBeVisible();

    expect(offOrigin, `off-origin: ${offOrigin.join(", ")}`).toEqual([]);
    expect(errors, `console: ${errors.join(" | ")}`).toEqual([]);
  });
});
