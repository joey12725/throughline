// Conformance: every statement the lens engine can produce, run against a live server.
//
// Run with `pnpm exec vitest run --config vitest.conformance.config.ts` after exporting
// THROUGHLINE_URL and THROUGHLINE_TOKEN. Excluded from `pnpm test`.
//
// For each channel of the server it gathers the profile, ranks the lenses and executes
// every candidate's statement (all time, and a bounded scope in a non-UTC zone), the
// declared-fields-only path, the starters, and the lenses of a few SQL results. It does
// this twice: with the extended builtin allow-list the compiler assumes by default, and
// with the original narrow list, where every builtin goes through its fallback. Any
// response that is not HTTP 200 is a failure and is reported with the server's own error.
//
// Optional: CONFORMANCE_REPORT=<path> writes the full report (failures, and the table of
// primary archetype and top lenses per channel) as JSON.

import { writeFileSync } from "node:fs";
import { describe, expect, test } from "vitest";
import {
  type Capabilities,
  type ChannelMeta,
  type ChannelProfile,
  EXTENDED,
  NARROW,
  type Row,
  type RunSql,
  type Scope,
  type SqlResult,
  type ViewSpec,
  analyzeChannel,
  analyzeResult,
  builtinProbes,
  channelFamilies,
  channelViewName,
  compileView,
  profileChannel,
  profilePlan,
  profileResult,
  startersForChannel,
} from "./index";

const URL_BASE = (process.env.THROUGHLINE_URL ?? "").replace(/\/$/, "");
const TOKEN = process.env.THROUGHLINE_TOKEN ?? "";
const ONLY = (process.env.CONFORMANCE_CHANNELS ?? "").split(",").map((s) => s.trim()).filter(Boolean);

interface Failure {
  mode: string;
  channel: string;
  what: string;
  status: number;
  detail: string;
  code: string | null;
  sql: string;
}

class SqlHttpError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly code: string | null,
  ) {
    super(`HTTP ${status}: ${detail}${code ? ` (${code})` : ""}`);
  }
}

const headers = { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Throughline-Client": "web" };

async function post(statement: string, row_cap: number): Promise<SqlResult> {
  const res = await fetch(`${URL_BASE}/v1/sql`, {
    method: "POST",
    headers,
    body: JSON.stringify({ statement, row_cap: Math.max(1, Math.min(10000, row_cap)) }),
  });
  const text = await res.text();
  let body: Record<string, unknown> = {};
  try {
    body = JSON.parse(text) as Record<string, unknown>;
  } catch {
    body = { detail: text.slice(0, 300) };
  }
  if (res.status !== 200) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body).slice(0, 300);
    throw new SqlHttpError(res.status, detail, typeof body.code === "string" ? body.code : null);
  }
  return {
    rows: (body.rows as Row[]) ?? [],
    columns: Array.isArray(body.columns) ? (body.columns as SqlResult["columns"]) : [],
    truncated: body.truncated === true,
  };
}

/** Run tasks with bounded concurrency. */
async function pool<T>(items: T[], size: number, fn: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  await Promise.all(
    Array.from({ length: Math.min(size, items.length) }, async () => {
      while (next < items.length) {
        const item = items[next++] as T;
        await fn(item);
      }
    }),
  );
}

async function loadChannels(): Promise<{ metas: ChannelMeta[]; source: string }> {
  try {
    const res = await fetch(`${URL_BASE}/v1/web/bootstrap`, { headers });
    if (res.status === 200) {
      const body = (await res.json()) as { channels?: ChannelMeta[] };
      if (Array.isArray(body.channels) && body.channels.length > 0) return { metas: body.channels, source: "bootstrap" };
    }
  } catch {
    // fall through to SQL
  }
  // The bootstrap route is not on this server (yet): assemble the same shape from SQL.
  const ch = await post(
    "SELECT name, plugin, description, record_kind, is_reference, is_outside, conversation_whole_thread AS whole_thread, fields FROM api.channels ORDER BY name",
    1000,
  );
  const counts = await post(
    "SELECT channel, count(*) AS rows, count(DISTINCT thread_id) AS threads, min(happened_at) AS first_at, max(happened_at) AS last_at, " +
      "max(ingested_at) AS last_ingest_at, count(lat) AS with_geo, count(ended_at) AS with_ended_at, " +
      "count(*) FILTER (WHERE images IS NOT NULL AND images <> '[]'::jsonb) AS with_images FROM api.messages GROUP BY 1",
    1000,
  );
  const byName = new Map(counts.rows.map((r) => [String(r.channel), r]));
  const metas = ch.rows.map((r): ChannelMeta => {
    const c = byName.get(String(r.name)) ?? {};
    return {
      name: String(r.name),
      plugin: String(r.plugin ?? ""),
      description: (r.description as string | null) ?? "",
      record_kind: String(r.record_kind ?? "life_event"),
      is_reference: r.is_reference === true,
      is_outside: r.is_outside === true,
      whole_thread: r.whole_thread === true,
      fields: Array.isArray(r.fields) ? (r.fields as ChannelMeta["fields"]) : [],
      view: channelViewName(String(r.name)),
      rows: Number(c.rows ?? 0),
      threads: Number(c.threads ?? 0),
      first_at: (c.first_at as string | null) ?? null,
      last_at: (c.last_at as string | null) ?? null,
      last_ingest_at: (c.last_ingest_at as string | null) ?? null,
      with_geo: Number(c.with_geo ?? 0),
      with_ended_at: Number(c.with_ended_at ?? 0),
      with_images: Number(c.with_images ?? 0),
    };
  });
  return { metas, source: "sql" };
}

