import { test, expect, type Page } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * Places: the map renders from real geo data, a place can be selected and its
 * detail reads, no request leaves the origin, no renderer throws.
 */
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

test.describe("places", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("renders the map, selects a place, no egress, no errors", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    await page.goto("places", { waitUntil: "networkidle" });

    await expect(page.getByRole("application", { name: /Map of places/ })).toBeVisible();
    await expect(page.getByText("Top places by dwell")).toBeVisible();
    await page.waitForTimeout(2500);

    // The track is sampled across the whole scope, not re-capped to the
    // server's default 1,000 rows: the point count must exceed that.
    const meta = await page.locator('[class*="toolMeta"]').first().innerText();
    const points = Number((meta.match(/([\d,]+)\s*points/)?.[1] ?? "0").replace(/,/g, ""));
    expect(points).toBeGreaterThan(1000);

    // Home + fit-all framing controls are present.
    await expect(page.getByRole("button", { name: /Fit home city/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /Fit all places/ })).toBeVisible();

    // Select the first top place and confirm the detail reads.
    const firstPlace = page.locator('[class*="placeRow"]').first();
    await expect(firstPlace).toBeVisible();
    await firstPlace.click();
    await expect(page.getByText("Total dwell")).toBeVisible();
    await expect(page.getByText("When you are here")).toBeVisible();

    // A layer toggle works (turn on the heat layer).
    await page.getByText("Heat", { exact: true }).click();
    await page.waitForTimeout(500);

    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });
});
