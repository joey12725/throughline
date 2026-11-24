import Ajv from "ajv";
import { describe, expect, test } from "vitest";
import { ZOO_NAMES, allMetas, metaOf, syntheticProfile } from "./__fixtures__";
import { picksSample, sleepSample, usageSample } from "./__fixtures__/samples";
import { analyzeChannel, lensesForChannel } from "./engine";
import { LENSES, LENS_CATALOG, LENS_LIST } from "./lenses/index";
import { channelFamilies } from "./roles";
import { EXTENDED, NARROW, compileView, scanParams } from "./sql";
import type { ChannelMeta, ChannelProfile, LensCandidate, LensId } from "./types";
import { LENS_IDS, VIEW_SPEC_SCHEMA } from "./types";

const ajv = new Ajv({ allErrors: true, strict: false });
const validate = ajv.compile(VIEW_SPEC_SCHEMA);

const families = channelFamilies(allMetas());
function siblingsOf(name: string): ChannelMeta[] {
  const fam = families.find((f) => f.channels.includes(name));
  return fam ? fam.channels.map((n) => metaOf(n)) : [];
}

function candidates(name: string, mutate?: (p: ChannelProfile) => void): LensCandidate[] {
  const profile = syntheticProfile(name);
  mutate?.(profile);
  return lensesForChannel(metaOf(name), profile, null, { siblings: siblingsOf(name) });
}

/** Lens ids in rank order without the two that are always there. */
function charts(cs: LensCandidate[]): LensId[] {
  return cs.map((c) => c.spec.lens).filter((l) => l !== "spine" && l !== "table");
}

describe("catalogue", () => {
  test("one module per lens of the design's table", () => {
    expect(Object.keys(LENSES).sort()).toEqual([...LENS_IDS].sort());
    expect(LENS_LIST.length).toBe(27);
    for (const id of LENS_IDS) {
      expect(LENSES[id].id).toBe(id);
      expect(LENSES[id].needs.length).toBeGreaterThan(0);
      expect(Object.keys(LENSES[id].channels).length).toBeGreaterThan(0);
    }
    const groups = (g: string) => LENS_CATALOG.filter((l) => l.group === g).map((l) => l.id).sort();
    expect(groups("time")).toEqual(["calendar_heat", "gantt", "interval_clock", "sawtooth", "small_multiples", "spine", "timeseries", "weekday_hour"]);
    expect(groups("distribution")).toEqual(["composition", "histogram", "scatter", "strip", "top_values"]);
    expect(groups("structure")).toEqual(["json_tree", "reply_latency", "speaker_share", "status_matrix", "tags", "thread_tapestry"]);
    expect(groups("space")).toEqual(["dwell_rank", "pin_map", "track_map"]);
    expect(groups("relation")).toEqual(["ego_graph", "link_matrix"]);
    expect(groups("reading")).toEqual(["reader", "stat", "table"]);
  });
});

