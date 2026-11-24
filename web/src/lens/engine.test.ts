import Ajv from "ajv";
import { describe, expect, test } from "vitest";
import { metaOf, syntheticProfile } from "./__fixtures__";
import { usageSample } from "./__fixtures__/samples";
import { describeResult, questionFor, startersForChannel } from "./describe";
import { analyzeChannel, analyzeResult, lensesForChannel, lensesForResult } from "./engine";
import { applyLensOverrides, applyRoleOverrides, candidateKey, normalizeOverrides } from "./overrides";
import { assembleChannelProfile, declaredProfile, profileChannel, profilePlan, profileResult } from "./profile";
import { inferRoles } from "./roles";
import { compileView, scanParams } from "./sql";
import type { ChannelMeta, Row, RunSql } from "./types";
import { VIEW_SPEC_SCHEMA } from "./types";

const ajv = new Ajv({ allErrors: true, strict: false });
const validate = ajv.compile(VIEW_SPEC_SCHEMA);

describe("view spec schema", () => {
  test("accepts the design's example and rejects a chart grammar", () => {
    const example = {
      v: 1,
      title: "Bedtime to wake, per night",
      why: "Every record has a start and an end; time of day is the pattern",
      lens: "interval_clock",
      source: {
        sql: "SELECT happened_at, ended_at, sleep_type FROM api.ch_health_oura_sleep WHERE happened_at >= :from AND happened_at < :to ORDER BY 1",
        row_cap: 2000,
      },
      encode: { start: "happened_at", end: "ended_at", color: "sleep_type" },
      provenance: { by: "engine", channel: "health.oura.sleep", sampled: false },
    };
    expect(validate(example)).toBe(true);
    expect(compileView(example as never, { from: "2026-01-01", to: "2026-02-01" }).unresolved).toEqual([]);
    expect(validate({ ...example, lens: "pie" })).toBe(false);
    expect(validate({ ...example, mark: "bar" })).toBe(false);
    expect(validate({ ...example, source: { sql: "SELECT 1", row_cap: 20000 } })).toBe(false);
    expect(validate({ ...example, encode: { start: 3 } })).toBe(false);
    expect(validate({ ...example, provenance: { by: "plugin", sampled: false } })).toBe(false);
  });
});

describe("overrides", () => {
  const meta = metaOf("health.weight");
  const profile = syntheticProfile("health.weight");

  test("normalizeOverrides keeps only what is well formed", () => {
    expect(normalizeOverrides(null)).toEqual({});
    expect(normalizeOverrides("x")).toEqual({});
    expect(
      normalizeOverrides({
        fields: { bmi: { role: "score", unit: "kg/m2", hidden: false, extra: 1 }, bad: { role: "nonsense" }, worse: 3 },
        pinned: ["histogram:bmi", 4, "", "histogram:bmi"],
        hidden: "scatter",
      }),
    ).toEqual({ fields: { bmi: { role: "score", unit: "kg/m2", hidden: false } }, pinned: ["histogram:bmi"] });
  });

  test("role, unit and hidden per field", () => {
    const roles = inferRoles(profile, meta);
    expect(roles.operation_count?.role).toBe("count");
    const next = applyRoleOverrides(roles, { fields: { operation_count: { role: "bookkeeping" }, weight: { unit: "lb" }, bmi: { hidden: true } } });
    expect(next.operation_count).toMatchObject({ role: "bookkeeping", confidence: 1, plumbing: true, overridden: true });
    expect(next.operation_count?.evidence[0]).toMatch(/set to bookkeeping by the owner/);
    expect(next.weight).toMatchObject({ role: "mass", unit: "lb", overridden: true });
    expect(next.bmi?.hidden).toBe(true);
    // the input is not changed
    expect(roles.operation_count?.role).toBe("count");
    expect(roles.bmi?.hidden).toBeUndefined();
  });

  test("a hidden or re-roled field leaves the charts", () => {
    const before = lensesForChannel(meta, profile);
    expect(before.some((c) => c.spec.provenance.fields?.includes("bmi"))).toBe(true);
    const after = lensesForChannel(meta, profile, { fields: { bmi: { hidden: true }, weight: { role: "identifier" } } });
    for (const c of after) {
      expect(c.spec.provenance.fields ?? []).not.toContain("bmi");
      if (c.spec.lens !== "table") expect(c.spec.provenance.fields ?? []).not.toContain("weight");
      expect(c.spec.source.sql).not.toMatch(/\bbmi\b/);
    }
  });

  test("a unit override reaches the view spec", () => {
    const cs = lensesForChannel(meta, profile, { fields: { weight: { unit: "lb" } } });
    const ts = cs.find((c) => c.spec.lens === "timeseries" && c.spec.provenance.fields?.[0] === "weight");
    expect((ts?.spec.options?.units as Record<string, string>).weight).toBe("lb");
  });

  test("pins come first in the owner's order, hides drop out, spine and table stay", () => {
    const base = lensesForChannel(meta, profile);
    const histogram = base.find((c) => c.spec.lens === "histogram");
    const key = histogram ? candidateKey(histogram) : "";
    expect(key).toMatch(/^histogram:/);
    const out = lensesForChannel(meta, profile, { pinned: [key, "calendar_heat"], hidden: ["timeseries", "table", "spine", "scatter:nope"] });
    expect(candidateKey(out[0] as never)).toBe(key);
    expect(out[1]?.spec.lens).toBe("calendar_heat");
    expect(out.some((c) => c.spec.lens === "timeseries")).toBe(false);
    expect(out.some((c) => c.spec.lens === "table")).toBe(true);
    expect(out.some((c) => c.spec.lens === "spine")).toBe(true);
    expect(out.length).toBe(base.length - base.filter((c) => c.spec.lens === "timeseries").length);
    expect(applyLensOverrides(base, null)).toBe(base);
  });
});

