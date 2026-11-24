import { test, expect, type Page } from "@playwright/test";
import { seedSetupMock, seedAuth } from "./fixtures/setupMock";

/*
 * The first-run wizard, driven entirely by the stateful mock in
 * fixtures/setupMock.ts (no backend needed). Invariants:
 *   1. no request leaves the app origin;
 *   2. the fresh flow walks every step and completes;
 *   3. errors render as helpful SetupProblems (failing checks, failed imports);
 *   4. the restore path greets a returning install and shows connected sources;
 *   5. the pre-sign-in gate offers account creation when no owner exists.
 */

function watch(page: Page): { offOrigin: string[]; errors: string[] } {
  const offOrigin: string[] = [];
  const errors: string[] = [];
  page.on("request", (req) => {
    const u = req.url();
    if (u.startsWith("data:") || u.startsWith("blob:")) return;
    try {
      if (new URL(u).origin !== new URL(page.url() || "http://127.0.0.1").origin && !u.includes("127.0.0.1") && !u.includes("localhost")) offOrigin.push(u);
    } catch {
      /* ignore */
    }
  });
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  return { offOrigin, errors };
}

async function clickButton(page: Page, name: RegExp | string): Promise<void> {
  await page.getByRole("button", { name }).first().click();
}

test.describe("onboarding wizard", () => {
  test("fresh flow walks every step and finishes", async ({ page }) => {
    const seen = watch(page);
    await seedAuth(page);
    seedSetupMock(page);

    await page.goto("/ui/welcome");

    // welcome
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/one hub|pick up/i);
    await clickButton(page, /Begin/);

    // account (signed in)
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/signed in/i);
    await clickButton(page, /Continue/);

    // models
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/sense of meaning/i);
    await expect(page.getByText("Embeddings", { exact: true })).toBeVisible();
    await expect(page.getByText("Assistant", { exact: true })).toBeVisible();
    await clickButton(page, /Continue/);

    // photos
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/photos/i);
    await clickButton(page, /^Skip$/);

    // sources — preselected sources carry through
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/choose your sources/i);
    await expect(page.getByText("Already connected · 3,650 records")).toBeVisible();
    await clickButton(page, /Continue with/);

    // connect — a failing check renders a SetupProblem
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/connect what you picked/i);
    await page.getByRole("button", { name: /GBrain/ }).first().click();
    await page.getByRole("button", { name: /^Test$/ }).first().click();
    await expect(page.getByText(/Couldn't reach the GBrain database/i)).toBeVisible();
    await clickButton(page, /Continue/);

    // companion
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/into the loop/i);
    await page.getByRole("button", { name: /Generate pairing code/ }).click();
    await expect(page.getByText(/tlc1\./).first()).toBeVisible();
    await clickButton(page, /Skip/);

    // import — live progress with a failed row and retry
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/pull your past in/i);
    await expect(page.getByText(/records imported so far/i)).toBeVisible();
    await expect(page.getByText(/points/).first()).toBeVisible();
    await expect(page.getByText(/Wave rejected the token/i)).toBeVisible();
    await clickButton(page, /Continue/);

    // people — me, then answer a question
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/make sense of your people/i);
    await page.getByRole("button", { name: /You \(Wave\)/ }).click();
    await expect(page.getByText(/Linked 212 accounts/i)).toBeVisible();
    await expect(page.getByText(/Birdie/).first()).toBeVisible();
    await clickButton(page, /Same person/);
    await clickButton(page, /Skip the rest/);

    // lenses — create starter lenses, see the quotes
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/keep notes/i);
    await expect(page.getByText("Balanced", { exact: true })).toBeVisible();
    await clickButton(page, /Create \d/);
    await expect(page.getByText(/to read|free/i).first()).toBeVisible();
    await clickButton(page, /Continue/);

    // pages — choose families, save, see the plan
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/running record/i);
    await clickButton(page, /Save pages/);
    await expect(page.getByText(/pages ready to write/i).first()).toBeVisible();
    await clickButton(page, /Continue/);

    // queries — run one inline
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/one question away/i);
    await page.getByRole("button", { name: /^Run$/ }).first().click();
    await expect(page.getByText(/Q4 draft/i).first()).toBeVisible();
    await clickButton(page, /Continue/);

    // agent — mint a token, show-once warning
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/point your ai/i);
    await page.getByRole("button", { name: /Create a token for this agent/ }).click();
    await expect(page.getByText(/shown once/i)).toBeVisible();
    await clickButton(page, /Continue/);

    // tour → finish
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/lay of the land/i);
    await clickButton(page, /Finish & explore/);

    await expect(page).not.toHaveURL(/\/welcome/);
    expect(seen.offOrigin, `off-origin requests: ${seen.offOrigin.join(", ")}`).toEqual([]);
  });

  test("restore path greets a returning install", async ({ page }) => {
    await seedAuth(page);
    seedSetupMock(page, { existing: true });
    await page.goto("/ui/welcome/welcome?resume=1");

    await expect(page.getByText(/Welcome back/i)).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/pick up where you left off/i);
    await expect(page.getByText("Already connected", { exact: true })).toBeVisible();
    await expect(page.getByText(/Oura/).first()).toBeVisible();
  });

  test("lenses run and show rows; pages write and render markdown", async ({ page }) => {
    await seedAuth(page);
    seedSetupMock(page);

    await page.goto("/ui/welcome/lenses");
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/keep notes/i);
    await clickButton(page, /Create \d/);
    await page.getByRole("button", { name: /Run now/ }).first().click();
    await expect(page.getByText(/Lisbon/i).first()).toBeVisible({ timeout: 15_000 });

    await page.goto("/ui/welcome/pages");
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/running record/i);
    await clickButton(page, /Save pages/);
    await page.getByRole("button", { name: /Write my first pages/ }).click();
    await expect(page.getByText(/Open loops/i)).toBeVisible({ timeout: 15_000 });
  });

  test("models step offers the librarian role with a discovered model list", async ({ page }) => {
    await seedAuth(page);
    seedSetupMock(page);
    await page.goto("/ui/welcome/models");
    await expect(page.getByText("Librarian, lenses & pages")).toBeVisible();
    // Pick the CLI proxy for the semantic role; it has no fixed model list.
    await page.getByRole("radiogroup", { name: /Librarian/i }).getByRole("radio", { name: /CLI proxy/ }).click();
    await page.getByRole("button", { name: /Test connection/ }).last().click();
    await expect(page.getByText(/3 models/i)).toBeVisible();
  });

  test("add-from-github surfaces a helpful problem on a bad URL", async ({ page }) => {
    await seedAuth(page);
    seedSetupMock(page);
    await page.goto("/ui/welcome/sources");
    await page.getByLabel("Repository URL").fill("not-a-repo");
    await page.getByRole("button", { name: /^Add$/ }).click();
    await expect(page.getByText(/doesn't look like a repository/i)).toBeVisible();
  });

  test("gate offers account creation when no owner exists", async ({ page }) => {
    // No token seeded → signed out. needs_account → the gate shows.
    seedSetupMock(page, { needsAccount: true });
    await page.addInitScript(() => {
      try { localStorage.setItem("tl.prefs", JSON.stringify({ theme: "nocturne", density: "observatory", veil: false, mapTiles: false, disclosure: "schema", tz: "UTC" })); } catch { /* ignore */ }
    });
    await page.goto("/ui/");
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/create your owner account/i);
    await page.getByLabel("Handle").fill("me");
    await page.getByLabel("Password", { exact: true }).fill("a-strong-passphrase");
    await page.getByLabel("Confirm password").fill("a-strong-passphrase");
    await page.getByLabel("Setup code").fill("ABCD-1234");
    await page.getByRole("button", { name: /Create account/ }).click();
    // After creation the app adopts the session and leaves the gate.
    await expect(page.getByText(/Create your owner account/i)).toHaveCount(0, { timeout: 10_000 });
  });
});
