import { test, expect } from "@playwright/test";
import { seed, TOKEN, RAIL_PATHS } from "./helpers";

test.describe("shell smoke", () => {
  test("sign-in screen appears without a token or session", async ({ page }) => {
    await page.goto("/ui/");
    await expect(page.getByRole("heading", { name: "Throughline" })).toBeVisible();
    // Every configuration offers the token way in, directly or as a link.
    await expect(page.getByText(/access token/i).first()).toBeVisible();
  });

  test.describe("authenticated", () => {
    test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run authenticated tests");

    for (const theme of ["nocturne", "daybook"] as const) {
      test(`navigates every rail item (${theme})`, async ({ page }) => {
        await seed(page, theme);
        await page.goto("/ui/");

        // The shell renders (rail present), not the token screen.
        await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
        await expect(page.getByText("Access token")).toHaveCount(0);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);

        for (const path of RAIL_PATHS) {
          await page.goto(`/ui${path === "/" ? "/" : path}`);
          // The main region mounts and the route resolves (no error boundary).
          await expect(page.locator("#main")).toBeVisible();
          await expect(page.getByText("This screen hit an error")).toHaveCount(0);
        }
      });
    }

    test("command palette opens and finds channels", async ({ page }) => {
      await seed(page);
      await page.goto("/ui/");
      // Wait for the shell (which registers the ⌘K handler) to mount.
      await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
      await page.keyboard.press("Control+k");
      await expect(page.getByPlaceholder(/Jump to a channel/i)).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByPlaceholder(/Jump to a channel/i)).toHaveCount(0);
    });
  });
});