interface ModeReport {
  mode: string;
  statements: number;
  failures: Failure[];
  table: { channel: string; rows: number; primary: string; score: number; top: string[] }[];
  engineErrors: { channel: string; lens: string; message: string }[];
}

function scopesFor(meta: ChannelMeta): Scope[] {
  const last = meta.last_at ? Date.parse(meta.last_at) : Date.now();
  return [
    {},
    { from: new Date(last - 90 * 86400000).toISOString(), to: new Date(last + 86400000).toISOString(), tz: "America/New_York" },
  ];
}

async function runMode(mode: string, caps: Capabilities, metas: ChannelMeta[]): Promise<ModeReport> {
  const report: ModeReport = { mode, statements: 0, failures: [], table: [], engineErrors: [] };
  const seen = new Set<string>();
  const exec = async (channel: string, what: string, sql: string, row_cap: number): Promise<SqlResult | null> => {
    const key = `${row_cap}|${sql}`;
    if (seen.has(key)) return null;
    seen.add(key);
    report.statements++;
    try {
      return await post(sql, row_cap);
    } catch (err) {
      const e = err instanceof SqlHttpError ? err : new SqlHttpError(0, err instanceof Error ? err.message : String(err), null);
      report.failures.push({ mode, channel, what, status: e.status, detail: e.detail, code: e.code, sql });
      return null;
    }
  };
  const runSpec = async (channel: string, what: string, spec: ViewSpec, scopes: Scope[]) => {
    for (const [i, scope] of scopes.entries()) {
      let compiled;
      try {
        compiled = compileView(spec, scope);
      } catch (err) {
        report.failures.push({ mode, channel, what: `${what} (compile)`, status: 0, detail: err instanceof Error ? err.message : String(err), code: null, sql: spec.source.sql });
        continue;
      }
      if (compiled.unresolved.length > 0) {
        report.failures.push({ mode, channel, what: `${what} (unresolved ${compiled.unresolved.join(",")})`, status: 0, detail: "parameters left in the statement", code: null, sql: compiled.sql });
        continue;
      }
      await exec(channel, `${what}${i ? " [scoped]" : ""}`, compiled.sql, compiled.row_cap);
    }
  };

  for (const probe of builtinProbes(caps)) await exec("(builtins)", `builtin ${probe.name}`, probe.sql, 5);

  const families = channelFamilies(metas);
  await pool(metas, 4, async (meta) => {
    const fam = families.find((f) => f.channels.includes(meta.name));
    const siblings = fam ? metas.filter((m) => fam.channels.includes(m.name)) : [];
    const scopes = scopesFor(meta);

    // 1. Profile statements, one by one so each failure is attributed.
    const plan = profilePlan(meta, { caps });
    let profileFailed = false;
    const guarded: RunSql = async (statement, opts) => {
      const key = plan.statements.find((s) => s.sql === statement)?.key ?? "?";
      const res = await exec(meta.name, `profile ${key}`, statement, opts?.row_cap ?? 1000);
      if (!res) profileFailed = true;
      return res ?? { rows: [], columns: [], truncated: false };
    };
    let profile: ChannelProfile | null = null;
    try {
      profile = await profileChannel(meta, guarded, { caps });
    } catch (err) {
      report.engineErrors.push({ channel: meta.name, lens: "(profile)", message: err instanceof Error ? err.message : String(err) });
    }
    void profileFailed;

    // 2. Every candidate of the observed profile.
    const analysis = analyzeChannel(meta, profile, null, { caps, siblings });
    for (const e of analysis.errors) report.engineErrors.push({ channel: meta.name, ...e });
    for (const c of analysis.candidates) {
      const fields = c.spec.provenance.fields?.slice(0, 2).join("+");
      await runSpec(meta.name, `${c.spec.lens}${fields ? `:${fields}` : ""}`, c.spec, scopes);
    }
    const top = analysis.candidates
      .filter((c) => c.spec.lens !== "spine" && c.spec.lens !== "table")
      .slice(0, 5)
      .map((c) => `${c.spec.lens}${c.spec.provenance.fields?.length ? `(${c.spec.provenance.fields.slice(0, 2).join(", ")})` : ""}`);
    report.table.push({
      channel: meta.name,
      rows: meta.rows,
      primary: analysis.archetypes.primary,
      score: analysis.archetypes.scores[0]?.score ?? 0,
      top,
    });

    // 3. The declared-fields-only path (what a channel with no rows gets).
    const empty: ChannelMeta = { ...meta, rows: 0, threads: 0, first_at: null, last_at: null, with_geo: 0, with_ended_at: 0, with_images: 0 };
    const predicted = analyzeChannel(empty, null, null, { caps, siblings });
    for (const e of predicted.errors) report.engineErrors.push({ channel: `${meta.name} (declared)`, ...e });
    for (const c of predicted.candidates) await runSpec(meta.name, `declared ${c.spec.lens}`, c.spec, [{}]);

    // 4. Starters are runnable as they stand.
    for (const s of startersForChannel(meta, profile, { caps, siblings })) await exec(meta.name, "starter", s.statement, s.spec.source.row_cap);

    // 5. Result lenses: profile what three of the channel's own views return and run
    //    every lens of those results, wrapped ones included.
    for (const c of analysis.candidates.filter((x) => ["table", "timeseries", "top_values"].includes(x.spec.lens)).slice(0, 3)) {
      const statement = compileView(c.spec, {}).sql;
      let res: SqlResult;
      try {
        res = await post(statement, c.spec.source.row_cap);
      } catch {
        continue; // already reported above
      }
      const ra = analyzeResult(profileResult(res.rows, res.columns, res.truncated), statement, { caps });
      for (const e of ra.errors) report.engineErrors.push({ channel: `${meta.name} (result)`, ...e });
      for (const rc of ra.candidates) await runSpec(meta.name, `result ${rc.spec.lens} of ${c.spec.lens} (${rc.spec.provenance.statement})`, rc.spec, [{ tz: "Europe/Berlin" }]);
    }
  });
  report.table.sort((a, b) => (a.channel < b.channel ? -1 : 1));
  return report;
}

