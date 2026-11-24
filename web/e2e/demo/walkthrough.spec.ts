import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

/*
 * Onboarding walkthrough on a real, fresh Throughline (no mocks), recorded as
 * a video. See e2e/demo/README.md for the demo server and invented data.
 * DEMO_DIR holds config/setup-code (written by the server) and data/ (from
 * make_demo_data.py). The demo connectors read invented recordings: their
 * fixture paths are set through the API before the Connect step, everything
 * else is typed into the wizard.
 */

const DEMO = process.env.DEMO_DIR ?? "";
const DATA = `${DEMO}/data`;

async function caption(page: Page, text: string): Promise<void> {
  await page.evaluate((t) => {
    let el = document.getElementById("demo-caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "demo-caption";
      el.setAttribute("style", [
        "position:fixed", "left:50%", "bottom:22px", "transform:translateX(-50%)", "z-index:99999",
        "padding:10px 18px", "border-radius:999px", "background:rgba(10,12,15,.86)", "color:#ECE6D8",
        "font:500 15px 'Instrument Sans Variable',system-ui,sans-serif", "letter-spacing:.01em",
        "border:1px solid rgba(217,166,74,.55)", "box-shadow:0 8px 30px rgba(0,0,0,.35)",
        "pointer-events:none", "transition:opacity .25s", "max-width:80vw", "text-align:center",
      ].join(";"));
      document.body.appendChild(el);
    }
    el.textContent = t;
    el.style.opacity = t ? "1" : "0";
  }, text);
}

async function pause(page: Page, ms = 900): Promise<void> {
  await page.waitForTimeout(ms);
}

async function type(page: Page, label: string | RegExp, value: string, exact = false): Promise<void> {
  const field = page.getByLabel(label, { exact }).first();
  await field.click();
  await field.pressSequentially(value, { delay: 28 });
}

async function api(page: Page, method: "PUT" | "POST", path: string, data: unknown): Promise<void> {
  const r = await page.request.fetch(`/v1${path}`, { method, data, headers: { "X-Throughline-Client": "web" } });
  if (!r.ok()) throw new Error(`${method} ${path}: ${r.status()} ${await r.text()}`);
}

async function next(page: Page, name: RegExp = /^Continue/): Promise<void> {
  await pause(page, 600);
  await page.getByRole("button", { name }).last().click();
  await pause(page, 900);
}

const T0 = Date.now();
function mark(name: string): void {
  // Seconds since the test (and the recording) started; the video script
  // speeds up the long import wait between import-start and import-end.
  console.log(`MARK ${name} ${((Date.now() - T0) / 1000).toFixed(1)}`);
}

