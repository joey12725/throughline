import { describe, expect, it } from "vitest";
import type { ValueSchema } from "@/api/classifiers";
import {
  attributeProblems,
  checkMessages,
  correctionLabels,
  defaultValue,
  definitionToForm,
  emptyField,
  emptyForm,
  fieldInputKind,
  fieldsToSchema,
  formatOffset,
  formatUsd,
  formProblems,
  formToDefinition,
  isFlagged,
  lineOf,
  orderVerdicts,
  parseFieldInput,
  parseIds,
  schemaToFields,
  summarizeDetail,
  summarizeValue,
  toggleField,
  validateValue,
} from "./model";

const NOISE: ValueSchema = { is_noise: { type: "boolean", required: true } };
const RICH: ValueSchema = {
  is_noise: { type: "boolean", required: true },
  kind: { type: "enum", values: ["tv", "music"] },
  level: { type: "integer", min: 1, max: 5, required: true },
  note: { type: "string" },
};

describe("value schema model", () => {
  it("picks a control per field", () => {
    expect(fieldInputKind({ type: "boolean" })).toBe("boolean");
    expect(fieldInputKind({ type: "string", values: ["a"] })).toBe("enum");
    expect(fieldInputKind({ type: "int" })).toBe("integer");
    expect(fieldInputKind({ type: "float" })).toBe("number");
    expect(fieldInputKind({ type: "string" })).toBe("text");
  });

  it("builds defaults that validate", () => {
    const v = defaultValue(RICH);
    expect(v).toEqual({ is_noise: true, level: 1 });
    expect(validateValue(RICH, v)).toEqual({});
  });

  it("reports required, type, choice and range problems", () => {
    expect(validateValue(RICH, {})).toMatchObject({ is_noise: "Required", level: "Required" });
    expect(validateValue(RICH, { is_noise: "yes", level: 9, kind: "radio" })).toEqual({
      is_noise: "Must be true or false",
      level: "At most 5",
      kind: "One of tv, music",
    });
    expect(validateValue(RICH, { is_noise: true, level: 2.5 }).level).toBe("Must be a whole number");
    expect(validateValue(NOISE, { is_noise: true, extra: 1 }).extra).toMatch(/Not in/);
  });

  it("parses typed input", () => {
    expect(parseFieldInput({ type: "integer" }, "3")).toBe(3);
    expect(parseFieldInput({ type: "integer" }, "")).toBeUndefined();
    expect(parseFieldInput({ type: "boolean" }, "false")).toBe(false);
    expect(parseFieldInput({ type: "integer", values: [1, 2] }, "2")).toBe(2);
  });

  it("summarizes and flags values", () => {
    expect(summarizeValue({ is_noise: true })).toBe("Is noise: yes");
    expect(summarizeValue(null)).toBe("cleared");
    expect(isFlagged({ is_noise: true })).toBe(true);
    expect(isFlagged({ is_noise: false })).toBe(false);
    expect(isFlagged({ topic: "x" })).toBe(true);
    expect(isFlagged(null)).toBe(false);
    // the attribute's flag field decides, whatever other booleans say
    expect(isFlagged({ is_noise: false, loud: true }, "is_noise")).toBe(false);
    expect(summarizeValue({ kind: "tv", is_noise: true, classifier: "x" }, "is_noise")).toBe("Is noise: yes · Kind: tv");
    expect(summarizeDetail({ kind: "audiobook", level: "partial", media_speakers: ["A", "B"] })).toBe("level partial · kind audiobook · media speakers A, B");
    expect(summarizeDetail(null)).toBe("");
  });

  it("finds the quick-toggle field only when unambiguous", () => {
    expect(toggleField(NOISE)).toBe("is_noise");
    expect(toggleField(RICH)).toBeNull();
    expect(toggleField({ a: { type: "boolean" }, note: { type: "string" } })).toBe("a");
    expect(toggleField({})).toBeNull();
    // a declared flag field wins over counting booleans
    const LIVE: ValueSchema = { is_noise: { type: "boolean", required: true }, kind: { type: "enum", values: ["tv"] }, other: { type: "boolean" } };
    expect(toggleField(LIVE)).toBeNull();
    expect(toggleField(LIVE, "is_noise")).toBe("is_noise");
  });

  it("labels corrections", () => {
    expect(correctionLabels("media_noise").no).toBe("Not media");
    expect(correctionLabels("off_topic")).toEqual({ yes: "Off topic", no: "Not off topic", allYes: "All off topic", allNo: "No off topic" });
  });
});

describe("attribute field drafts", () => {
  it("round-trips schema <-> fields", () => {
    const fields = schemaToFields(RICH);
    expect(fieldsToSchema(fields)).toEqual(RICH);
  });

  it("validates a new attribute", () => {
    expect(attributeProblems({ name: "Bad Name", fields: [] })).toMatchObject({ name: expect.any(String), fields: expect.any(String) });
    const dup = [{ ...emptyField(), name: "a" }, { ...emptyField(), name: "a" }];
    expect(attributeProblems({ name: "ok_name", fields: dup })["field.1"]).toBe("duplicate name");
    expect(attributeProblems({ name: "ok_name", fields: [{ ...emptyField(), name: "n", type: "integer", values: "1, x" }] })["field.0"]).toMatch(/numbers/);
  });
});

