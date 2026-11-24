// Profiles: what is known about a channel (gathered by a handful of aggregate statements)
// or about an arbitrary SQL result (computed from the returned rows; pure, worker-safe).

import {
  type Capabilities,
  EXTENDED,
  channelView,
  quoteIdent,
  sampleModulo,
  sqlFns,
} from "./sql";
import type {
  Cadence,
  CadenceClass,
  ChannelMeta,
  ChannelProfile,
  ColumnKind,
  Envelope,
  FieldDecl,
  FieldProfile,
  JsonShape,
  JsonStats,
  ResultProfile,
  Row,
  RunSql,
  SqlColumn,
  ThreadClass,
  ThreadShape,
} from "./types";

// ---------------------------------------------------------------- helpers

export function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "bigint") return Number(value);
  if (typeof value === "boolean") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_TS = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}(:?\d{2})?)?$/;
const NUMERIC_TEXT = /^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?$/;

export function fieldKind(type: string): ColumnKind {
  switch (type) {
    case "text": return "text";
    case "integer": return "integer";
    case "number": return "number";
    case "boolean": return "boolean";
    case "timestamp": return "timestamp";
    case "json": return "json";
    default: return "unknown";
  }
}

function clipText(s: string, n = 80): string {
  return s.length > n ? s.slice(0, n) : s;
}

function median(xs: number[]): number {
  if (xs.length === 0) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? (s[mid] as number) : ((s[mid - 1] as number) + (s[mid] as number)) / 2;
}

function daysBetween(a: string | null, b: string | null): number {
  if (!a || !b) return 0;
  const ms = Date.parse(b) - Date.parse(a);
  return Number.isFinite(ms) ? Math.max(0, ms / 86400000) : 0;
}

export function jsonStats(values: unknown[]): JsonStats | undefined {
  const present = values.filter((v) => v !== null && v !== undefined);
  if (present.length === 0) return undefined;
  let arrays = 0;
  let objects = 0;
  let scalars = 0;
  let items = 0;
  let objectItems = 0;
  let scalarItems = 0;
  const keys = new Map<string, number>();
  const bump = (o: object) => {
    for (const k of Object.keys(o).slice(0, 60)) keys.set(k, (keys.get(k) ?? 0) + 1);
  };
  for (const v of present) {
    if (Array.isArray(v)) {
      arrays++;
      items += v.length;
      for (const item of v.slice(0, 20)) {
        if (item !== null && typeof item === "object" && !Array.isArray(item)) {
          objectItems++;
          bump(item);
        } else scalarItems++;
      }
    } else if (typeof v === "object") {
      objects++;
      bump(v as object);
    } else scalars++;
  }
  let shape: JsonShape = "mixed";
  const n = present.length;
  // Arrays that are all empty say nothing about what they would hold.
  if (arrays >= n * 0.8) shape = objectItems + scalarItems === 0 ? "mixed" : objectItems > scalarItems ? "array_object" : "array_scalar";
  else if (objects >= n * 0.8) shape = "object";
  else if (scalars >= n * 0.8) shape = "scalar";
  return {
    shape,
    avg_items: arrays > 0 ? items / arrays : null,
    keys: [...keys.entries()].sort((a, b) => b[1] - a[1]).slice(0, 40).map(([k]) => k),
  };
}

// ---------------------------------------------------------------- channel: statements

export interface ProfileStatement {
  key: "envelope" | "presence" | "numeric" | "text" | "text_top" | "boolean" | "time" | "cadence" | "threads" | "sample";
  sql: string;
  row_cap: number;
}

export interface ProfilePlan {
  channel: string;
  statements: ProfileStatement[];
  sampled: boolean;
  modulo: number;
  /** Field names in the order their indexed aliases (n0_, t0_, b0_, s0_) use. */
  numeric: string[];
  text: string[];
  boolean: string[];
  time: string[];
}

