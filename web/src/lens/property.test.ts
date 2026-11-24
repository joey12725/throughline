// Property tests: whatever the profile, lens selection does not throw, always offers the
// table (and the spine for a channel), and every statement it emits compiles.

import { describe, expect, test } from "vitest";
import { ZOO } from "./__fixtures__";
import { describeResult, startersForChannel } from "./describe";
import { analyzeChannel, analyzeResult } from "./engine";
import { declaredProfile, profileResult } from "./profile";
import { EXTENDED, NARROW, compileView, scanParams } from "./sql";
import type { ChannelMeta, ChannelProfile, FieldDecl, FieldProfile, Row } from "./types";
import { ROLE_IDS } from "./types";

function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const TYPES = ["text", "integer", "number", "boolean", "timestamp", "json", "bytea"];
const WORDS = [
  "id", "start", "end", "s", "pct", "percent", "score", "count", "used", "left", "lat", "lon", "latitude", "longitude",
  "url", "title", "status", "role", "day", "date", "min", "max", "total", "name", "email", "window", "revision", "to",
  "from", "select", "Weird Name", "ünï", "a'b", 'q"r', "x;y", "duration", "probability", "tags", "items", "user", "order",
  "$$", "--", "/*", ":from", "0", "_", "level", "resets", "at", "m", "kg", "contrib",
];
const pool = ZOO.flatMap((c) => c.fields);

function pick<T>(r: () => number, xs: readonly T[]): T {
  return xs[Math.floor(r() * xs.length)] as T;
}

function randomName(r: () => number): string {
  if (r() < 0.5) return pick(r, pool).name;
  const n = 1 + Math.floor(r() * 3);
  return Array.from({ length: n }, () => pick(r, WORDS)).join(r() < 0.8 ? "_" : " ");
}

function randomValue(r: () => number, type: string, i: number): unknown {
  if (r() < 0.1) return null;
  switch (type) {
    case "integer": return Math.floor(r() * 200) - (r() < 0.1 ? 100 : 0);
    case "number": return r() < 0.05 ? Number.NaN : (r() - 0.2) * (r() < 0.5 ? 1 : 1000);
    case "boolean": return r() < 0.5;
    case "timestamp": return new Date(Date.UTC(2020 + Math.floor(r() * 8), Math.floor(r() * 12), 1 + Math.floor(r() * 28), Math.floor(r() * 24))).toISOString();
    case "json": return r() < 0.3 ? ["a", "b"] : r() < 0.5 ? [{ k: i }] : r() < 0.7 ? { k: i, nested: { x: 1 } } : r() < 0.85 ? 3 : "s";
    default: return r() < 0.2 ? `https://example.invalid/${i}` : r() < 0.3 ? "2026-01-02" : pick(r, ["ok", "error", "a'b", "", "x".repeat(Math.floor(r() * 300)), "pick", "skip"]);
  }
}