function summarize(r: ModeReport): string {
  const lines = [`[${r.mode}] ${r.statements} statements, ${r.failures.length} failures, ${r.engineErrors.length} engine errors`];
  const groups = new Map<string, Failure[]>();
  for (const f of r.failures) {
    const key = `${f.status} ${f.detail}${f.code ? ` ${f.code}` : ""} :: ${f.what.replace(/ \[scoped\]$/, "").replace(/^declared /, "")}`;
    groups.set(key, [...(groups.get(key) ?? []), f]);
  }
  for (const [key, fs] of [...groups.entries()].sort((a, b) => b[1].length - a[1].length).slice(0, 60)) {
    lines.push(`  ${fs.length}x ${key}  e.g. ${fs[0]?.channel}: ${fs[0]?.sql.slice(0, 260)}`);
  }
  for (const e of r.engineErrors.slice(0, 20)) lines.push(`  engine error ${e.channel} ${e.lens}: ${e.message}`);
  return lines.join("\n");
}

describe("lens engine conformance against a live server", () => {
  const ready = URL_BASE !== "" && TOKEN !== "";
  const reports: ModeReport[] = [];
  let metas: ChannelMeta[] = [];
  let source = "";

  test("server is reachable and has channels", async () => {
    expect(ready, "export THROUGHLINE_URL and THROUGHLINE_TOKEN first").toBe(true);
    const loaded = await loadChannels();
    metas = ONLY.length ? loaded.metas.filter((m) => ONLY.includes(m.name)) : loaded.metas;
    source = loaded.source;
    expect(metas.length).toBeGreaterThan(0);
  });

  test("narrow allow-list: every statement runs through the fallbacks", async () => {
    const r = await runMode("narrow", NARROW, metas);
    reports.push(r);
    console.log(summarize(r));
    expect(r.engineErrors).toEqual([]);
    expect(r.failures.map((f) => `${f.channel} ${f.what}: ${f.status} ${f.detail} ${f.code ?? ""}`)).toEqual([]);
  });

  test("extended allow-list (the default): every statement runs", async () => {
    const r = await runMode("extended", EXTENDED, metas);
    reports.push(r);
    console.log(summarize(r));
    if (process.env.CONFORMANCE_REPORT) {
      writeFileSync(process.env.CONFORMANCE_REPORT, JSON.stringify({ url: URL_BASE, channels: metas.length, channel_source: source, reports }, null, 1));
    }
    expect(r.engineErrors).toEqual([]);
    expect(r.failures.map((f) => `${f.channel} ${f.what}: ${f.status} ${f.detail} ${f.code ?? ""}`)).toEqual([]);
  });
});