describe("channel profile", () => {
  const big: ChannelMeta = { ...metaOf("chat.discord"), rows: 303506 };
  const small = metaOf("health.weight");

  test("value statistics sample by id above 50,000 rows; counts stay exact", () => {
    const plan = profilePlan(big);
    expect(plan.sampled).toBe(true);
    expect(plan.modulo).toBe(16);
    const by = new Map(plan.statements.map((s) => [s.key, s.sql]));
    expect(by.get("text")).toContain("WHERE id % 16 = 0");
    expect(by.get("text_top")).toContain("WHERE id % 16 = 0");
    expect(by.get("time")).toContain("WHERE id % 16 = 0");
    for (const exact of ["envelope", "presence", "cadence", "threads"] as const) expect(by.get(exact)).not.toContain("id %");
    expect(by.get("sample")).toContain("WHERE id % 1517 = 0");
    const p = profilePlan(small);
    expect(p.sampled).toBe(false);
    for (const s of p.statements) expect(s.sql).not.toContain("id %");
    expect(p.statements.map((s) => s.key)).toEqual(["envelope", "presence", "numeric", "text", "text_top", "boolean", "time", "cadence", "threads", "sample"]);
  });

  test("statements quote awkward field names and never use bind parameters", () => {
    const plan = profilePlan(metaOf("email.message"));
    const all = plan.statements.map((s) => s.sql).join("\n");
    expect(all).toContain('count("to") AS p');
    expect(all).toContain('"references"');
    expect(all).not.toMatch(/\$\d/);
    for (const s of plan.statements) {
      expect(scanParams(s.sql)).toEqual([]);
      expect(s.sql).toMatch(/FROM (api\.ch_email_message|s )/);
    }
  });

  test("assembly from the statements' rows", async () => {
    const answers: Record<string, Row[]> = {
      envelope: [{ rows: 89, first_at: "2025-05-16T07:00:00Z", last_at: "2026-11-10T07:00:00Z", last_ingest_at: "2026-11-10T08:00:00Z", threads: 14, people: 0, authors: 0, with_ended_at: 0, with_geo: 4, with_geo_source: 0, with_images: 0, avg_content_chars: 51.2, max_content_chars: 80, content_empty: 0, max_version: 2, future_rows: 0, hours_used: 3, min_duration_s: null, avg_duration_s: null, max_duration_s: null }],
      cadence: [{ day: "2026-11-10", n: 1 }, { day: "2026-11-03", n: 2 }, { day: "2026-10-27", n: 1 }],
      threads: [{ thread_id: 1, n: 40, speakers: 0 }, { thread_id: 2, n: 49, speakers: 0 }],
      sample: [{ id: 5, happened_at: "2026-11-10T07:00:00Z", content: "80.1 kg", fields: { weight_kg: 80.1, deleted: false, scale_source: "wifi" } }],
    };
    const run: RunSql = async (statement) => {
      const plan = profilePlan(small);
      const s = plan.statements.find((x) => x.sql === statement);
      if (!s) throw new Error("unexpected statement");
      if (s.key === "presence") return { rows: [Object.fromEntries(small.fields.map((_f, i) => [`p${i}`, i === 0 ? 89 : 80]))], columns: [], truncated: false };
      if (s.key === "numeric") {
        const i = plan.numeric.indexOf("weight_kg");
        return { rows: [{ [`n${i}_min`]: 78.5, [`n${i}_max`]: 84.25, [`n${i}_avg`]: 81, [`n${i}_sd`]: 1.2, [`n${i}_dist`]: 60, [`n${i}_frac`]: 70, [`n${i}_n`]: 80 }], columns: [], truncated: false };
      }
      if (s.key === "text") {
        const i = plan.text.indexOf("scale_source");
        return { rows: [{ [`t${i}_dist`]: 2, [`t${i}_len`]: 5, [`t${i}_max`]: 9, [`t${i}_n`]: 80 }], columns: [], truncated: false };
      }
      if (s.key === "text_top") return { rows: [{ f: plan.text.indexOf("scale_source"), value: "wifi", n: 70 }, { f: plan.text.indexOf("scale_source"), value: "bluetooth", n: 10 }], columns: [], truncated: false };
      if (s.key === "boolean") return { rows: [{ b0_t: 3, b0_n: 80 }], columns: [], truncated: false };
      return { rows: answers[s.key] ?? [{}], columns: [], truncated: false };
    };
    const p = await profileChannel(small, run);
    expect(p.source).toBe("observed");
    expect(p.envelope.rows).toBe(89);
    expect(p.cache_key).toBe("health.weight|89|2026-11-10T08:00:00Z");
    const w = p.fields.find((f) => f.name === "weight_kg");
    expect(w?.numeric).toEqual({ min: 78.5, max: 84.25, avg: 81, stddev: 1.2, integral: false });
    expect(w?.fill).toBeCloseTo(80 / 89);
    const src = p.fields.find((f) => f.name === "scale_source");
    expect(src?.text?.top).toEqual([{ value: "wifi", n: 70 }, { value: "bluetooth", n: 10 }]);
    expect(p.fields.find((f) => f.name === "deleted")?.boolean?.true_share).toBeCloseTo(3 / 80);
    expect(p.sample[0]).toMatchObject({ id: 5, weight_kg: 80.1, scale_source: "wifi" });
    expect(p.threads.count).toBe(14);
    expect(p.cadence.active_days).toBe(3);
    // and the same through the two-step API
    const plan = profilePlan(small);
    expect(assembleChannelProfile(small, plan, {}).source).toBe("envelope");
  });

  test("JSON structure comes from the sample; empty arrays decide nothing", () => {
    const meta = metaOf("email.message");
    const plan = profilePlan(meta);
    const sample = Array.from({ length: 30 }, (_x, i) => ({
      id: i,
      happened_at: "2026-11-01T00:00:00Z",
      fields: { labels: ["INBOX", "UNREAD"], cc: [], to: [{ address: "a@example.invalid", name: "A" }], participants: ["a@example.invalid"] },
    }));
    const p = assembleChannelProfile(meta, plan, { sample });
    const shape = (name: string) => p.fields.find((f) => f.name === name)?.json?.shape;
    expect(shape("labels")).toBe("array_scalar");
    expect(shape("to")).toBe("array_object");
    expect(shape("cc")).toBe("mixed");
    const roles = inferRoles(p, meta);
    expect(roles.labels?.role).toBe("json.tags");
    expect(roles.to?.role).toBe("json.items");
    expect(roles.cc?.role).toBe("json.items");
    expect(p.fields.find((f) => f.name === "to")?.json?.keys).toEqual(["address", "name"]);
  });

  test("an empty channel costs no statements", async () => {
    let calls = 0;
    const run: RunSql = async () => {
      calls++;
      return { rows: [], columns: [], truncated: false };
    };
    const p = await profileChannel(metaOf("youtube.watch", true), run);
    expect(calls).toBe(0);
    expect(p).toEqual(declaredProfile(metaOf("youtube.watch", true)));
  });
});