export interface ProfileOptions {
  caps?: Capabilities;
  /** Row count above which value statistics run on a sample (default 50,000). */
  sampleAbove?: number;
  /** Rows the sample aims for (default 20,000). */
  sampleTarget?: number;
  /** Rows of the spread sample (default 200). */
  spread?: number;
}

/**
 * The statements that profile one channel. Counts (envelope, presence, cadence, threads)
 * are always exact; value statistics switch to an `id % k = 0` sample above 50,000 rows.
 */
export function profilePlan(meta: ChannelMeta, opts: ProfileOptions = {}): ProfilePlan {
  const caps = opts.caps ?? EXTENDED;
  const f = sqlFns(caps);
  const view = channelView(meta);
  const rows = Math.max(0, meta.rows ?? 0);
  const sampled = rows > (opts.sampleAbove ?? 50000);
  const modulo = sampled ? sampleModulo(rows, opts.sampleTarget ?? 20000) : 1;
  const where = modulo > 1 ? ` WHERE id % ${modulo} = 0` : "";
  const of = (pred: (d: FieldDecl) => boolean) => meta.fields.filter(pred).map((d) => d.name);
  const numeric = of((d) => d.type === "integer" || d.type === "number");
  const text = of((d) => d.type === "text");
  const boolean = of((d) => d.type === "boolean");
  const time = of((d) => d.type === "timestamp");
  const statements: ProfileStatement[] = [];

  statements.push({
    key: "envelope",
    row_cap: 1,
    sql:
      `SELECT count(*) AS rows, min(happened_at) AS first_at, max(happened_at) AS last_at, ` +
      `max(ingested_at) AS last_ingest_at, count(DISTINCT thread_id) AS threads, ` +
      `count(DISTINCT person_id) AS people, count(DISTINCT author_handle) AS authors, ` +
      `count(ended_at) AS with_ended_at, count(lat) AS with_geo, ` +
      `count(*) FILTER (WHERE lat IS NOT NULL AND loc_method = 'source') AS with_geo_source, ` +
      `count(*) FILTER (WHERE images IS NOT NULL AND images <> '[]'::jsonb) AS with_images, ` +
      `avg(length(content))::float8 AS avg_content_chars, max(length(content)) AS max_content_chars, ` +
      `count(*) FILTER (WHERE content IS NULL OR content = '') AS content_empty, ` +
      `max(version) AS max_version, count(*) FILTER (WHERE happened_at > now()) AS future_rows, ` +
      `count(DISTINCT extract(hour FROM happened_at)) AS hours_used, ` +
      `min(extract(epoch FROM ended_at - happened_at))::float8 AS min_duration_s, ` +
      `avg(extract(epoch FROM ended_at - happened_at))::float8 AS avg_duration_s, ` +
      `max(extract(epoch FROM ended_at - happened_at))::float8 AS max_duration_s ` +
      `FROM ${view}`,
  });

  if (meta.fields.length > 0) {
    const cols = meta.fields.map((d, i) => `count(${quoteIdent(d.name)}) AS p${i}`);
    statements.push({ key: "presence", row_cap: 1, sql: `SELECT ${cols.join(", ")} FROM ${view}` });
  }
  if (numeric.length > 0) {
    const cols = numeric.flatMap((name, i) => {
      const c = quoteIdent(name);
      return [
        `min(${c})::float8 AS n${i}_min`,
        `max(${c})::float8 AS n${i}_max`,
        `avg(${c})::float8 AS n${i}_avg`,
        `${f.stddev(c)} AS n${i}_sd`,
        `count(DISTINCT ${c}) AS n${i}_dist`,
        `count(*) FILTER (WHERE ${c} <> round(${c})) AS n${i}_frac`,
        `count(${c}) AS n${i}_n`,
      ];
    });
    statements.push({ key: "numeric", row_cap: 1, sql: `SELECT ${cols.join(", ")} FROM ${view}${where}` });
  }
  if (text.length > 0) {
    const cols = text.flatMap((name, i) => {
      const c = quoteIdent(name);
      return [
        `count(DISTINCT left(${c}, 200)) AS t${i}_dist`,
        `avg(length(${c}))::float8 AS t${i}_len`,
        `max(length(${c})) AS t${i}_max`,
        `count(${c}) AS t${i}_n`,
      ];
    });
    statements.push({ key: "text", row_cap: 1, sql: `SELECT ${cols.join(", ")} FROM ${view}${where}` });
    const inner = text.map((name, i) => `left(${quoteIdent(name)}, 80) AS t${i}`).join(", ");
    const arms = text.map(
      (_name, i) =>
        `(SELECT ${i} AS f, t${i} AS value, count(*) AS n FROM s WHERE t${i} IS NOT NULL GROUP BY 2 ORDER BY 3 DESC, 2 LIMIT 12)`,
    );
    statements.push({
      key: "text_top",
      row_cap: Math.min(10000, text.length * 12),
      sql: `WITH s AS MATERIALIZED (SELECT ${inner} FROM ${view}${where}) ${arms.join(" UNION ALL ")}`,
    });
  }
  if (boolean.length > 0) {
    const cols = boolean.flatMap((name, i) => {
      const c = quoteIdent(name);
      return [`count(*) FILTER (WHERE ${c}) AS b${i}_t`, `count(${c}) AS b${i}_n`];
    });
    statements.push({ key: "boolean", row_cap: 1, sql: `SELECT ${cols.join(", ")} FROM ${view}${where}` });
  }
  if (time.length > 0) {
    const cols = time.flatMap((name, i) => {
      const c = quoteIdent(name);
      return [
        `min(${c}) AS s${i}_min`,
        `max(${c}) AS s${i}_max`,
        `count(*) FILTER (WHERE ${c} > now()) AS s${i}_future`,
        `avg(extract(epoch FROM ${c} - happened_at))::float8 AS s${i}_lead`,
        `count(DISTINCT ${c}) AS s${i}_dist`,
        `count(${c}) AS s${i}_n`,
      ];
    });
    statements.push({ key: "time", row_cap: 1, sql: `SELECT ${cols.join(", ")} FROM ${view}${where}` });
  }
  statements.push({
    key: "cadence",
    row_cap: 10000,
    sql: `SELECT (happened_at AT TIME ZONE 'UTC')::date AS day, count(*) AS n FROM ${view} GROUP BY 1 ORDER BY 1 DESC`,
  });
  statements.push({
    key: "threads",
    row_cap: 200,
    sql:
      `SELECT thread_id, count(*) AS n, count(DISTINCT coalesce(person_id::text, author_handle)) AS speakers ` +
      `FROM ${view} GROUP BY 1 ORDER BY n DESC, 1 LIMIT 200`,
  });
  const spread = Math.max(1, opts.spread ?? 200);
  const k = Math.max(1, Math.floor(rows / spread));
  statements.push({
    key: "sample",
    row_cap: spread,
    sql:
      `SELECT id, thread_id, thread_no, happened_at, ended_at, author_handle, person_id, ` +
      `left(content, 300) AS content, length(content) AS content_chars, lat, lon, fields ` +
      `FROM ${view}${k > 1 ? ` WHERE id % ${k} = 0` : ""} ORDER BY id LIMIT ${spread}`,
  });
  return { channel: meta.name, statements, sampled, modulo, numeric, text, boolean, time };
}

