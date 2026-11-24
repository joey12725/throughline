import { test, expect, type Page, type Route } from "@playwright/test";
import { seed, TOKEN } from "./helpers";

/*
 * Classifiers, end to end against MOCKED classifier routes. Every
 * /v1/classifiers*, /v1/annotation-attributes, thread classification and
 * group-annotation request is answered here and recorded, so define / approve /
 * try / draft / corrections are exercised without touching live data. Only
 * bootstrap (a read) reaches the real API.
 */

const PY = {
  id: 1,
  name: "is_it_media",
  kind: "python",
  description: "Background media in voice recordings",
  attribute: "media_noise",
  channels: ["fixture.audio"],
  spec: { settle_minutes: 30, max_cost_per_run: 0.5 },
  version: 3,
  status: "active",
  monthly_cap_usd: 5,
  has_code: true,
  last_run_at: "2026-11-22T10:00:00Z",
  last_error: null,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-20T00:00:00Z",
  stats: {
    annotations: 1200,
    group_annotations: 40,
    threads_judged: 88,
    month_cost: 0.42,
    total_cost: 1.9,
    pending_threads: 3,
    last_run: { id: 9, trigger: "timer", status: "done", started_at: "2026-11-22T10:00:00Z", finished_at: "2026-11-22T10:01:00Z", items: 4, marks: 2, calls: 4, tokens: 9000, cost: 0.02, errors: [], summary: null },
  },
};
const MSG = {
  ...PY,
  id: 2,
  name: "needs_reply",
  kind: "message",
  description: "Messages that ask me something",
  attribute: "needs_reply",
  channels: [],
  spec: { worker: "jev", question: "Does this ask me to reply?", answer: "yes_no", threshold: 0.7, value: { needs: true } },
  status: "proposed",
  has_code: false,
  stats: { ...PY.stats, pending_messages: 12, pending_threads: undefined, last_run: null },
};
const ATTRS = {
  attributes: [
    { name: "media_noise", description: "Background media", value_schema: { is_noise: { type: "boolean", required: true } }, recall_effect: "exclude", flag: "is_noise", origin: "core", status: "active", active_annotations: 1200, classifiers: 1 },
    { name: "needs_reply", description: "Asks for a reply", value_schema: { needs: { type: "boolean", required: true } }, recall_effect: "none", flag: "needs", origin: "owner", status: "active", active_annotations: 0, classifiers: 0 },
  ],
};
const GUIDE = {
  summary: "s",
  workflow: [],
  message_spec: {},
  python: { contract: "def classify(thread) -> dict:\n    ...", allowed_imports: ["re", "math"], spec: {}, rules: ["No network."] },
  examples: {
    message: [{ title: "Needs a reply", definition: { kind: "message", attribute: "needs_reply", spec: { worker: "jev", question: "Does it ask me?", answer: "yes_no", threshold: 0.6, value: { needs: true } } } }],
    python: [{ title: "Media in recordings", attribute: "media_noise", channels: ["fixture.audio"], code: "def classify(thread):\n    return {'verdict': 'none'}\n" }],
  },
};
const RESULTS = {
  kind: "python",
  verdicts: { none: 70, partial: 12, full: 6 },
  items: [
    {
      thread_id: 501,
      title: "Kitchen, Saturday evening",
      channel: "fixture.audio",
      started_at: "2026-11-21T18:00:00Z",
      judged_at: "2026-11-21T19:00:00Z",
      version: 3,
      result: {
        status: "done",
        verdict: "partial",
        marks: [
          { target: "participant", key: "S2", label: "Speaker 2", value: { is_noise: true, kind: "tv", classifier: "is_it_media" }, detail: {}, confidence: 0.91, reason: "reads like a TV ad" },
          { target: "thread", key: null, label: null, value: null, detail: { level: "partial", kind: "tv" }, confidence: 0.9, reason: null },
        ],
        participants: [{ key: "S1", label: "Me", is_owner: true, turns: 30 }, { key: "S2", label: "Speaker 2", is_owner: false, turns: 12 }],
        messages: 42,
      },
    },
  ],
};
const CLASSIFICATION = {
  thread: { id: 501, channel: "fixture.audio", title: "Kitchen, Saturday evening", started_at: "2026-11-21T18:00:00Z", duration_s: 1800 },
  attribute: "media_noise",
  participants: [
    { key: "S1", label: "Me", is_owner: true, person_id: 1, turns: 30, flagged: 0 },
    { key: "S2", label: "Speaker 2", is_owner: false, person_id: null, turns: 12, flagged: 12 },
  ],
  messages: [
    { id: 1, at: "2026-11-21T18:00:00Z", offset_s: 3, participant: "S1", text: "Did you feed the cat?", annotation: null },
    { id: 2, at: "2026-11-21T18:00:05Z", offset_s: 8, participant: "S2", text: "Call now and save forty percent!", annotation: { value: { is_noise: true }, method: "model", group_annotation_id: 70, job: "is_it_media" } },
  ],
  messages_total: 42,
  flagged_total: 12,
  group_annotations: [
    { id: 70, target_kind: "participant", participant_key: "S2", value: { is_noise: true, kind: "tv", classifier: "is_it_media" }, detail: {}, method: "model", confidence: 0.91, reason: "reads like a TV ad", messages_covered: 12 },
    { id: 71, target_kind: "thread", participant_key: null, value: null, detail: { level: "partial", kind: "tv" }, method: "model", confidence: 0.9, reason: null, messages_covered: 0 },
    { id: 72, target_kind: "participant", participant_key: "S2", value: { is_noise: false }, detail: {}, method: "owner", confidence: null, reason: "it was my brother", messages_covered: 12 },
  ],
  verdicts: [{ name: "is_it_media", kind: "python", result: RESULTS.items[0]!.result, judged_at: "2026-11-21T19:00:00Z", version: 3 }],
};