function randomChannel(seed: number): { meta: ChannelMeta; profile: ChannelProfile } {
  const r = rng(seed);
  const nFields = Math.floor(r() * 30);
  const fields: FieldDecl[] = [];
  for (let i = 0; i < nFields; i++) {
    const base = r() < 0.3 ? pick(r, pool) : null;
    fields.push({
      name: base ? base.name : randomName(r),
      type: base && r() < 0.8 ? base.type : pick(r, TYPES),
      description: r() < 0.6 ? (base?.description ?? pick(r, pool).description) : r() < 0.5 ? "" : null,
    });
  }
  const rows = r() < 0.2 ? 0 : Math.floor(10 ** (r() * 6.5));
  const span = Math.floor(r() * 4000);
  const last = Date.UTC(2026, 10, 20);
  const meta: ChannelMeta = {
    name: r() < 0.9 ? `rnd.c${seed}` : `Odd-Name ${seed}.x`,
    plugin: "rnd",
    description: r() < 0.5 ? pick(r, ZOO).description : "",
    record_kind: pick(r, ["life_event", "reference", "derived", "page_revision", "other"]),
    is_reference: r() < 0.2,
    is_outside: r() < 0.2,
    whole_thread: r() < 0.2,
    fields,
    view: r() < 0.8 ? "" : r() < 0.5 ? "api.ch_rnd" : "weird view",
    rows,
    threads: rows === 0 ? 0 : Math.max(0, Math.floor(rows * r() ** 3)),
    first_at: rows ? new Date(last - span * 86400000).toISOString() : null,
    last_at: rows ? new Date(last).toISOString() : null,
    last_ingest_at: rows ? new Date(last).toISOString() : null,
    with_geo: Math.floor(rows * (r() < 0.7 ? 0 : r())),
    with_ended_at: Math.floor(rows * (r() < 0.6 ? 0 : r())),
    with_images: 0,
  };
  const profile = declaredProfile(meta);
  if (rows > 0 && r() < 0.8) {
    profile.source = "observed";
    Object.assign(profile.envelope, {
      people: Math.floor(r() * 50),
      authors: Math.floor(r() * 80),
      with_geo_source: r() < 0.5 ? null : Math.floor(meta.with_geo * r()),
      avg_content_chars: r() < 0.1 ? null : r() * 8000,
      hours_used: r() < 0.2 ? null : Math.floor(r() * 25),
      min_duration_s: r() < 0.5 ? null : r() * 100,
      avg_duration_s: r() < 0.5 ? null : r() * 90000,
      max_duration_s: r() < 0.5 ? null : r() * 900000,
    });
    profile.sampled = rows > 50000;
    const sampleRows = Math.floor(r() * 80);
    profile.sample = Array.from({ length: sampleRows }, (_x, i) => {
      const row: Row = { id: i, happened_at: new Date(last - i * 3600000).toISOString(), thread_id: i % 5 };
      for (const f of profile.fields) row[f.name] = randomValue(r, f.kind, i);
      return row;
    });
    profile.fields = profile.fields.map((f): FieldProfile => {
      if (r() < 0.25) return f;
      const observed = Math.floor(r() * 500);
      const out: FieldProfile = { ...f, observed, present: Math.floor(rows * r()), fill: r(), distinct: r() < 0.1 ? null : Math.floor(r() * (observed + 1)) };
      const lo = (r() - 0.3) * 1000;
      if (f.kind === "integer" || f.kind === "number") out.numeric = { min: lo, max: r() < 0.15 ? lo : lo + r() * 5000, avg: lo + 1, stddev: r() < 0.5 ? null : r() * 10, integral: r() < 0.5 };
      if (f.kind === "text") out.text = { avg_len: r() * 300, max_len: 900, top: Array.from({ length: Math.floor(r() * 12) }, (_y, i) => ({ value: pick(r, ["ok", "error", "a'b", `v${i}`]), n: 10 - i })), date_like: r() < 0.2 ? 1 : 0, url_like: r() < 0.2 ? 1 : 0 };
      if (f.kind === "boolean") out.boolean = { true_share: r() };
      if (f.kind === "timestamp") out.time = { min: "2020-01-01T00:00:00Z", max: "2027-01-01T00:00:00Z", future_share: r(), lead_s: r() < 0.5 ? null : r() * 1000 };
      if (f.kind === "json") out.json = { shape: pick(r, ["array_scalar", "array_object", "object", "scalar", "mixed"] as const), avg_items: r() < 0.5 ? null : r() * 5, keys: r() < 0.5 ? [] : ["k", "a b"] };
      return out;
    });
  }
  return { meta, profile };
}

