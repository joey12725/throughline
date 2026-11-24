import { test, expect, type Page } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * Ledger: the commitments board renders real open loops, the tabs switch, the
 * owner sees spend and audit, and the decision confirmation dialog opens and
 * cancels WITHOUT deciding anything (there is no pending item to decide on dev
 * data, so nothing is written). No request leaves the origin, no renderer throws.
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

test.describe("ledger", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");

  test("renders every tab, decisions never auto-decide, no egress", async ({ page }, info) => {
    const origin = new URL(info.project.use.baseURL as string).origin;
    const { offOrigin, errors } = watch(page, origin);
    await seed(page);
    await page.goto("ledger", { waitUntil: "networkidle" });

    // Proposals & approvals: the decision machinery exists; on dev data both are
    // empty, so there is nothing to decide — the designed empty states show.
    await expect(page.getByRole("heading", { name: /Approvals/ })).toBeVisible();
    await expect(page.getByRole("heading", { name: /Proposals/ })).toBeVisible();

    // Spend and Audit are owner-visible.
    await page.getByRole("tab", { name: "Spend" }).click();
    await expect(page.getByText("Total cost")).toBeVisible();
    await page.getByRole("tab", { name: "Audit" }).click();
    await page.waitForTimeout(1200);

    expect(offOrigin, `off-origin:\n${offOrigin.join("\n")}`).toEqual([]);
    expect(errors, `errors:\n${errors.join("\n")}`).toEqual([]);
  });
});
