import { test, expect, type Request } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

const BASE = "/ui/";

test.describe("The Loom", () => {
  test.skip(!TOKEN, "needs THROUGHLINE_TOKEN");

  test("renders the vitals sentence, a lane for every channel, and the canvas", async ({ page }) => {
    // Real JS errors only; "Failed to load resource" is a network-status log the
    // browser emits for any non-2xx fetch (e.g. the shell's own state probe,
    // which the app handles gracefully) and is not a rendering error.
    const consoleErrors: string[] = [];
    page.on("console", (m) => {
      if (m.type() === "error" && !/Failed to load resource/i.test(m.text())) consoleErrors.push(m.text());
    });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror ${e.message}`));

    await seed(page);
    await page.goto(BASE, { waitUntil: "domcontentloaded" });

    // Vitals sentence (each number is a link into Query).
    await expect(page.getByText(/records from/i)).toBeVisible();

    // The signature Loom: canvas + an SVG overlay for marks and the brush.
    await expect(page.getByTestId("loom-canvas")).toBeVisible();
    await expect(page.getByTestId("loom-overlay")).toBeVisible();

    // Plugins with many small channels auto-collapse to a summary band to keep
    // the gutter legible; expand them all so every channel has its own lane.
    for (let i = 0; i < 20; i++) {
      const collapsed = page.getByTestId("loom").locator('[aria-expanded="false"]');
      const n = await collapsed.count();
      if (n === 0) break;
      await collapsed.first().click();
    }

    // One lane per visible channel once everything is expanded.
    const boot = await page.evaluate(async (tok) => {
      const r = await fetch("/v1/web/bootstrap", { headers: { Authorization: `Bearer ${tok}` } });
      return r.json();
    }, TOKEN);
    const channelCount = boot.channels.length as number;
    await expect.poll(() => page.getByTestId("loom-lane").count()).toBe(channelCount);

    // No console errors (the shell's own state 404 is network, not a console error).
    expect(consoleErrors).toEqual([]);
  });

  test("river toggle redraws the same data without error", async ({ page }) => {
    await seed(page);
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("loom-canvas")).toBeVisible();
    await page.getByRole("radio", { name: "River" }).click();
    await expect(page.getByRole("radio", { name: "River" })).toHaveAttribute("aria-checked", "true");
    // Canvas still present after the transition.
    await expect(page.getByTestId("loom-canvas")).toBeVisible();
  });

  test("brushing the Loom sets the global time scope (reflected in the URL)", async ({ page }) => {
    await seed(page);
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    const overlay = page.getByTestId("loom-overlay");
    await expect(overlay).toBeVisible();
    const box = (await overlay.boundingBox())!;
    const y = box.y + box.height / 2;
    await page.mouse.move(box.x + box.width * 0.3, y);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.6, y, { steps: 8 });
    await page.mouse.up();
    // The scope is linkable: the URL gains a from/to range.
    await expect.poll(() => page.url()).toMatch(/from=/);
  });

  test("clicking a lane opens that channel", async ({ page }) => {
    await seed(page);
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    await page.getByTestId("loom-lane").first().click();
    await expect.poll(() => page.url()).toMatch(/\/ui\/c\//);
  });

  test("no request leaves the app origin", async ({ page }, testInfo) => {
    const origin = new URL(testInfo.project.use.baseURL as string).origin;
    const host = new URL(origin).host;
    const offOrigin: string[] = [];
    page.on("request", (req: Request) => {
      const url = req.url();
      if (url.startsWith("data:") || url.startsWith("blob:")) return;
      let reqOrigin: string;
      try { reqOrigin = new URL(url).origin; } catch { return; }
      if (reqOrigin !== origin && !url.startsWith(`ws://${host}`) && !url.startsWith(`wss://${host}`)) {
        offOrigin.push(url);
      }
    });
    await seed(page);
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    await page.getByTestId("loom-canvas").waitFor();
    await page.waitForTimeout(1500);
    expect(offOrigin, `off-origin: ${offOrigin.join(", ")}`).toEqual([]);
  });
});