test("onboarding walkthrough", async ({ page }) => {
  test.skip(!DEMO, "demo recording only: set DEMO_DIR (see the header)");
  await page.addInitScript(() => {
    try {
      localStorage.setItem("tl.prefs", JSON.stringify({ theme: "nocturne", density: "observatory", veil: false, mapTiles: false, disclosure: "schema", tz: "America/Los_Angeles" }));
    } catch { /* ignore */ }
    // A visible cursor for the recording.
    window.addEventListener("DOMContentLoaded", () => {
      const dot = document.createElement("div");
      dot.setAttribute("style", "position:fixed;z-index:100000;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;background:rgba(217,166,74,.35);border:2px solid #D9A64A;pointer-events:none;transition:transform .08s;left:-40px;top:-40px");
      document.body.appendChild(dot);
      document.addEventListener("mousemove", (e) => { dot.style.left = e.clientX + "px"; dot.style.top = e.clientY + "px"; }, true);
      document.addEventListener("mousedown", () => { dot.style.transform = "scale(.7)"; }, true);
      document.addEventListener("mouseup", () => { dot.style.transform = "scale(1)"; }, true);
    });
  });

  const FROM = process.env.FROM ?? "";
  const steps = ["account", "models", "photos", "sources", "connect", "companion", "import", "people", "lenses", "pages", "queries", "agent", "tour"];
  const at = (step: string) => !FROM || steps.indexOf(step) >= steps.indexOf(FROM);
  if (FROM) {
    // Iterating on a later step: sign in with the account a full run made.
    const r = await page.request.post("/v1/auth/login", {
      data: { username: "jordan", password: "orchard lantern seventeen", remember: true },
      headers: { "X-Throughline-Client": "web" },
    });
    if (!r.ok()) throw new Error("login failed: " + (await r.text()));
    await page.goto(`/ui/welcome/${FROM}`);
    await page.getByRole("heading", { level: 1 }).waitFor();
  }
  const code = FROM ? "" : readFileSync(`${DEMO}/config/setup-code`, "utf8").trim();

  // ── First run: the link the server printed ──
  if (!FROM) {
  await page.goto(`/ui/welcome?setup=${code}`);
  await page.getByLabel("Handle").waitFor();
  await caption(page, "A fresh install. The server printed a one-time setup link.");
  await pause(page, 1800);
  await type(page, "Handle", "jordan");
  await type(page, "Display name", "Jordan Reyes");
  await type(page, "Password", "orchard lantern seventeen", true);
  await type(page, "Confirm password", "orchard lantern seventeen");
  await pause(page, 500);
  await page.getByRole("button", { name: /Create account/ }).click();

  // ── Welcome ──
  await page.getByRole("heading", { level: 1 }).waitFor();
  await caption(page, "Start fresh, restore a backup, or move from another system.");
  await pause(page, 2200);
  await page.getByRole("radio").first().click().catch(() => {});
  await next(page, /Begin|Continue/);

  // ── Account (already done) ──
  if (await page.getByRole("heading", { level: 1 }).filter({ hasText: /signed in|account/i }).count()) await next(page);
  }

  if (at("models")) {
  // ── Intelligence: bring your own keys, tested live ──
  await caption(page, "Bring your own keys: OpenRouter, Voyage, OpenAI, Anthropic, a CLI proxy or local models.");
  await pause(page, 1500);
  // Embeddings: try a local server that isn't running → a clear error.
  const emb = page.getByRole("radiogroup", { name: /Embeddings provider/ });
  await emb.getByRole("radio", { name: /^Local models/ }).click();
  await pause(page, 800);
  const localUrl = page.getByLabel(/Server URL/).first();
  await localUrl.fill("");
  await localUrl.pressSequentially("http://127.0.0.1:11434/v1", { delay: 25 });
  await page.getByRole("button", { name: /Test connection/ }).first().click();
  await caption(page, "Every key is tested before it's saved, and errors say what to do next.");
  await pause(page, 3200);
  await emb.getByRole("radio", { name: /^Skip/ }).click();
  await pause(page, 600);
  // Librarian: the owner's CLI proxy (its key is set on the server, never typed here).
  const lib = page.getByRole("radiogroup", { name: /Librarian/ });
  await lib.scrollIntoViewIfNeeded();
  await caption(page, "Lenses and pages run on a CLI proxy for an existing subscription: no per-call cost.");
  await lib.getByRole("radio", { name: /^CLI proxy/ }).click();
  await pause(page, 800);
  const base = page.getByLabel(/^Base URL/).last();
  await base.fill("");
  await base.pressSequentially(process.env.DEMO_MODEL_URL ?? "http://127.0.0.1:8317/v1", { delay: 25 });
  await page.getByRole("button", { name: /Test connection/ }).last().click();
  await page.getByText(/models offered/i).last().waitFor({ timeout: 30_000 });
  await pause(page, 1800);
  await page.getByRole("button", { name: /^Save$/ }).last().click();
  await pause(page, 900);
  const model = page.locator("#cli_proxy-model").last();
  await model.click();
  await model.pressSequentially(process.env.DEMO_MODEL ?? "gpt-6-luna", { delay: 40 });
  await model.press("Tab");
  await pause(page, 1500);
  await next(page, /^Continue/);

  }
  if (at("photos")) {
  // ── Photos: bring your own Immich ──
  await caption(page, "Already run Immich? Connect it and your photos join the timeline.");
  await pause(page, 2200);
  await next(page, /^Skip$/);

  }
  if (at("sources")) {
  // ── Sources: only what you use ──
  await caption(page, "Pick only the sources you use. Popular ones first, search for the rest.");
  await pause(page, 1500);
  for (const name of ["Beeper", "Wave", "Email (IMAP)", "Google Timeline"]) {
    const search = page.getByPlaceholder(/Search by name/);
    await search.fill("");
    await search.pressSequentially(name.split(" ")[0]!, { delay: 40 });
    await pause(page, 500);
    await page.getByRole("button", { name: new RegExp(`^${name.replace(/[()]/g, "\\$&")}`) }).first().click();
    await pause(page, 500);
  }
  await page.getByPlaceholder(/Search by name/).fill("");
  await caption(page, "Third-party plugins install straight from a GitHub link.");
  const repo = page.getByLabel("Repository URL");
  await repo.scrollIntoViewIfNeeded();
  await repo.pressSequentially("https://github.com/example/not-a-real-plugin-repo", { delay: 18 });
  await page.getByRole("button", { name: /^Add$/ }).click();
  await page.getByText(/Repository not found|isn't a Throughline|Couldn't download/i).first().waitFor({ timeout: 30_000 }).catch(() => {});
  await pause(page, 2600);
  await next(page, /Continue with/);

  }
  if (at("connect")) {
  // Point the demo connectors at their invented recordings (not typed: they
  // are test settings, not something an owner fills in).
  await api(page, "PUT", "/setup/plugins/imap/config", { values: { IMAP_FIXTURE: `${DATA}/mailbox.json` } });
  await api(page, "PUT", "/setup/plugins/wave/config", { values: { WAVE_API_FIXTURE: `${DATA}/wave.json` } });
  await api(page, "PUT", "/setup/plugins/beeper/config", { values: { BEEPER_RECORDING: `${DATA}/beeper.json` } });

  // ── Connect: one card per source ──
  await caption(page, "Each source explains where to find its credentials, then tests them.");
  await pause(page, 1500);
  const card = (name: RegExp) => page.getByRole("button", { name }).first();
  const saveAndTest = async () => {
    await page.getByRole("button", { name: /^Save/ }).first().click();
    await pause(page, 700);
    await page.getByRole("button", { name: /^Test/ }).first().click();
    await page.getByText(/can read your data|Connected/i).first().waitFor({ timeout: 60_000 });
    await pause(page, 1500);
  };
  // Email
  await card(/Email \(IMAP\)/).click();
  await pause(page, 600);
  await type(page, "IMAP server", "imap.example.com");
  await type(page, "Login", "jordan.reyes@example.com");
  await type(page, "Password or app password", "demo-app-password");
  await saveAndTest();
  // Wave
  await card(/^Wave/).click();
  await pause(page, 600);
  await type(page, "API token", "wv_demo_token_4f2a");
  await saveAndTest();
  // Beeper
  await card(/^Beeper/).click();
  await pause(page, 600);
  await type(page, "Desktop API token", "bp_demo_token_91c0");
  await saveAndTest();
  // Google Timeline: upload the export
  await card(/Google Timeline/).click();
  await caption(page, "Location history from a Google Timeline export: drop the file in.");
  await pause(page, 800);
  await page.locator('input[type="file"]').first().setInputFiles(`${DATA}/Timeline.json`);
  await page.getByText(/Timeline\.json/).first().waitFor();
  await pause(page, 1200);
  await page.getByRole("button", { name: /^Save/ }).first().click();
  await pause(page, 1500);
  await next(page);

  }
  if (at("companion")) {
  // ── Companion ──
  await caption(page, "Sources on your computer run through the desktop companion: download, paste a code.");
  await pause(page, 1500);
  await page.getByRole("button", { name: /Generate pairing code/ }).click().catch(() => {});
  await pause(page, 3000);
  await next(page, /^Skip|^Continue/);

  }
  if (at("import")) {
  // ── Import: history now, not months from now ──
  await caption(page, "Import years of history right away, with live progress for every source.");
  await page.getByRole("button", { name: /Start import/ }).click();
  await pause(page, 6000);
  mark("import-start");
  await caption(page, "It keeps going in the background. (Sped up for the video.)");
  await page.getByText(/All imports finished|Up to date|imported/i).first().waitFor({ timeout: 10 * 60_000 }).catch(() => {});
  for (let i = 0; i < 600; i++) {
    const running = await page.evaluate(async () => {
      const r = await fetch("/v1/setup/jobs", { headers: { "X-Throughline-Client": "web" } });
      const j = await r.json();
      return j.summary.running + j.summary.queued;
    });
    if (!running) break;
    await page.waitForTimeout(1000);
  }
  mark("import-end");
  await caption(page, "Done: messages, email, meetings and 18 months of location.");
  await pause(page, 3500);
  await next(page);

  }
  if (at("people")) {
  // ── People ──
  await caption(page, "Who's who: first, which of these is you?");
  await pause(page, 2000);
  await page.getByRole("button", { name: /Jordan Reyes/ }).first().click();
  await pause(page, 1500);
  const link = page.getByRole("button", { name: /Link my people/ });
  if (await link.count()) await link.click();
  await caption(page, "Certain matches are linked automatically. Only real ambiguity becomes a question.");
  mark("people-start");
  await page.getByText(/same person as/i).first().waitFor({ timeout: 120_000 });
  mark("people-end");
  await pause(page, 2500);
  const showLinked = page.getByRole("button", { name: /Show what was linked/ });
  if (await showLinked.count()) {
    await showLinked.click();
    await pause(page, 3000);
    await showLinked.click().catch(() => {});
  }
  for (const answer of [/^Same person/, /^Same person/, /^Different people/]) {
    if (!(await page.getByText(/same person as/i).count())) break;
    await pause(page, 1800);
    await page.getByRole("button", { name: answer }).first().click();
    await pause(page, 1200);
  }
  await caption(page, "Answers are saved as yours; anything can be split later.");
  await pause(page, 2000);
  await next(page);

  }
  // ── Lenses ──
  if (at("lenses")) {
  await caption(page, "Lenses: tables an AI keeps filled from your conversations. Pick a pace: speed versus cost.");
  await page.getByRole("heading", { level: 1 }).waitFor();
  await pause(page, 2200);
  await page.getByRole("radiogroup", { name: /How often/ }).getByRole("radio", { name: /^Thrifty/ }).click();
  await pause(page, 1500);
  await page.getByRole("radiogroup", { name: /How often/ }).getByRole("radio", { name: /^Balanced/ }).click();
  await pause(page, 1200);
  await page.getByRole("button", { name: /^Create \d+ lens/ }).click();
  await page.getByText(/days to read|free/i).first().waitFor({ timeout: 60_000 });
  await caption(page, "Each lens shows what its first run will read and cost before anything runs.");
  await pause(page, 2500);
  await page.getByRole("button", { name: /^Run now/ }).first().click();
  mark("lens-start");
  await caption(page, "Running the Plans lens on two weeks of invented conversations. (Sped up for the video.)");
  await page.locator("table").first().waitFor({ timeout: 15 * 60_000 }).catch(() => {});
  mark("lens-end");
  await caption(page, "Its rows are a SQL view your agents can query: api.lens_starter_plans.");
  await pause(page, 4000);
  await next(page);
  }

  // ── Pages ──
  if (at("pages")) {
  await caption(page, "Pages write themselves: one per day, one per person, a weekly rollup.");
  await page.getByRole("heading", { level: 1 }).waitFor();
  await pause(page, 2200);
  await page.getByRole("button", { name: /^Save pages|^Update/ }).click();
  await pause(page, 2000);
  await page.getByRole("button", { name: /Write my first pages/ }).click();
  mark("pages-start");
  await caption(page, "Writing yesterday's page from the evidence, with every claim cited. (Sped up for the video.)");
  await page.locator("article").first().waitFor({ timeout: 4 * 60_000 }).catch(() => {});
  mark("pages-end");
  await page.locator("article").first().scrollIntoViewIfNeeded().catch(() => {});
  await pause(page, 5000);
  await next(page);
  }

  // ── Queries ──
  if (at("queries")) {
  await caption(page, "First questions, chosen from your data. Run them, tweak them, save them.");
  await page.getByRole("heading", { level: 1 }).waitFor();
  await pause(page, 2000);
  const runs = page.getByRole("button", { name: /^Run$/ });
  for (const i of [1, 3]) {
    if ((await runs.count()) > i) {
      await runs.nth(i).scrollIntoViewIfNeeded();
      await runs.nth(i).click();
      await pause(page, 3000);
    }
  }
  await caption(page, "Everything here is plain SQL, search or a timeline — and your agent can run it too.");
  await pause(page, 2500);
  await next(page);
  }

  if (at("agent")) {
  // ── Agent ──
  await caption(page, "Connect Claude Code, Claude Desktop, Cursor, Codex or ChatGPT over MCP.");
  await pause(page, 1800);
  await page.getByRole("button", { name: /Create a token for this agent/ }).click();
  await pause(page, 2500);
  await page.getByRole("tab", { name: /Cursor/ }).click().catch(() => {});
  await pause(page, 1500);
  await page.getByRole("button", { name: /Set up with an agent instead/ }).click();
  await caption(page, "Or paste this prompt into any agent and it walks you through the same setup.");
  await pause(page, 3500);
  await page.keyboard.press("Escape");
  await pause(page, 600);
  await next(page);

  }
  if (at("tour")) {
  // ── Tour ──
  await caption(page, "A quick tour, then you're in.");
  await pause(page, 3500);
  await page.getByRole("button", { name: /Finish/ }).last().click();
  await expect(page).not.toHaveURL(/\/welcome/, { timeout: 20_000 });
  }
  await caption(page, "");
  await pause(page, 3000);
});