describe("result profiles and their lenses", () => {
  const monthly: Row[] = Array.from({ length: 24 }, (_x, i) => ({
    bucket: new Date(Date.UTC(2025, i, 1)).toISOString().replace(".000Z", "Z"),
    n: 100 + ((i * 37) % 60),
    avg_len: `${52 + (i % 7)}.6333482743164500`,
  }));

  test("types come from the column list, or from the values when there is none", () => {
    const typed = profileResult(monthly, [
      { name: "bucket", type: "timestamptz" },
      { name: "n", type: "int8" },
      { name: "avg_len", type: "numeric" },
    ]);
    expect(typed.columns.map((c) => c.kind)).toEqual(["timestamp", "integer", "number"]);
    const inferred = profileResult(monthly);
    expect(inferred.columns.map((c) => c.kind)).toEqual(["timestamp", "integer", "number"]);
    expect(inferred.columns[2]?.numeric?.min).toBeCloseTo(52.6333);
    expect(inferred.unique).toContainEqual(["bucket"]);
    // An id with leading zeros stays text.
    expect(profileResult([{ zip: "00123" }, { zip: "04000" }]).columns[0]?.kind).toBe("text");
    // An empty result still knows its columns.
    expect(profileResult([], [{ name: "a", type: "text" }]).columns.map((c) => c.name)).toEqual(["a"]);
  });

  test("a series is drawn from the statement as it is", () => {
    const statement = "SELECT date_trunc('month', happened_at) AS bucket, count(*) AS n, avg(length(content)) AS avg_len FROM api.messages GROUP BY 1 ORDER BY 1";
    const cs = lensesForResult(profileResult(monthly), statement);
    expect(cs[0]?.spec.lens).toBe("timeseries");
    expect(cs[0]?.spec.source.sql).toBe(statement);
    expect(cs[0]?.spec.provenance.statement).toBe("as_is");
    expect(cs[0]?.spec.encode).toEqual({ x: "bucket", y: ["n", "avg_len"] });
    expect(cs.some((c) => c.spec.lens === "table")).toBe(true);
    for (const c of cs) expect(validate(c.spec), ajv.errorsText(validate.errors)).toBe(true);
  });

  test("a keyed aggregate becomes bars; raw rows are wrapped to be counted", () => {
    const perChannel = [
      { channel: "a.one", n: 1460 },
      { channel: "a.two", n: 730 },
      { channel: "b.three", n: 12 },
    ];
    const bars = lensesForResult(profileResult(perChannel), "SELECT channel, count(*) AS n FROM api.messages GROUP BY 1");
    expect(bars[0]?.spec.lens).toBe("top_values");
    expect(bars[0]?.spec.encode).toEqual({ label: "channel", value: "n" });
    expect(bars[0]?.spec.provenance.statement).toBe("as_is");

    const raw = Array.from({ length: 300 }, (_x, i) => ({
      id: i + 1,
      happened_at: new Date(Date.UTC(2026, 0, 1, i * 5)).toISOString(),
      channel: ["a.one", "a.two", "b.three"][i % 3],
      content_chars: 20 + ((i * 53) % 400),
    }));
    const statement = "SELECT id, happened_at, channel, length(content) AS content_chars FROM api.messages -- newest\n";
    const cs = lensesForResult(profileResult(raw), statement);
    const wrapped = cs.filter((c) => c.spec.provenance.statement === "wrapped");
    expect(wrapped.map((c) => c.spec.lens).sort()).toEqual(expect.arrayContaining(["histogram", "spine", "top_values", "weekday_hour"]));
    for (const c of wrapped) {
      // The closing parenthesis is on its own line, after the user's trailing comment.
      expect(c.spec.source.sql).toContain("FROM (\nSELECT id, happened_at, channel, length(content) AS content_chars FROM api.messages -- newest\n) AS q");
      expect(compileView(c.spec).unresolved).toEqual([]);
    }
    const hist = wrapped.find((c) => c.spec.lens === "histogram");
    expect(hist?.spec.source.params).toMatchObject({ lo: { type: "number", value: 20 }, bins: { type: "int", value: 30 } });
    // The id column is never binned or charted.
    expect(cs.some((c) => c.spec.lens !== "table" && Object.values(c.spec.encode).flat().includes("id") && c.spec.lens !== "reader")).toBe(false);
  });

  test("several statements, or none, are never wrapped; the table is still there", () => {
    const rows = Array.from({ length: 50 }, (_x, i) => ({ k: `v${i % 4}`, t: new Date(Date.UTC(2026, 0, 1, i)).toISOString() }));
    for (const statement of ["SELECT 1; SELECT 2", "", "   "]) {
      const a = analyzeResult(profileResult(rows), statement);
      expect(a.errors).toEqual([]);
      expect(a.candidates.every((c) => c.spec.provenance.statement !== "wrapped")).toBe(true);
      expect(a.candidates.some((c) => c.spec.lens === "table")).toBe(true);
    }
  });

  test("one row of numbers is a stat; coordinates are a map; start and end a clock", () => {
    expect(lensesForResult(profileResult([{ records: 186000, threads: 4100 }]), "SELECT 1")[0]?.spec.lens).toBe("stat");
    const places = [{ lat: 40.7, lon: -74, n: 12 }, { lat: 52.5, lon: 13.4, n: 3 }];
    expect(lensesForResult(profileResult(places), "SELECT 1")[0]?.spec.lens).toBe("pin_map");
    const nights = Array.from({ length: 10 }, (_x, i) => ({
      happened_at: new Date(Date.UTC(2026, 5, i, 22)).toISOString(),
      ended_at: new Date(Date.UTC(2026, 5, i + 1, 6)).toISOString(),
    }));
    expect(lensesForResult(profileResult(nights), "SELECT 1")[0]?.spec.lens).toBe("interval_clock");
  });
});