describe("worked examples", () => {
  test("a channel of sleep periods: the interval clock, then what the night is made of", () => {
    const cs = candidates("health.oura.sleep", (p) => {
      p.sample = sleepSample(60);
      Object.assign(p.envelope, { min_duration_s: 1500, avg_duration_s: 27000, max_duration_s: 36000 });
    });
    const order = charts(cs);
    expect(order[0]).toBe("interval_clock");
    expect(order[1]).toBe("composition");
    const top = order.slice(0, 8);
    expect(top).toContain("timeseries");
    expect(top).toContain("scatter");
    const hero = cs.find((c) => c.spec.lens === "interval_clock");
    expect(hero?.spec.encode).toMatchObject({ start: "happened_at", end: "ended_at", color: "sleep_type" });
    expect(hero?.spec.source.sql).toBe(
      "SELECT id, happened_at, ended_at, (sleep_type)::text AS sleep_type FROM api.ch_health_oura_sleep WHERE happened_at >= :from AND happened_at < :to AND ended_at IS NOT NULL ORDER BY happened_at",
    );
    const comp = cs.find((c) => c.spec.lens === "composition");
    expect((comp?.spec.encode.parts as string[]).sort()).toEqual(["awake_s", "deep_sleep_s", "light_sleep_s", "rem_sleep_s"]);
    expect(comp?.spec.encode.whole).toBe("time_in_bed_s");
  });

  test("usage windows: a sawtooth per window with the reset marked", () => {
    const cs = candidates("t3code.usage", (p) => {
      p.sample = usageSample();
    });
    const order = charts(cs);
    expect(order[0]).toBe("sawtooth");
    const saw = cs.find((c) => c.spec.lens === "sawtooth");
    expect(saw?.spec.encode).toMatchObject({ y: "used_percent", series: "series", marker: "marker" });
    expect(saw?.spec.source.sql).toContain("coalesce(window_id, '(none)') AS series");
    expect(saw?.spec.source.sql).toContain("resets_at AS marker");
    expect(saw?.why).toMatch(/climbs and falls back within each window id/);
    // used and left mirror each other: only one of them is drawn as a sawtooth.
    expect(cs.filter((c) => c.spec.lens === "sawtooth").map((c) => c.spec.encode.y)).not.toContain("left_percent");
    const top = order.slice(0, 8);
    expect(top).toContain("top_values");
    expect(top).toContain("scatter");
  });

  test("usage windows are predicted from names when nothing is sampled", () => {
    const order = charts(candidates("t3code.usage"));
    expect(order[0]).toBe("sawtooth");
  });

  test("daily snapshots and their twelve siblings: the status matrix first", () => {
    for (const name of ["morning.weather", "morning.email", "morning.pipeline"]) {
      const cs = candidates(name);
      const order = charts(cs);
      expect(order[0], name).toBe("status_matrix");
      const top = order.slice(0, 8);
      expect(top.some((l) => l === "timeseries" || l === "calendar_heat")).toBe(true);
      expect(top).toContain("json_tree");
      const itemCount = cs.find((c) => (c.spec.lens === "timeseries" || c.spec.lens === "calendar_heat") && c.spec.provenance.fields?.includes("item_count"));
      expect(itemCount, `${name} charts item_count`).toBeDefined();
      const family = cs.find((c) => c.spec.lens === "status_matrix" && c.spec.source.params?.channels);
      expect((family?.spec.source.params?.channels?.value as string[]).length).toBe(13);
      expect(cs.some((c) => c.spec.lens === "reader")).toBe(true);
    }
  });

  test("a large chat channel: the thread tapestry, then rhythm, speakers and replies; aggregates only", () => {
    const cs = candidates("chat.discord", (p) => {
      p.envelope.hours_used = 24;
    });
    const order = charts(cs);
    expect(order[0]).toBe("thread_tapestry");
    const top = order.slice(0, 7);
    expect(top).toContain("weekday_hour");
    expect(top).toContain("speaker_share");
    expect(top).toContain("reply_latency");
    expect(order).toContain("calendar_heat");
    // Only the reader and the table touch raw rows, 200 at a time.
    for (const c of cs) {
      const raw = !/\b(count|sum|avg|min|max)\(/.test(c.spec.source.sql);
      if (raw) {
        expect(["reader", "table", "json_tree"]).toContain(c.spec.lens);
        expect(c.spec.source.row_cap).toBeLessThanOrEqual(200);
      }
    }
  });

  test("scored feed items: the probability strip coloured by verdict", () => {
    const cs = candidates("youtube.picks", (p) => {
      p.sample = picksSample();
    });
    const order = charts(cs);
    expect(order[0]).toBe("strip");
    expect(cs.find((c) => c.spec.lens === "strip")?.spec.encode).toEqual({ x: "watch_probability", color: "verdict" });
    const top = order.slice(0, 8);
    expect(top).toContain("scatter");
    expect(top).toContain("top_values");
    expect(top).toContain("reader");
    const sc = cs.find((c) => c.spec.lens === "scatter");
    expect([sc?.spec.encode.x, sc?.spec.encode.y].sort()).toEqual(["junk_food_probability", "watch_probability"]);
    expect(cs.find((c) => c.spec.lens === "reader")?.spec.options?.variant).toBe("feed");
  });

  test("other heroes follow the archetype", () => {
    expect(charts(candidates("location.timeline"))[0]).toBe("track_map");
    expect(charts(candidates("location.places"))[0]).toBe("pin_map");
    expect(charts(candidates("calendar.events"))[0]).toBe("interval_clock");
    expect(charts(candidates("recordings.wave"))[0]).toBe("interval_clock");
    expect(charts(candidates("email.message"))[0]).toBe("reader");
    expect(charts(candidates("gbrain.pages"))[0]).toBe("reader");
    expect(charts(candidates("t3code.messages"))[0]).toBe("thread_tapestry");
    expect(candidates("t3code.subscriptions").find((c) => c.spec.lens !== "spine")?.spec.lens).toBe("table");
    expect(candidates("email.message").find((c) => c.spec.lens === "reader")?.spec.options?.variant).toBe("document");
    expect(candidates("chat.sms").find((c) => c.spec.lens === "reader")?.spec.options?.variant).toBe("bubbles");
    expect(candidates("transcripts.wave").find((c) => c.spec.lens === "reader")?.spec.options?.variant).toBe("transcript");
    expect(candidates("transcripts.wave").find((c) => c.spec.lens === "reader")?.spec.encode).toMatchObject({ start_offset: "start_s", end_offset: "end_s", speaker: "speaker_label" });
  });

  test("the top of the list is not six of one kind", () => {
    for (const name of ["health.oura.daily", "health.weight", "health.oura.heartrate", "t3code.usage", "chat.discord"]) {
      const top = charts(candidates(name)).slice(0, 6);
      const counts = new Map<string, number>();
      for (const l of top) counts.set(l, (counts.get(l) ?? 0) + 1);
      expect(Math.max(...counts.values()), `${name}: ${top.join(", ")}`).toBeLessThanOrEqual(4);
      expect(counts.size, `${name}: ${top.join(", ")}`).toBeGreaterThanOrEqual(2);
    }
  });
});

describe("every candidate of every fixture channel", () => {
  const cases = ZOO_NAMES.flatMap((name) => [
    { name, mode: "observed", caps: EXTENDED },
    { name, mode: "observed, narrow builtins", caps: NARROW },
    { name, mode: "declared", caps: EXTENDED },
  ]);

  test.each(cases)("$name ($mode)", ({ name, mode, caps }) => {
    const declared = mode === "declared";
    const meta = metaOf(name, declared);
    const profile = declared ? null : syntheticProfile(name);
    if (profile && name === "health.oura.sleep") profile.sample = sleepSample(40);
    if (profile && name === "t3code.usage") profile.sample = usageSample();
    const a = analyzeChannel(meta, profile, null, { caps, siblings: siblingsOf(name) });
    expect(a.errors).toEqual([]);
    const lenses = a.candidates.map((c) => c.spec.lens);
    expect(lenses).toContain("spine");
    expect(lenses).toContain("table");
    const titles = new Set<string>();
    for (const c of a.candidates) {
      const spec = c.spec;
      expect(validate(spec), `${spec.lens}: ${ajv.errorsText(validate.errors)}`).toBe(true);
      expect(c.score).toBeGreaterThanOrEqual(0);
      expect(c.score).toBeLessThanOrEqual(1);
      // Short English, one sentence, no trailing period, no dashes used as punctuation.
      expect(spec.title.length).toBeGreaterThan(3);
      expect(spec.title.length).toBeLessThanOrEqual(100);
      expect(c.why).toBe(spec.why);
      expect(spec.why.length).toBeGreaterThan(12);
      expect(spec.why).not.toMatch(/undefined|NaN|\[object|—|–/);
      expect(spec.title).not.toMatch(/undefined|NaN|\[object|—|–/);
      expect(/^[A-Z0-9]/.test(spec.title), spec.title).toBe(true);
      titles.add(`${spec.lens}|${spec.title}`);
      // One read-only SELECT over schema api, and nothing else.
      const sql = spec.source.sql;
      expect(sql).toMatch(/^(SELECT|WITH) /);
      expect(sql).not.toMatch(/;/);
      const relations = sql.replace(/extract\(\w+ FROM /g, "extract(").replace(/DISTINCT FROM/g, "");
      for (const m of relations.matchAll(/\b(?:FROM|JOIN)\s+([a-zA-Z_"][\w."]*)/g)) {
        const rel = m[1] as string;
        if (["top", "o", "g", "p", "t", "s", "h", "b", "generate_series", "LATERAL"].includes(rel)) continue;
        expect(rel.startsWith("api."), `${spec.lens} reads ${rel}`).toBe(true);
      }
      // Every parameter is a global or a declared local, and compiles to nothing unresolved.
      const locals = Object.keys(spec.source.params ?? {});
      for (const ref of scanParams(sql)) {
        expect(["from", "to", "tz", "person", "channel", "grain", ...locals]).toContain(ref.name);
      }
      const compiled = compileView(spec, { from: "2026-01-01", to: "2026-11-20", tz: "America/New_York" });
      expect(compiled.unresolved).toEqual([]);
      expect(scanParams(compiled.sql)).toEqual([]);
      expect(compileView(spec).unresolved).toEqual([]);
      // Encoded columns exist in the statement's select list.
      for (const col of Object.values(spec.encode).flat()) {
        expect(sql.includes(col), `${spec.lens} encodes ${col}, missing from: ${sql}`).toBe(true);
      }
      // Bookkeeping never reaches a chart.
      if (spec.lens !== "table" && spec.lens !== "reader") expect(spec.provenance.fields ?? []).not.toContain("mapping_revision");
      if (caps === NARROW) expect(sql).not.toMatch(/\b(width_bucket|lag|jsonb_array_elements_text|jsonb_typeof|floor|date_bin|percentile_cont)\(/);
    }
    expect(titles.size, "titles are distinct per lens").toBe(a.candidates.length);
  });
});

describe("time grain keeps results near 1,500 buckets", () => {
  test("spine of a nine-year channel uses weeks; small multiples of 13 channels a coarser grain", () => {
    const cs = candidates("chat.discord");
    const spine = cs.find((c) => c.spec.lens === "spine");
    expect(spine && compileView(spine.spec).grain).toBe("week");
    const fam = candidates("morning.weather").find((c) => c.spec.lens === "small_multiples" && c.spec.source.params?.channels);
    expect(fam).toBeDefined();
    const grain = fam && compileView(fam.spec).grain;
    expect(["day", "week"]).toContain(grain);
    const tapestry = cs.find((c) => c.spec.lens === "thread_tapestry");
    expect(tapestry && compileView(tapestry.spec).grain).toBe("month");
  });
});

describe("spine coverage vs count (by cadence)", () => {
  function specOf(name: string, lens: LensId, mutate?: (p: ChannelProfile) => void) {
    return candidates(name, mutate).find((c) => c.spec.lens === lens)?.spec;
  }
  const observed = (cadence: Partial<ChannelProfile["cadence"]>) => (p: ChannelProfile) => {
    p.source = "observed";
    p.envelope.rows = Math.max(1, p.envelope.rows);
    Object.assign(p.cadence, cadence);
  };

  test("a one-record-per-day channel draws a coverage strip, not a count band", () => {
    const spec = specOf(
      "health.oura.daily",
      "spine",
      observed({ class: "daily", span_days: 400, per_active_day_median: 1 }),
    );
    expect(spec?.options?.mode).toBe("coverage");
  });

  test("a high-volume/bursty channel keeps the count spine", () => {
    const spec = specOf(
      "chat.discord",
      "spine",
      observed({ class: "bursty", span_days: 400, per_active_day_median: 40 }),
    );
    expect(spec?.options?.mode).toBeUndefined();
  });

  test("a channel with no rows (predicted) keeps the count spine", () => {
    const spec = specOf("health.oura.daily", "spine", (p) => {
      p.source = "declared";
      p.envelope.rows = 0;
      Object.assign(p.cadence, { class: "daily", per_active_day_median: 1 });
    });
    expect(spec?.options?.mode).toBeUndefined();
  });
});

describe("timeseries rolling mean (noisy daily measures)", () => {
  const observed = (cadence: Partial<ChannelProfile["cadence"]>) => (p: ChannelProfile) => {
    p.source = "observed";
    p.envelope.rows = Math.max(1, p.envelope.rows);
    Object.assign(p.cadence, cadence);
  };
  function timeseriesSpecs(name: string, mutate?: (p: ChannelProfile) => void) {
    return candidates(name, mutate)
      .filter((c) => c.spec.lens === "timeseries")
      .map((c) => c.spec);
  }

  test("a daily measurement gets a rolling-mean overlay", () => {
    const specs = timeseriesSpecs(
      "health.oura.daily",
      observed({ class: "daily", span_days: 400, per_active_day_median: 1 }),
    );
    expect(specs.length).toBeGreaterThan(0);
    expect(specs.some((s) => s.options?.rolling === 7)).toBe(true);
  });

  test("a bursty channel's time series is not smoothed", () => {
    const specs = timeseriesSpecs(
      "health.oura.daily",
      observed({ class: "bursty", span_days: 400, per_active_day_median: 50 }),
    );
    expect(specs.every((s) => s.options?.rolling === undefined)).toBe(true);
  });
});