describe("lens selection for random channels", () => {
  test("never throws, always has the spine and the table, and every statement compiles", () => {
    let total = 0;
    for (let seed = 1; seed <= 400; seed++) {
      const { meta, profile } = randomChannel(seed);
      const r = rng(seed * 7919);
      const overrides =
        r() < 0.3
          ? { fields: Object.fromEntries(meta.fields.slice(0, 3).map((f) => [f.name, { role: pick(r, ROLE_IDS), hidden: r() < 0.3 }])), hidden: ["timeseries", "table"], pinned: ["histogram", "nope"] }
          : null;
      const a = analyzeChannel(meta, r() < 0.15 ? null : profile, overrides, { caps: r() < 0.5 ? EXTENDED : NARROW, siblings: r() < 0.2 ? [meta, { ...meta, name: `${meta.name}2` }] : [] });
      expect(a.errors, `seed ${seed}`).toEqual([]);
      const lenses = a.candidates.map((c) => c.spec.lens);
      expect(lenses, `seed ${seed}`).toContain("table");
      expect(lenses, `seed ${seed}`).toContain("spine");
      expect(a.archetypes.scores.length).toBe(10);
      for (const c of a.candidates) {
        total++;
        expect(Number.isFinite(c.score) && c.score >= 0 && c.score <= 1, `seed ${seed} ${c.spec.lens} score ${c.score}`).toBe(true);
        expect(c.spec.title.length).toBeGreaterThan(0);
        const compiled = compileView(c.spec, { from: "2026-01-01", to: "2026-06-01", tz: "Asia/Kolkata" });
        expect(compiled.unresolved, `seed ${seed} ${c.spec.lens}`).toEqual([]);
        expect(scanParams(compiled.sql), `seed ${seed} ${c.spec.lens}: ${compiled.sql}`).toEqual([]);
        expect(compiled.sql).not.toMatch(/NaN|undefined|Infinity\b(?!')/);
      }
      expect(() => startersForChannel(meta, profile)).not.toThrow();
    }
    expect(total).toBeGreaterThan(4000);
  });

  test("garbage in the metadata is tolerated", () => {
    const bad = [
      { name: "x" },
      { name: "x", fields: null, rows: Number.NaN },
      { name: "x", fields: [null, 3, { name: 7 }, { name: "" }, { name: "ok" }, { name: "ok" }, { name: "f", type: null, description: 5 }], rows: -5, threads: "many" },
      { name: null, plugin: undefined, description: 12, fields: [{ name: "a", type: "number" }], rows: 10, with_ended_at: "x" },
    ];
    for (const meta of bad) {
      const a = analyzeChannel(meta as unknown as ChannelMeta);
      expect(a.errors).toEqual([]);
      expect(a.candidates.map((c) => c.spec.lens)).toEqual(expect.arrayContaining(["spine", "table"]));
    }
  });
});

describe("lens selection for random results", () => {
  test("never throws and always returns the table", () => {
    for (let seed = 1; seed <= 400; seed++) {
      const r = rng(seed + 10000);
      const nCols = Math.floor(r() * 8);
      const cols = Array.from({ length: nCols }, (_x, i) => ({ name: r() < 0.15 && i > 0 ? "dup" : randomName(r), type: pick(r, TYPES) }));
      const nRows = r() < 0.15 ? 0 : r() < 0.2 ? 1 : Math.floor(r() * 400);
      const rows: Row[] = Array.from({ length: nRows }, (_x, i) => {
        const row: Row = {};
        for (const c of cols) row[c.name] = r() < 0.05 ? (i % 2 ? "mixed" : 3) : randomValue(r, c.type, i);
        return row;
      });
      const typed = r() < 0.5 ? cols.map((c) => ({ name: c.name, type: pick(r, ["int8", "float8", "numeric", "text", "timestamptz", "date", "jsonb", "bool", "interval", "_text", "unknown", ""]) })) : [];
      const profile = profileResult(rows, typed, r() < 0.2);
      const statement = pick(r, ["SELECT * FROM api.messages", "SELECT 1;", "", "SELECT 1; SELECT 2", "WITH a AS (SELECT 1 AS x) SELECT x FROM a -- c", "SELECT ':from' AS a /* ; */"]);
      const a = analyzeResult(profile, statement, { caps: r() < 0.5 ? EXTENDED : NARROW });
      expect(a.errors, `seed ${seed}`).toEqual([]);
      expect(a.candidates.map((c) => c.spec.lens), `seed ${seed}`).toContain("table");
      for (const c of a.candidates) {
        expect(Number.isFinite(c.score)).toBe(true);
        expect(() => compileView(c.spec, { tz: "UTC" })).not.toThrow();
        if (c.spec.provenance.statement === "as_is" && statement.trim()) expect(c.spec.source.sql).toBe(statement);
      }
      const lines = describeResult(profile);
      expect(lines.length).toBeGreaterThan(0);
      for (const l of lines) expect(l).not.toMatch(/undefined/);
    }
  });
});