// ---------------------------------------------------------------- channel: assembly

function envelopeFromMeta(meta: ChannelMeta): Envelope {
  return {
    rows: Math.max(0, meta.rows ?? 0),
    first_at: meta.first_at ?? null,
    last_at: meta.last_at ?? null,
    last_ingest_at: meta.last_ingest_at ?? null,
    threads: Math.max(0, meta.threads ?? 0),
    people: 0,
    authors: 0,
    with_ended_at: Math.max(0, meta.with_ended_at ?? 0),
    with_geo: Math.max(0, meta.with_geo ?? 0),
    with_geo_source: null,
    with_images: Math.max(0, meta.with_images ?? 0),
    avg_content_chars: null,
    max_content_chars: null,
    content_empty: null,
    max_version: null,
    future_rows: null,
    hours_used: null,
    min_duration_s: null,
    avg_duration_s: null,
    max_duration_s: null,
  };
}

function emptyField(d: FieldDecl): FieldProfile {
  return {
    name: d.name,
    kind: fieldKind(d.type),
    declared: true,
    description: d.description ?? "",
    observed: 0,
    present: null,
    fill: null,
    distinct: null,
  };
}

function cadenceOf(days: { day: string; n: number }[], env: Envelope): Cadence {
  if (env.rows === 0) {
    return { class: "none", span_days: 0, active_days: 0, coverage: 0, per_active_day_median: 0, per_active_day_max: 0, median_gap_days: 0, days: [] };
  }
  const sorted = [...days].sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0));
  const span = Math.max(1, Math.round(daysBetween(env.first_at, env.last_at)) + 1);
  if (sorted.length === 0) {
    // Only envelope counts are known: estimate.
    const perDay = env.rows / span;
    const cls: CadenceClass = perDay >= 3 ? "continuous" : perDay >= 0.7 ? "daily" : perDay >= 0.15 ? "regular" : "sparse";
    const active = Math.min(span, env.rows);
    return {
      class: cls,
      span_days: span,
      active_days: active,
      coverage: active / span,
      per_active_day_median: Math.max(1, Math.round(perDay)),
      per_active_day_max: Math.max(1, Math.round(perDay)),
      median_gap_days: active > 1 ? span / active : 0,
      days: [],
    };
  }
  const counts = sorted.map((d) => d.n);
  const gaps: number[] = [];
  for (let i = 1; i < sorted.length; i++) {
    gaps.push(Math.round(daysBetween((sorted[i - 1] as { day: string }).day, (sorted[i] as { day: string }).day)));
  }
  const active = sorted.length;
  const coverage = Math.min(1, active / span);
  const med = median(counts);
  const max = Math.max(...counts);
  const gap = gaps.length ? median(gaps) : 0;
  let cls: CadenceClass;
  if (coverage >= 0.7) cls = med > 3 ? "continuous" : "daily";
  else if (coverage >= 0.2 && gap <= 8) cls = "regular";
  else if (max >= Math.max(10, med * 8) || (coverage < 0.2 && med >= 5)) cls = "bursty";
  else cls = "sparse";
  return {
    class: cls,
    span_days: span,
    active_days: active,
    coverage,
    per_active_day_median: med,
    per_active_day_max: max,
    median_gap_days: gap,
    days: sorted.slice(-400),
  };
}