interface Posted {
  path: string;
  body: unknown;
}

async function mockClassifiers(page: Page): Promise<Posted[]> {
  const posted: Posted[] = [];
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  await page.route(/\/v1\/(classifiers|annotation-attributes|group-annotations|threads\/[^/]+\/classification)/, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/v1/, "");
    if (req.method() === "POST") {
      const body = req.postDataJSON() as Record<string, unknown>;
      posted.push({ path, body });
      if (path === "/classifiers/check") {
        const code = String((body.definition as Record<string, unknown>)?.code ?? "");
        return json(route, code.includes("import os")
          ? { ok: false, errors: ["line 1: import os is not allowed"], warnings: [], grain: "thread", kind: "python" }
          : { ok: true, errors: [], warnings: [], grain: "thread", kind: "python" });
      }
      if (path === "/classifiers/try") {
        return json(route, {
          ok: true,
          items: [{ thread_id: 501, title: "Kitchen, Saturday evening", status: "done", verdict: "partial", marks: RESULTS.items[0]!.result.marks, participants: RESULTS.items[0]!.result.participants, would_cover: { messages: 12, of: 42 }, logs: "judged 2 speakers" }],
          calls: 2,
          cost: 0.004,
          cost_per_item: 0.004,
          projection: { threads_to_judge: 120, estimated_cost: 0.48 },
        });
      }
      if (path === "/classifiers/draft") return json(route, { detail: "model unreachable" }, 503);
      if (path === "/classifiers") {
        const def = body as { name: string };
        return json(route, { classifier: { ...PY, name: def.name }, check: { ok: true, errors: [], warnings: [] }, status: "active", receipt: {} });
      }
      return json(route, { ok: true, id: 99, status: "done", items: 0, marks: 0, cost: 0 });
    }
    if (path === "/classifiers") return json(route, { classifiers: [PY, MSG], sandbox: "bwrap" });
    if (path === "/classifiers/guide") return json(route, GUIDE);
    if (path === "/annotation-attributes") return json(route, ATTRS);
    if (path === "/classifiers/is_it_media") return json(route, { ...PY, code: GUIDE.examples.python[0]!.code, runs: [PY.stats.last_run], attribute_definition: ATTRS.attributes[0] });
    if (path === "/classifiers/needs_reply") return json(route, { ...MSG, runs: [], attribute_definition: ATTRS.attributes[1] });
    if (path === "/classifiers/is_it_media/results") return json(route, RESULTS);
    if (path.endsWith("/classification")) return json(route, CLASSIFICATION);
    return json(route, { detail: "not mocked" }, 404);
  });
  return posted;
}

