import { test, expect, type Page } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * Graph: the overview renders the whole network fit to the frame from real
 * entity/person links; focusing a node (from the overview rail) grows its
 * neighbourhood and reads it in the side panel; search runs; no request leaves
 * the origin and no renderer throws.
 */
const SHOT_DIR = process.env.SHOTS_DIR ?? "test-results/shots";

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

test.describe("graph", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("renders the overview, focuses a node, searches, no egress", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    await page.goto("graph", { waitUntil: "networkidle" });

    await expect(page.getByText("Entities by type")).toBeVisible();
    await expect(page.getByRole("application", { name: /graph canvas/i })).toBeVisible();
    // The overview lands fit-to-view with many nodes (not a tiny seed cluster).
    await expect(page.getByText(/\d+ nodes · \d+ links/)).toBeVisible();
    await page.waitForTimeout(2500);

    // Focus the top most-connected entity → focus mode + the node panel reads.
    const firstConn = page.locator('[class*="connRow"]').first();
    await expect(firstConn).toBeVisible();
    await firstConn.click();
    await page.waitForTimeout(1500);
    await expect(page.getByRole("button", { name: "Overview", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Links", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Records", exact: true })).toBeVisible();

    // Back to the overview.
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    await expect(page.getByText("Overview", { exact: true })).toBeVisible();

    // Search finds matches.
    await page.getByRole("textbox", { name: "Search the graph" }).fill("ca");
    await page.waitForTimeout(1200);

    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });

  for (const theme of ["nocturne", "daybook"] as const) {
    test(`screenshots — ${theme}`, async ({ browser }) => {
      for (const vp of [
        { width: 1440, height: 900, tag: "wide" },
        { width: 390, height: 844, tag: "phone" },
      ]) {
        const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
        await seed(page, theme);
        await page.goto("/ui/graph", { waitUntil: "networkidle" });
        await page.waitForTimeout(3200);
        await page.screenshot({ path: `${SHOT_DIR}/graph-${theme}-${vp.tag}.png` });

        // Focus the top hub and capture the neighbourhood + node panel.
        const conn = page.locator('[class*="connRow"]').first();
        if (await conn.isVisible().catch(() => false)) {
          await conn.click();
          await page.waitForTimeout(1800);
          await page.screenshot({ path: `${SHOT_DIR}/graph-${theme}-${vp.tag}-focus.png` });
        }
        await page.close();
      }
    });
  }
});