function threadsOf(rows: Row[], env: Envelope): ThreadShape {
  const count = env.threads;
  if (env.rows === 0 || count === 0) {
    return { class: env.rows === 0 ? "none" : "single", count, rows_per_thread: env.rows, median_size: env.rows, max_size: env.rows, top_share: env.rows ? 1 : 0, median_speakers: 0 };
  }
  const rpt = env.rows / count;
  const sizes = rows.map((r) => num(r.n) ?? 0).filter((n) => n > 0);
  const speakers = rows.map((r) => num(r.speakers) ?? 0);
  // The listing holds the 200 largest threads; with more threads than that the median of
  // the listing overstates, so fall back to the mean.
  const med = sizes.length > 0 && count <= sizes.length ? median(sizes) : rpt;
  const max = sizes.length > 0 ? Math.max(...sizes) : rpt;
  let cls: ThreadClass;
  if (count <= 1) cls = "single";
  else if (rpt < 1.3) cls = "one_per_record";
  else if (count <= 60 && rpt >= 20) cls = "few_large";
  else if (rpt < 5) cls = "many_small";
  else cls = "mixed";
  return {
    class: cls,
    count,
    rows_per_thread: rpt,
    median_size: med,
    max_size: max,
    top_share: env.rows > 0 ? Math.min(1, max / env.rows) : 0,
    median_speakers: speakers.length ? median(speakers.slice(0, 50)) : 0,
  };
}