test.describe("classifiers (mocked)", () => {
  test.skip(!TOKEN, "set THROUGHLINE_TOKEN to run");
  test.setTimeout(60_000);

  test("list: proposed stands out and approve posts status", async ({ page }) => {
    await seed(page);
    const posted = await mockClassifiers(page);
    await page.goto("classifiers");
    await expect(page.getByRole("main").getByRole("heading", { name: "Classifiers", level: 1 })).toBeVisible({ timeout: 25_000 });
    const proposed = page.getByRole("article", { name: "needs_reply" });
    await expect(proposed.getByText("Proposed by an agent")).toBeVisible();
    await proposed.getByRole("button", { name: "Approve" }).click();
    await expect.poll(() => posted.find((p) => p.path === "/classifiers/needs_reply/status")?.body).toEqual({ status: "active" });
    await page.screenshot({ path: "test-results/classifiers-list.png", fullPage: true });
  });

  test("editor: code mode prefills, checks live by line, tries and saves", async ({ page }) => {
    await seed(page);
    const posted = await mockClassifiers(page);
    await page.goto("classifiers/new");
    await page.getByRole("radio", { name: /Write code/ }).click({ timeout: 25_000 });
    await expect(page.getByText("def classify(thread):")).toBeVisible();
    await page.getByLabel("Name", { exact: true }).fill("is_it_tv");
    await expect(page.getByText("Checks pass")).toBeVisible();

    // A forbidden import shows up as a line-numbered problem.
    const editor = page.getByRole("textbox", { name: "Classifier code" });
    await editor.click();
    await page.keyboard.press("Control+Home");
    await page.keyboard.type("import os\n");
    await expect(page.getByRole("button", { name: "line 1" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Save classifier" })).toBeDisabled();
    await page.keyboard.press("Control+Home");
    await page.keyboard.press("Shift+End");
    await page.keyboard.press("Delete");
    await expect(page.getByText("Checks pass")).toBeVisible();

    // Draft with AI: a 503 is explained, not thrown.
    await page.getByLabel("Goal").fill("flag the television in recordings");
    await page.getByRole("button", { name: "Draft it" }).click();
    await expect(page.getByText(/model is unreachable/)).toBeVisible();

    // Try on a sample: verdict, marked speaker, coverage, projection.
    await expect(page.getByText("spends a few cents")).toBeVisible();
    await page.getByRole("button", { name: "Try it" }).click();
    await expect(page.getByText("would mark")).toBeVisible();
    await expect(page.getByText(/120 threads/)).toBeVisible();
    const tried = posted.find((p) => p.path === "/classifiers/try");
    expect(tried?.body).toMatchObject({ definition: { name: "is_it_tv", kind: "python", attribute: "media_noise" }, sample: { limit: 5 } });
    await page.screenshot({ path: "test-results/classifiers-editor.png", fullPage: true });

    await page.getByRole("button", { name: "Save classifier" }).click();
    await expect(page).toHaveURL(/\/classifiers\/is_it_tv$/);
    expect(posted.find((p) => p.path === "/classifiers")?.body).toMatchObject({ name: "is_it_tv", kind: "python", channels: ["fixture.audio"] });
  });

  test("editor: message mode builds the yes/no definition", async ({ page }) => {
    await seed(page);
    const posted = await mockClassifiers(page);
    await page.goto("classifiers/new");
    await page.getByLabel("Name", { exact: true }).fill("asks_me", { timeout: 25_000 });
    await page.getByRole("combobox", { name: "Attribute" }).selectOption("needs_reply");
    await page.getByLabel("Question").fill("Does this ask me something?");
    await page.getByRole("radio", { name: "Recent days" }).click();
    await expect(page.getByRole("button", { name: "Save classifier" })).toBeEnabled();
    await page.getByRole("button", { name: "Save classifier" }).click();
    await expect.poll(() => posted.find((p) => p.path === "/classifiers")?.body).toMatchObject({
      name: "asks_me",
      kind: "message",
      attribute: "needs_reply",
      channels: ["*"],
      backfill: "30d",
      spec: { worker: "jev", question: "Does this ask me something?", answer: "yes_no", value: { needs: true } },
    });
  });

  test("detail and thread review: filter, corrections, undo", async ({ page }) => {
    await seed(page);
    const posted = await mockClassifiers(page);
    await page.goto("classifiers/is_it_media");
    await expect(page.getByRole("button", { name: /partial\s*12/ })).toBeVisible({ timeout: 25_000 });
    await page.getByRole("link", { name: /Kitchen, Saturday evening/ }).click();
    await expect(page).toHaveURL(/threads\/501$/);
    await expect(page.getByText("Call now and save forty percent!")).toBeVisible();
    await expect(page.getByText("flagged · classifier")).toBeVisible();
    await page.screenshot({ path: "test-results/classifiers-thread.png", fullPage: true });

    await expect(page.getByText("Classifier's note: level partial · kind tv (90%)")).toBeVisible();
    await expect(page.getByText("you: Is noise: no")).toBeVisible();
    await page.getByRole("button", { name: "Not media" }).click(); // Speaker 2; the unflagged owner row has none
    await expect.poll(() => posted.find((p) => p.path === "/group-annotations")?.body).toMatchObject({
      thread_id: 501,
      attribute: "media_noise",
      participant_key: "S2",
      value: { is_noise: false },
    });
    await page.getByRole("button", { name: "Undo my correction" }).click();
    await expect.poll(() => posted.some((p) => p.path === "/group-annotations/72/retract")).toBe(true);
    await page.getByRole("button", { name: "All media" }).click();
    await expect
      .poll(() => posted.filter((p) => p.path === "/group-annotations").map((p) => p.body))
      .toContainEqual(expect.objectContaining({ value: { is_noise: true } }));
    const wholeThread = posted.filter((p) => p.path === "/group-annotations").at(-1)?.body as Record<string, unknown>;
    expect(wholeThread.participant_key).toBeUndefined();
  });

  test("detail: retract needs the name typed", async ({ page }) => {
    await seed(page);
    const posted = await mockClassifiers(page);
    await page.goto("classifiers/is_it_media");
    await page.getByRole("button", { name: "Retract marks" }).click();
    const dialog = page.getByRole("dialog");
    const confirm = dialog.getByRole("button", { name: "Retract marks" });
    await expect(confirm).toBeDisabled();
    await dialog.getByLabel(/Type is_it_media/).fill("is_it_media");
    await confirm.click();
    await expect.poll(() => posted.some((p) => p.path === "/classifiers/is_it_media/retract-marks")).toBe(true);
  });

  test("phone width: list and review fit", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seed(page);
    await mockClassifiers(page);
    await page.goto("classifiers/is_it_media/threads/501");
    await expect(page.getByText("Call now and save forty percent!")).toBeVisible({ timeout: 25_000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await page.screenshot({ path: "test-results/classifiers-phone.png", fullPage: true });
  });
});
