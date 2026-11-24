import { test, expect, type Page } from "@playwright/test";

/*
 * People: the constellation (/people) and a person page (/people/:id). Invariants:
 *   1. no request leaves the app origin;
 *   2. no uncaught page error and no console error;
 *   3. the constellation draws and lists people; search narrows the list;
 *   4. a person page renders its header and panels.
 */
const TOKEN = process.env.THROUGHLINE_TOKEN ?? "";
const SHOTS = process.env.SHOTS_DIR ?? "test-results/shots";

async function seed(page: Page, theme: "nocturne" | "daybook" = "nocturne"): Promise<void> {
  await page.addInitScript(
    ([tok, th]) => {
      try {
        if (tok) sessionStorage.setItem("tl.token", tok);
        localStorage.setItem(
          "tl.prefs",
          JSON.stringify({ theme: th, density: "observatory", veil: false, mapTiles: false, disclosure: "schema", tz: "America/New_York" }),
        );
      } catch {
        /* ignore */
      }
    },
    [TOKEN, theme] as const,
  );
}

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

test.describe("people", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("constellation draws, lists people and search narrows it", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    await page.goto("people", { waitUntil: "networkidle" });

    // The lede reports a person count; the canvas and the side table render.
    await expect(page.getByText(/people orbit you/)).toBeVisible();
    await expect(page.locator("canvas")).toBeVisible();
    await page.waitForTimeout(1500);
    const rowsBefore = await page.locator("tbody tr").count();
    expect(rowsBefore).toBeGreaterThan(5);

    // Search narrows the list to a known person.
    await page.getByLabel("Search people").fill("Ivo");
    await page.waitForTimeout(400);
    await expect(page.getByText("Ivo Tumbleweed").first()).toBeVisible();
    const rowsAfter = await page.locator("tbody tr").count();
    expect(rowsAfter).toBeLessThan(rowsBefore);

    await page.screenshot({ path: `${SHOTS}/people-constellation-nocturne.png`, fullPage: false });
    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("person page renders header and the stacked relationship timeline", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    await page.goto("people/17", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { name: "Ivo Tumbleweed" })).toBeVisible();
    await expect(page.getByText("Relationship over time")).toBeVisible();
    await expect(page.getByText("Reply latency")).toBeVisible();
    await expect(page.getByText("Channel mix")).toBeVisible();
    await expect(page.getByRole("button", { name: /Ask about this person/ })).toBeVisible();
    await page.waitForTimeout(2500);

    // The bespoke stacked area draws an SVG and a per-channel legend.
    const timeline = page.locator("article", { hasText: "Relationship over time" }).first();
    await expect(timeline.locator("svg").first()).toBeVisible({ timeout: 12000 });

    await page.screenshot({ path: `${SHOTS}/people-person-nocturne.png`, fullPage: true });
    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("a sparse person still renders a timeline and designed empty states", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    // Person 31 has only a couple dozen records through one channel.
    await page.goto("people/31", { waitUntil: "networkidle" });

    await expect(page.getByText("Relationship over time")).toBeVisible();
    await expect(page.getByText("Records").first()).toBeVisible();
    await page.waitForTimeout(2000);
    const timeline = page.locator("article", { hasText: "Relationship over time" }).first();
    await expect(timeline.locator("svg").first()).toBeVisible({ timeout: 12000 });

    await page.screenshot({ path: `${SHOTS}/people-person-sparse-nocturne.png`, fullPage: true });
    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("the constellation and person page hold up at phone width", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await page.setViewportSize({ width: 390, height: 844 });
    await seed(page);
    await page.goto("people", { waitUntil: "networkidle" });
    // The lede has a gutter (not flush against the rail) and the list reflows.
    await expect(page.getByText(/people orbit you/)).toBeVisible();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${SHOTS}/people-constellation-phone-nocturne.png`, fullPage: true });

    await page.goto("people/17", { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: "Ivo Tumbleweed" })).toBeVisible();
    await page.waitForTimeout(1800);
    await page.screenshot({ path: `${SHOTS}/people-person-phone-nocturne.png`, fullPage: true });

    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("daybook screenshots", async ({ page }) => {
    await seed(page, "daybook");
    await page.goto("people", { waitUntil: "networkidle" });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${SHOTS}/people-constellation-daybook.png` });
    await page.goto("people/17", { waitUntil: "networkidle" });
    await page.waitForTimeout(2500);
    await page.screenshot({ path: `${SHOTS}/people-person-daybook.png`, fullPage: true });
  });
});
