import type { Page } from "@playwright/test";

export const TOKEN = process.env.THROUGHLINE_TOKEN ?? "";

/** Seed the token (sessionStorage) and a theme (localStorage) before load. */
export async function seed(page: Page, theme: "nocturne" | "daybook" = "nocturne"): Promise<void> {
  await page.addInitScript(
    ([tok, th]) => {
      try {
        if (tok) sessionStorage.setItem("tl.token", tok);
        localStorage.setItem(
          "tl.prefs",
          JSON.stringify({ theme: th, density: "observatory", veil: false, mapTiles: false, disclosure: "schema", tz: "UTC" }),
        );
      } catch {
        /* ignore */
      }
    },
    [TOKEN, theme],
  );
}

/** The rail items to visit in the smoke test (paths, not labels). */
export const RAIL_PATHS = ["/", "/sources", "/live", "/query", "/people", "/places", "/graph", "/ledger", "/settings", "/styleguide"];