function flattenSample(rows: Row[]): Row[] {
  return rows.map((r) => {
    const fields = r.fields !== null && typeof r.fields === "object" && !Array.isArray(r.fields) ? (r.fields as Row) : {};
    const { fields: _drop, ...rest } = r;
    void _drop;
    return { ...fields, ...rest };
  });
}

function shares(values: unknown[]): { date_like: number; url_like: number } {
  const xs = values.filter((v): v is string => typeof v === "string" && v.length > 0);
  if (xs.length === 0) return { date_like: 0, url_like: 0 };
  let dates = 0;
  let urls = 0;
  for (const x of xs) {
    if (ISO_DATE.test(x) || ISO_TS.test(x)) dates++;
    if (/^https?:\/\//i.test(x)) urls++;
  }
  return { date_like: dates / xs.length, url_like: urls / xs.length };
}

export function cacheKey(meta: Pick<ChannelMeta, "name" | "rows" | "last_ingest_at">): string {
  return `${meta.name}|${meta.rows ?? 0}|${meta.last_ingest_at ?? ""}`;
}

/** A profile from declared fields and bootstrap counts alone: what a channel page shows first. */
export function declaredProfile(meta: ChannelMeta): ChannelProfile {
  const envelope = envelopeFromMeta(meta);
  return {
    v: 1,
    channel: meta.name,
    source: envelope.rows > 0 ? "envelope" : "declared",
    rows: envelope.rows,
    sampled: false,
    sample_modulo: 1,
    envelope,
    fields: meta.fields.map(emptyField),
    cadence: cadenceOf([], envelope),
    threads: threadsOf([], envelope),
    sample: [],
    cache_key: cacheKey(meta),
  };
}

/** Build the profile from the rows each statement of the plan returned (missing keys are tolerated). */
export function assembleChannelProfile(
  meta: ChannelMeta,
  plan: ProfilePlan,
  results: Partial<Record<ProfileStatement["key"], Row[]>>,
): ChannelProfile {
  const env = envelopeFromMeta(meta);
  const e = results.envelope?.[0];
  if (e) {
    env.rows = num(e.rows) ?? env.rows;
    env.first_at = (e.first_at as string | null) ?? env.first_at;
    env.last_at = (e.last_at as string | null) ?? env.last_at;
    env.last_ingest_at = (e.last_ingest_at as string | null) ?? env.last_ingest_at;
    env.threads = num(e.threads) ?? env.threads;
    env.people = num(e.people) ?? 0;
    env.authors = num(e.authors) ?? 0;
    env.with_ended_at = num(e.with_ended_at) ?? env.with_ended_at;
    env.with_geo = num(e.with_geo) ?? env.with_geo;
    env.with_geo_source = num(e.with_geo_source);
    env.with_images = num(e.with_images) ?? env.with_images;
    env.avg_content_chars = num(e.avg_content_chars);
    env.max_content_chars = num(e.max_content_chars);
    env.content_empty = num(e.content_empty);
    env.max_version = num(e.max_version);
    env.future_rows = num(e.future_rows);
    env.hours_used = num(e.hours_used);
    env.min_duration_s = num(e.min_duration_s);
    env.avg_duration_s = num(e.avg_duration_s);
    env.max_duration_s = num(e.max_duration_s);
  }
  const sample = flattenSample(results.sample ?? []);
  const presence = results.presence?.[0];
  const numeric = results.numeric?.[0];
  const text = results.text?.[0];
  const bool = results.boolean?.[0];
  const time = results.time?.[0];
  const tops = new Map<number, { value: string; n: number }[]>();
  for (const r of results.text_top ?? []) {
    const i = num(r.f);
    if (i === null || typeof r.value !== "string") continue;
    const list = tops.get(i) ?? [];
    list.push({ value: r.value, n: num(r.n) ?? 0 });
    tops.set(i, list);
  }

  const fields: FieldProfile[] = meta.fields.map((d, idx) => {
    const fp = emptyField(d);
    const present = presence ? num(presence[`p${idx}`]) : null;
    fp.present = present;
    fp.fill = present !== null && env.rows > 0 ? Math.min(1, present / env.rows) : null;
    const values = sample.map((r) => r[d.name]);
    const ni = plan.numeric.indexOf(d.name);
    const ti = plan.text.indexOf(d.name);
    const bi = plan.boolean.indexOf(d.name);
    const si = plan.time.indexOf(d.name);
    if (ni >= 0 && numeric) {
      const n = num(numeric[`n${ni}_n`]) ?? 0;
      fp.observed = n;
      fp.distinct = num(numeric[`n${ni}_dist`]);
      const min = num(numeric[`n${ni}_min`]);
      const max = num(numeric[`n${ni}_max`]);
      const avg = num(numeric[`n${ni}_avg`]);
      if (n > 0 && min !== null && max !== null && avg !== null) {
        fp.numeric = { min, max, avg, stddev: num(numeric[`n${ni}_sd`]), integral: (num(numeric[`n${ni}_frac`]) ?? 0) === 0 };
      }
    } else if (ti >= 0 && text) {
      const n = num(text[`t${ti}_n`]) ?? 0;
      fp.observed = n;
      fp.distinct = num(text[`t${ti}_dist`]);
      if (n > 0) {
        fp.text = {
          avg_len: num(text[`t${ti}_len`]) ?? 0,
          max_len: num(text[`t${ti}_max`]) ?? 0,
          top: tops.get(ti) ?? [],
          ...shares(values),
        };
      }
    } else if (bi >= 0 && bool) {
      const n = num(bool[`b${bi}_n`]) ?? 0;
      fp.observed = n;
      if (n > 0) {
        const t = num(bool[`b${bi}_t`]) ?? 0;
        fp.boolean = { true_share: t / n };
        fp.distinct = t === 0 || t === n ? 1 : 2;
      }
    } else if (si >= 0 && time) {
      const n = num(time[`s${si}_n`]) ?? 0;
      fp.observed = n;
      fp.distinct = num(time[`s${si}_dist`]);
      if (n > 0) {
        fp.time = {
          min: (time[`s${si}_min`] as string | null) ?? null,
          max: (time[`s${si}_max`] as string | null) ?? null,
          future_share: (num(time[`s${si}_future`]) ?? 0) / n,
          lead_s: num(time[`s${si}_lead`]),
        };
      }
    } else if (fp.kind === "json") {
      const js = jsonStats(values);
      if (js) {
        fp.json = js;
        fp.observed = values.filter((v) => v !== null && v !== undefined).length;
      }
    }
    return fp;
  });

  const days = (results.cadence ?? [])
    .map((r) => ({ day: String(r.day ?? ""), n: num(r.n) ?? 0 }))
    .filter((d) => ISO_DATE.test(d.day));
  return {
    v: 1,
    channel: meta.name,
    source: env.rows > 0 ? (results.envelope ? "observed" : "envelope") : "declared",
    rows: env.rows,
    sampled: plan.sampled,
    sample_modulo: plan.modulo,
    envelope: env,
    fields,
    cadence: cadenceOf(days, env),
    threads: threadsOf(results.threads ?? [], env),
    sample,
    cache_key: cacheKey({ name: meta.name, rows: env.rows, last_ingest_at: env.last_ingest_at }),
  };
}

/**
 * Profile a channel through the injected data access. A channel bootstrap reports as
 * empty costs no statements at all.
 */
export async function profileChannel(meta: ChannelMeta, runSql: RunSql, opts: ProfileOptions = {}): Promise<ChannelProfile> {
  if (!(meta.rows > 0)) return declaredProfile(meta);
  const plan = profilePlan(meta, opts);
  const results: Partial<Record<ProfileStatement["key"], Row[]>> = {};
  await Promise.all(
    plan.statements.map(async (s) => {
      const res = await runSql(s.sql, { row_cap: s.row_cap });
      results[s.key] = res.rows;
    }),
  );
  return assembleChannelProfile(meta, plan, results);
}

// ---------------------------------------------------------------- results

const PG_KIND: Record<string, ColumnKind> = {
  int2: "integer", int4: "integer", int8: "integer", smallint: "integer", integer: "integer", bigint: "integer", oid: "integer",
  float4: "number", float8: "number", numeric: "number", real: "number", "double precision": "number", money: "number",
  bool: "boolean", boolean: "boolean",
  timestamptz: "timestamp", timestamp: "timestamp",
  "timestamp with time zone": "timestamp", "timestamp without time zone": "timestamp",
  date: "date",
  json: "json", jsonb: "json",
  interval: "interval",
  text: "text", varchar: "text", bpchar: "text", name: "text", uuid: "text", char: "text", "character varying": "text",
  time: "text", timetz: "text",
};

function kindFromType(type: string | undefined): ColumnKind | null {
  if (!type) return null;
  const t = type.toLowerCase().trim();
  if (t.startsWith("_") || t.endsWith("[]")) return "json";
  return PG_KIND[t] ?? PG_KIND[t.replace(/\(.*\)$/, "")] ?? null;
}

function inferKind(values: unknown[]): ColumnKind {
  let seen = 0;
  let ints = 0;
  let nums = 0;
  let bools = 0;
  let stamps = 0;
  let dates = 0;
  let numericText = 0;
  let objects = 0;
  let strings = 0;
  for (const v of values) {
    if (v === null || v === undefined) continue;
    seen++;
    if (typeof v === "number") {
      nums++;
      if (Number.isInteger(v)) ints++;
    } else if (typeof v === "boolean") bools++;
    else if (typeof v === "object") objects++;
    else if (typeof v === "string") {
      strings++;
      if (ISO_DATE.test(v)) dates++;
      else if (ISO_TS.test(v)) stamps++;
      else if (NUMERIC_TEXT.test(v)) numericText++;
    }
  }
  if (seen === 0) return "unknown";
  if (nums === seen) return ints === seen ? "integer" : "number";
  if (bools === seen) return "boolean";
  if (objects === seen) return "json";
  if (strings === seen) {
    if (stamps === seen) return "timestamp";
    if (dates === seen) return "date";
    if (stamps + dates === seen) return "timestamp";
    // numeric and bigint aggregates arrive as strings; ids with leading zeros do not match.
    if (numericText === seen) return "number";
    return "text";
  }
  return objects > 0 ? "json" : "text";
}

function keyOf(v: unknown): string {
  if (v === null || v === undefined) return "\u0000null";
  return typeof v === "object" ? JSON.stringify(v) : `${typeof v}:${String(v)}`;
}

const MAX_RESULT_ROWS = 10000;

/**
 * Profile the rows of any SQL result. Pure and synchronous, at most 10,000 rows are
 * examined: meant to run in a worker.
 */
export function profileResult(rows: Row[], columns: SqlColumn[] = [], truncated = false): ResultProfile {
  const data = rows.length > MAX_RESULT_ROWS ? rows.slice(0, MAX_RESULT_ROWS) : rows;
  const names: string[] = [];
  const seen = new Set<string>();
  const types = new Map<string, string>();
  for (const c of columns) {
    if (!c || typeof c.name !== "string" || seen.has(c.name)) continue;
    seen.add(c.name);
    names.push(c.name);
    types.set(c.name, c.type);
  }
  for (const r of data.slice(0, 50)) {
    if (r === null || typeof r !== "object") continue;
    for (const k of Object.keys(r)) {
      if (!seen.has(k)) {
        seen.add(k);
        names.push(k);
      }
    }
  }
  const now = Date.now();
  const total = data.length;
  const distincts = new Map<string, number>();
  const profiles: FieldProfile[] = names.map((name) => {
    const values = data.map((r) => (r !== null && typeof r === "object" ? (r as Row)[name] : undefined));
    const pg = types.get(name);
    const kind = kindFromType(pg) ?? inferKind(values);
    const present = values.filter((v) => v !== null && v !== undefined);
    const distinct = new Set(present.map(keyOf)).size;
    distincts.set(name, distinct);
    const fp: FieldProfile = {
      name,
      kind,
      declared: false,
      description: "",
      pg_type: pg,
      observed: present.length,
      present: present.length,
      fill: total > 0 ? present.length / total : null,
      distinct,
    };
    if (kind === "integer" || kind === "number") {
      const xs = present.map(num).filter((x): x is number => x !== null);
      if (xs.length > 0) {
        let min = Infinity;
        let max = -Infinity;
        let sum = 0;
        let integral = true;
        for (const x of xs) {
          if (x < min) min = x;
          if (x > max) max = x;
          sum += x;
          if (!Number.isInteger(x)) integral = false;
        }
        const avg = sum / xs.length;
        let ss = 0;
        for (const x of xs) ss += (x - avg) * (x - avg);
        fp.numeric = { min, max, avg, stddev: xs.length > 1 ? Math.sqrt(ss / (xs.length - 1)) : null, integral };
      }
    } else if (kind === "text") {
      const xs = present.map((v) => String(v));
      if (xs.length > 0) {
        const counts = new Map<string, number>();
        let len = 0;
        let maxLen = 0;
        for (const x of xs) {
          len += x.length;
          if (x.length > maxLen) maxLen = x.length;
          const k = clipText(x);
          counts.set(k, (counts.get(k) ?? 0) + 1);
        }
        fp.text = {
          avg_len: len / xs.length,
          max_len: maxLen,
          top: [...counts.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).slice(0, 12).map(([value, n]) => ({ value, n })),
          ...shares(xs.slice(0, 500)),
        };
      }
    } else if (kind === "boolean") {
      if (present.length > 0) fp.boolean = { true_share: present.filter((v) => v === true).length / present.length };
    } else if (kind === "timestamp" || kind === "date") {
      const xs = present.map((v) => String(v)).sort();
      if (xs.length > 0) {
        const future = xs.filter((x) => Date.parse(x) > now).length;
        fp.time = { min: xs[0] ?? null, max: xs[xs.length - 1] ?? null, future_share: future / xs.length, lead_s: null };
      }
    } else if (kind === "json") {
      fp.json = jsonStats(present.slice(0, 300));
    }
    return fp;
  });

  // Unique keys: single columns, then pairs of the first few key-like columns.
  const unique: string[][] = [];
  if (total > 0) {
    const keyish = profiles.filter(
      (p) => p.present === total && ["text", "integer", "timestamp", "date", "boolean"].includes(p.kind),
    );
    const singles = new Set<string>();
    for (const p of keyish) {
      if (distincts.get(p.name) === total) {
        unique.push([p.name]);
        singles.add(p.name);
      }
    }
    const rest = keyish.filter((p) => !singles.has(p.name)).slice(0, 8);
    for (let i = 0; i < rest.length; i++) {
      for (let j = i + 1; j < rest.length; j++) {
        const a = (rest[i] as FieldProfile).name;
        const b = (rest[j] as FieldProfile).name;
        if ((distincts.get(a) ?? 0) * (distincts.get(b) ?? 0) < total) continue;
        const combos = new Set<string>();
        for (const r of data) combos.add(`${keyOf((r as Row)[a])}\u0001${keyOf((r as Row)[b])}`);
        if (combos.size === total) unique.push([a, b]);
      }
    }
  }
  return { v: 1, rows: rows.length, truncated, columns: profiles, unique, sample: data.slice(0, 200) };
}