describe("form <-> definition", () => {
  it("builds a yes/no message definition", () => {
    const f = { ...emptyForm("message"), name: "is_spam", attribute: "spam", question: " Is this spam? ", value: { is_spam: true }, threshold: 0.8123, context_messages: "2" };
    const def = formToDefinition(f);
    expect(def).toEqual({
      name: "is_spam",
      kind: "message",
      attribute: "spam",
      channels: ["*"],
      spec: { worker: "jev", question: "Is this spam?", answer: "yes_no", threshold: 0.81, value: { is_spam: true }, context_messages: 2 },
    });
  });

  it("builds a choice definition and drops empty option values", () => {
    const f = {
      ...emptyForm("message"),
      name: "tone",
      attribute: "tone",
      question: "Tone?",
      answer: "choice" as const,
      options: ["warm", "cold", " "],
      values: { warm: { tone: "warm" }, cold: {} },
      channels: ["a.b"],
      backfill: "days" as const,
      backfillDays: "30",
    };
    const def = formToDefinition(f);
    expect(def.spec).toMatchObject({ options: ["warm", "cold"], values: { warm: { tone: "warm" } } });
    expect(def.spec.threshold).toBeUndefined();
    expect(def.channels).toEqual(["a.b"]);
    expect(def.backfill).toBe("30d");
  });

  it("builds a python definition with a new attribute", () => {
    const f = {
      ...emptyForm("python"),
      name: "is_it_x",
      code: "def classify(t):\n  pass\n",
      attrMode: "new" as const,
      newAttr: { name: "x_noise", description: "d", recall_effect: "exclude" as const, flag: "is_noise", fields: [{ ...emptyField(), name: "is_noise" }, { ...emptyField(), name: "kind", type: "string" as const, required: false, values: "tv, music" }] },
      settle_minutes: "30",
      max_cost_per_run: "0.5",
      monthly_cap_usd: "2",
    };
    const def = formToDefinition(f);
    expect(def.code).toContain("classify");
    expect(def.attribute).toEqual({
      name: "x_noise",
      description: "d",
      value_schema: { is_noise: { type: "boolean", required: true }, kind: { type: "enum", values: ["tv", "music"] } },
      recall_effect: "exclude",
      flag: "is_noise",
    });
    expect(def.spec).toEqual({ settle_minutes: 30, max_cost_per_run: 0.5 });
    expect(def.monthly_cap_usd).toBe(2);
    expect(def.backfill).toBeUndefined();
  });

  it("loads a definition back, keeping unknown spec keys", () => {
    const def = {
      name: "tone",
      kind: "message" as const,
      attribute: "tone",
      channels: ["*"],
      spec: { worker: "jev", question: "Q", answer: "choice", options: ["a", "b"], values: { a: { t: 1 } }, future_knob: 3 },
      backfill: "2026-01-02T00:00:00Z",
    };
    const f = definitionToForm(def);
    expect(f.channels).toEqual([]);
    expect(f.answer).toBe("choice");
    expect(f.backfill).toBe("since");
    expect(f.backfillSince).toBe("2026-01-02");
    expect(f.extraSpec).toEqual({ future_knob: 3 });
    expect(definitionToForm({ backfill: "7d" })).toMatchObject({ backfill: "days", backfillDays: "7" });
    expect(definitionToForm({ attribute: { name: "a", description: "", value_schema: { on: { type: "boolean" } }, recall_effect: "none", flag: "on" } }).newAttr.flag).toBe("on");
    expect(formToDefinition(f).spec.future_knob).toBe(3);
  });

  it("fills a partial draft over the current form", () => {
    const base = { ...emptyForm("message"), name: "keep_me", question: "old" };
    const f = definitionToForm({ spec: { question: "new" } }, base);
    expect(f.name).toBe("keep_me");
    expect(f.question).toBe("new");
  });

  it("flags client-side problems", () => {
    const f = emptyForm("message");
    expect(formProblems(f, NOISE)).toMatchObject({ name: expect.any(String), attribute: expect.any(String), question: "Required", value: expect.any(String) });
    const ok = { ...f, name: "ok_one", attribute: "media_noise", question: "Q?", value: { is_noise: true } };
    expect(formProblems(ok, NOISE)).toEqual({});
    expect(formProblems({ ...ok, context_messages: "25" }, NOISE).context_messages).toBe("0 to 20");
    expect(formProblems({ ...emptyForm("python"), name: "p_one", attribute: "a" }, NOISE).code).toBeDefined();
  });
});

describe("check diagnostics", () => {
  it("finds line numbers in several message styles", () => {
    expect(lineOf("line 12: import os is not allowed")).toBe(12);
    expect(lineOf("SyntaxError at line 3")).toBe(3);
    expect(lineOf("L7 uses open()")).toBe(7);
    expect(lineOf("4:2: undefined name")).toBe(4);
    expect(lineOf("classify must be defined")).toBeNull();
  });

  it("merges errors and warnings", () => {
    expect(checkMessages({ errors: ["line 2 bad"], warnings: ["slow"] })).toEqual([
      { line: 2, message: "line 2 bad", severity: "error" },
      { line: null, message: "slow", severity: "warning" },
    ]);
  });
});

describe("formatting", () => {
  it("formats money", () => {
    expect(formatUsd(0)).toBe("$0");
    expect(formatUsd(0.0042)).toBe("$0.0042");
    expect(formatUsd(0.123)).toBe("$0.123");
    expect(formatUsd(12.5)).toBe("$12.50");
    expect(formatUsd(null)).toBe("—");
  });

  it("formats offsets", () => {
    expect(formatOffset(65)).toBe("1:05");
    expect(formatOffset(3725)).toBe("1:02:05");
    expect(formatOffset(null)).toBe("");
  });

  it("orders verdicts and parses ids", () => {
    expect(orderVerdicts({ full: 1, other: 2, none: 3, partial: 4 }).map(([k]) => k)).toEqual(["none", "partial", "full", "other"]);
    expect(parseIds("12, 34\nabc-1")).toEqual([12, 34, "abc-1"]);
  });
});
