import { test, expect, type Page } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { seed, TOKEN } from "./helpers";

/*
 * Sources: the health board, a plugin detail page, and Add-a-source (drop a
 * file). Invariants everywhere:
 *   1. no request leaves the app origin (no CDN, tiles, fonts, analytics);
 *   2. no uncaught page error and no renderer console error.
 * The add-a-source flow is driven end to end up to the server dry-run, which
 * registers a `devtest.`-prefixed channel on the dev DB (no records written).
 */
const SAMPLE = fileURLToPath(new URL("./fixtures/import-sample.csv", import.meta.url));

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

test.describe("sources", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("the health board renders every source, expands, and draws the freshness matrix", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    await page.goto("sources", { waitUntil: "networkidle" });

    await expect(page.locator("#main").getByRole("heading", { name: "Sources", level: 1 })).toBeVisible();

    // The freshness matrix canvas mounts.
    await expect(page.locator("canvas").first()).toBeVisible();

    // One expandable row per plugin; expanding the first reveals channel cards.
    const rows = page.getByTestId("plugin-expand");
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);
    const first = rows.first();
    await first.click();
    await expect(first).toHaveAttribute("aria-expanded", "true");

    await page.waitForTimeout(2500);
    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("a plugin detail page opens from a source link", async ({ page }) => {
    await seed(page);
    await page.goto("sources", { waitUntil: "networkidle" });
    const link = page.locator('a[href*="/ui/sources/"]:not([href$="/add"])').first();
    await link.click();
    await expect(page.getByText("All sources")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Reconnect" })).toBeVisible();
  });

  test("add-a-source: drop a CSV, mapping is inferred, preview renders, dry-run passes", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    await page.goto("sources/add", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { name: "Add a source" })).toBeVisible();

    // Drop the CSV through the hidden input.
    await page.locator('[data-testid="file-input"]').setInputFiles(SAMPLE);

    // The mapping is inferred: required slots are filled from the sample.
    const timeSlot = page.locator('#slot-happened_at');
    await expect(timeSlot).toBeVisible();
    await expect(timeSlot).toHaveValue("sent_at");
    await expect(page.locator("#slot-content")).toHaveValue("body");
    await expect(page.locator("#slot-external_id")).toHaveValue("msg_id");

    // Name the channel for a clean dev-DB artefact.
    const nameInput = page.getByLabel("Channel name");
    await nameInput.fill("devtest.webui_e2e");

    // The preview renders at least one live lens (an SVG or canvas chart).
    await expect(page.locator('figure svg, figure canvas').first()).toBeVisible({ timeout: 10000 });

    // Validate with the server dry-run.
    await page.getByRole("button", { name: "Dry run" }).click();
    await expect(page.getByText(/would be written/)).toBeVisible({ timeout: 15000 });

    await page.waitForTimeout(1500);
    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });
});
