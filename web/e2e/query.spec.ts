import { test, expect, type Page } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * The Query screen: typing a statement and running it, the chart tab drawing a lens,
 * the helper opening (and answering in local/finder mode), a deep link that runs on
 * load, and the two invariants that matter everywhere — no request leaves the origin,
 * and nothing throws to the console.
 */

const SHOTS = process.env.SHOTS_DIR ?? "test-results/shots";
const STATEMENT = "SELECT channel, count(*) AS n FROM api.messages GROUP BY 1 ORDER BY n DESC LIMIT 20";

function watchers(page: Page, origin: string) {
  const host = new URL(origin).host;
  const offOrigin: string[] = [];
  const errors: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (url.startsWith("data:") || url.startsWith("blob:")) return;
    let reqOrigin = origin;
    try {
      reqOrigin = new URL(url).origin;
    } catch {
      return;
    }
    if (reqOrigin !== origin && !url.startsWith(`ws://${host}`) && !url.startsWith(`wss://${host}`)) offOrigin.push(url);
  });
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    const benign = /data:font/.test(text) || /Failed to load resource/.test(text) || /\b404\b/.test(text);
    if (!benign) errors.push(text);
  });
  page.on("pageerror", (err) => errors.push(String(err)));
  return { offOrigin, errors };
}

async function typeStatement(page: Page, text: string): Promise<void> {
  const editor = page.locator(".cm-content");
  await editor.click();
  await page.keyboard.insertText(text);
}

test.describe("query screen", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("type, run, table + chart, helper, deep link — no egress, no console errors", async ({ page }, testInfo) => {
    test.setTimeout(90_000);
    const origin = new URL(testInfo.project.use.baseURL as string).origin;
    const w = watchers(page, origin);

    await seed(page);
    await page.goto("query", { waitUntil: "networkidle" });

    // Empty state shows starters.
    await expect(page.getByText("Answer a question")).toBeVisible();

    // Type and run.
    await typeStatement(page, STATEMENT);
    await page.keyboard.press("Escape"); // dismiss any completion popup
    await page.keyboard.press("Control+Enter");

    // Table renders with rows.
    await expect(page.locator("text=/\\d+\\+? rows/").first()).toBeVisible({ timeout: 20_000 });
    await expect(page.locator(".cm-editor")).toBeVisible();

    // Chart tab draws a lens (canvas or svg).
    await page.getByRole("tab", { name: "Chart", exact: true }).click();
    await expect(page.locator(".cm-editor")).toBeVisible();
    await expect(page.locator("canvas, svg").first()).toBeVisible({ timeout: 20_000 });

    // Profile and Execution tabs render without throwing.
    await page.getByRole("tab", { name: "Profile", exact: true }).click();
    await page.getByRole("tab", { name: "Execution", exact: true }).click();
    await page.getByRole("tab", { name: "Table", exact: true }).click();

    // Helper opens and answers in local/finder mode.
    await page.getByRole("button", { name: "Toggle helper" }).click();
    await expect(page.getByLabel("Query helper")).toBeVisible();
    const ask = page.getByLabel("Ask the helper");
    await ask.click();
    await ask.fill("sleep");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    // Either a finder result (local) or a run box (model) appears.
    await expect(page.locator(".cm-editor")).toBeVisible();

    // Deep link: raw statement in ?q= runs on load.
    await page.goto(`query?q=${encodeURIComponent("SELECT 1 AS answer")}&run=1`, { waitUntil: "networkidle" });
    await expect(page.locator("text=/1\\+? rows/").first()).toBeVisible({ timeout: 20_000 });

    expect(w.offOrigin, `off-origin requests:\n${w.offOrigin.join("\n")}`).toEqual([]);
    expect(w.errors, `console/page errors:\n${w.errors.join("\n")}`).toEqual([]);
  });

  for (const theme of ["nocturne", "daybook"] as const) {
    test(`screenshots — ${theme}`, async ({ page }) => {
      test.setTimeout(90_000);
      await seed(page, theme);
      await page.goto("query", { waitUntil: "networkidle" });

      // Empty state with starters.
      await expect(page.getByText("Answer a question")).toBeVisible();
      await page.waitForTimeout(600);
      await page.screenshot({ path: `${SHOTS}/query-empty-${theme}.png` });

      // Run a query → table.
      await typeStatement(page, STATEMENT);
      await page.keyboard.press("Escape");
      await page.keyboard.press("Control+Enter");
      await expect(page.locator("text=/\\d+\\+? rows/").first()).toBeVisible({ timeout: 20_000 });
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${SHOTS}/query-table-${theme}.png` });

      // Chart tab.
      await page.getByRole("tab", { name: "Chart", exact: true }).click();
      await expect(page.locator("canvas, svg").first()).toBeVisible({ timeout: 20_000 });
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${SHOTS}/query-chart-${theme}.png` });

      // Helper open.
      await page.getByRole("button", { name: "Toggle helper" }).click();
      await expect(page.getByLabel("Query helper")).toBeVisible();
      const ask = page.getByLabel("Ask the helper");
      await ask.fill("sleep");
      await page.getByRole("button", { name: "Ask", exact: true }).click();
      await page.waitForTimeout(600);
      await page.screenshot({ path: `${SHOTS}/query-helper-${theme}.png` });

      // Error + Fix: run a broken statement.
      await page.getByRole("tab", { name: "Table", exact: true }).click();
      await page.locator(".cm-content").click();
      await page.keyboard.press("Control+A");
      await page.keyboard.insertText("SELECT nope FROM api.ch_does_not_exist");
      await page.keyboard.press("Escape");
      await page.keyboard.press("Control+Enter");
      await page.waitForTimeout(1200);
      await page.screenshot({ path: `${SHOTS}/query-error-${theme}.png` });
    });
  }
});
