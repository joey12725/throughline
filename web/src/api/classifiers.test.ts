import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  backfillClassifier,
  checkDefinition,
  createGroupAnnotation,
  defineClassifier,
  draftClassifier,
  retractClassifierMarks,
  retractGroupAnnotation,
  runClassifier,
  setClassifierStatus,
  tryClassifier,
  type ClassifierDefinition,
} from "./classifiers";
import { ApiError } from "./errors";

interface Call {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}

const calls: Call[] = [];
let reply: { status: number; body: unknown } = { status: 200, body: {} };

beforeEach(() => {
  calls.length = 0;
  reply = { status: 200, body: {} };
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    calls.push({
      url,
      method: String(init.method),
      body: init.body ? JSON.parse(String(init.body)) : undefined,
      headers: init.headers as Record<string, string>,
    });
    return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { "Content-Type": "application/json" } });
  });
});
afterEach(() => vi.unstubAllGlobals());

const DEF: ClassifierDefinition = {
  name: "is_it_x",
  kind: "python",
  attribute: "media_noise",
  channels: ["*"],
  spec: { settle_minutes: 30 },
  code: "def classify(t): ...",
};

const last = (): Call => calls[calls.length - 1] as Call;

describe("classifier client", () => {
  it("checks without an idempotency key (it is read-only)", async () => {
    reply.body = { ok: true, errors: [], warnings: [] };
    await expect(checkDefinition(DEF)).resolves.toMatchObject({ ok: true });
    expect(last()).toMatchObject({ url: "/v1/classifiers/check", method: "POST", body: { definition: DEF } });
    expect(last().headers["Idempotency-Key"]).toBeUndefined();
  });

  it("defines, tries and drafts with an idempotency key", async () => {
    await defineClassifier(DEF);
    expect(last()).toMatchObject({ url: "/v1/classifiers", method: "POST", body: DEF });
    const k1 = last().headers["Idempotency-Key"];
    expect(k1).toBeTruthy();

    await tryClassifier({ definition: DEF }, { thread_ids: [1, 2] });
    expect(last()).toMatchObject({ url: "/v1/classifiers/try", body: { definition: DEF, sample: { thread_ids: [1, 2] } } });
    expect(last().headers["Idempotency-Key"]).not.toBe(k1);

    await draftClassifier({ goal: "flag tv", kind: "python" });
    expect(last()).toMatchObject({ url: "/v1/classifiers/draft", body: { goal: "flag tv", kind: "python" } });
  });

  it("drives the lifecycle routes", async () => {
    await setClassifierStatus("a b", "active");
    expect(last()).toMatchObject({ url: "/v1/classifiers/a%20b/status", body: { status: "active" } });
    await runClassifier("x", 10);
    expect(last()).toMatchObject({ url: "/v1/classifiers/x/run", body: { max_items: 10 } });
    await runClassifier("x");
    expect(last().body).toEqual({});
    await backfillClassifier("x", "30d");
    expect(last()).toMatchObject({ url: "/v1/classifiers/x/backfill", body: { since: "30d" } });
    await retractClassifierMarks("x");
    expect(last()).toMatchObject({ url: "/v1/classifiers/x/retract-marks", body: {} });
  });

  it("posts and retracts owner corrections", async () => {
    await createGroupAnnotation({ thread_id: 7, attribute: "media_noise", participant_key: "S2", value: { is_noise: false } });
    expect(last()).toMatchObject({
      url: "/v1/group-annotations",
      method: "POST",
      body: { thread_id: 7, attribute: "media_noise", participant_key: "S2", value: { is_noise: false } },
    });
    await retractGroupAnnotation(41);
    expect(last()).toMatchObject({ url: "/v1/group-annotations/41/retract", method: "POST" });
  });

  it("surfaces a 422 detail and a 503 from draft", async () => {
    reply = { status: 422, body: { detail: "attribute media_nois does not exist" } };
    await expect(defineClassifier(DEF)).rejects.toMatchObject({ status: 422, message: "attribute media_nois does not exist" });
    reply = { status: 503, body: { detail: "model unreachable" } };
    const err = await draftClassifier({ goal: "x" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(503);
  });
});