describe("narrator", () => {
  test("describeResult says what a series looks like", () => {
    const rows = Array.from({ length: 12 }, (_x, i) => ({ bucket: `2026-${String(i + 1).padStart(2, "0")}-01T00:00:00Z`, n: i === 7 ? 912 : 100 + i }));
    const lines = describeResult(profileResult(rows));
    expect(lines[0]).toBe("12 rows and 2 columns.");
    expect(lines[1]).toBe("bucket runs from 2026-01-01 to 2026-12-01, one row per bucket.");
    expect(lines[2]).toBe("n ranges from 100 to 912 (mean 172.6), highest on 2026-08-01.");
  });

  test("categories, a single row, truncation and the empty result", () => {
    const rows = [
      { status: "ok", source: "weather" },
      { status: "ok", source: "news" },
      { status: "ok", source: "tasks" },
      { status: "error", source: "mail" },
    ];
    expect(describeResult(profileResult(rows))).toContain("status has 2 values; most common: ok (75%), error (25%).");
    expect(describeResult(profileResult([{ records: 1234, threads: 12 }]))).toEqual(["One row: records = 1,234, threads = 12."]);
    expect(describeResult(profileResult(rows, [], true))[0]).toMatch(/cut off at the row cap/);
    expect(describeResult(profileResult([]))).toEqual(["The statement returned no rows."]);
    expect(describeResult(profileResult([], [{ name: "a", type: "text" }]))).toEqual(["The statement returned no rows (1 column: a)."]);
  });

  test("starters are runnable questions drawn from the ranked lenses", () => {
    const meta = metaOf("t3code.usage");
    const profile = syntheticProfile(meta.name);
    profile.sample = usageSample();
    const starters = startersForChannel(meta, profile);
    expect(starters.length).toBe(5);
    expect(starters[0]?.question).toBe("How fast does used percent climb before each reset?");
    expect(new Set(starters.map((s) => s.question)).size).toBe(5);
    for (const s of starters) {
      expect(s.question.endsWith("?")).toBe(true);
      expect(scanParams(s.statement)).toEqual([]);
      expect(s.statement).toContain("api.ch_t3code_usage");
      expect(validate(s.spec)).toBe(true);
    }
    // No rows: still five predicted starters.
    expect(startersForChannel(metaOf("youtube.watch", true)).length).toBeGreaterThanOrEqual(3);
    const a = analyzeChannel(meta, profile);
    for (const c of a.candidates) expect(questionFor(c, meta.name)).toMatch(/\?$/);
  });
});
