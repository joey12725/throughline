import { test, expect, type Page } from "@playwright/test";

/*
 * Day: /day (today) and /day/:date. Invariants:
 *   1. no request leaves the app origin;
 *   2. no uncaught page error and no console error;
 *   3. a busy day draws its swimlanes and vitals;
 *   4. day navigation (buttons + keyboard) moves the date.
 */
const TOKEN = process.env.THROUGHLINE_TOKEN ?? "";
const SHOTS = process.env.SHOTS_DIR ?? "test-results/shots";
const BUSY = "2026-11-19";

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

test.describe("day", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("a busy day draws swimlanes, vitals and the narrative with no egress", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    await page.goto(`day/${BUSY}`, { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { name: /November 19/ })).toBeVisible();
    await expect(page.getByText("Across the day")).toBeVisible();
    await expect(page.getByText("The day, in order")).toBeVisible();
    await expect(page.locator("canvas")).toBeVisible();
    await expect(page.getByText("Jump to a day")).toBeVisible();
    await page.waitForTimeout(1500);

    await page.screenshot({ path: `${SHOTS}/day-busy-nocturne.png`, fullPage: true });
    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("day navigation moves the date (buttons and keyboard)", async ({ page }) => {
    await seed(page);
    await page.goto(`day/${BUSY}`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /Next day/ }).click();
    await expect(page).toHaveURL(/day\/2026-11-20/);
    await page.keyboard.press("ArrowLeft");
    await expect(page).toHaveURL(/day\/2026-11-19/);
    await page.keyboard.press("j");
    await expect(page).toHaveURL(/day\/2026-11-20/);
  });

  test("daybook screenshot", async ({ page }) => {
    await seed(page, "daybook");
    await page.goto(`day/${BUSY}`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${SHOTS}/day-busy-daybook.png`, fullPage: true });
  });
});
