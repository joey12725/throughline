// A model-free narrator: plain sentences about a result, and starter questions for a
// channel. Used by the helper panel when no model is configured, and as the first thing
// it shows when one is.

import { analyzeChannel, type EngineOptions } from "./engine";
import { lower } from "./lenses/common";
import { num } from "./profile";
import { inferRoles, isMeasure } from "./roles";
import { type Scope, compileView } from "./sql";
import type { ChannelMeta, ChannelProfile, FieldProfile, LensCandidate, LensId, ResultProfile, ViewSpec } from "./types";

function n(x: number): string {
  if (!Number.isFinite(x)) return "unknown";
  if (Number.isInteger(x)) return x.toLocaleString("en-US");
  const a = Math.abs(x);
  return a >= 1000 ? Math.round(x).toLocaleString("en-US") : a >= 10 ? x.toFixed(1) : a >= 1 ? x.toFixed(2) : x.toPrecision(2);
}

function day(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  return /T00:00:00(\.0+)?(Z|\+00:?00)?$/.test(iso) || iso.length <= 10 ? iso.slice(0, 10) : iso.slice(0, 16).replace("T", " ");
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${n(count)} ${count === 1 ? one : many}`;
}

/** A few plain sentences about a result: size, time range, what the numbers and groups look like. */
export function describeResult(profile: ResultProfile): string[] {
  const out: string[] = [];
  const cols = profile.columns;
  if (profile.rows === 0) {
    return [cols.length ? `The statement returned no rows (${plural(cols.length, "column")}: ${cols.map((c) => c.name).slice(0, 8).join(", ")}).` : "The statement returned no rows."];
  }
  const roles = inferRoles(profile);
  const measures = cols.filter((c) => c.numeric && isMeasure(roles[c.name]?.role ?? "identifier"));
  const times = cols.filter((c) => (c.kind === "timestamp" || c.kind === "date") && c.time);
  const cats = cols.filter((c) => c.kind === "text" && c.text && (c.distinct ?? 0) >= 1);
  const unique = (name: string) => profile.unique.some((u) => u.length === 1 && u[0] === name);

  if (profile.rows === 1) {
    const parts = cols
      .slice(0, 8)
      .map((c) => `${c.name} = ${format(profile.sample[0]?.[c.name])}`)
      .join(", ");
    out.push(`One row: ${parts}.`);
    return out;
  }
  out.push(
    `${plural(profile.rows, "row")} and ${plural(cols.length, "column")}${profile.truncated ? "; the result was cut off at the row cap, so totals below describe only what came back" : ""}.`,
  );

  const t = times[0];
  if (t?.time) {
    const per = unique(t.name) ? `, one row per ${t.name}` : "";
    out.push(`${t.name} runs from ${day(t.time.min)} to ${day(t.time.max)}${per}.`);
  }
  const key = cats.find((c) => unique(c.name));
  for (const m of measures.slice(0, 2)) {
    const s = m.numeric as NonNullable<FieldProfile["numeric"]>;
    if (s.min === s.max) {
      out.push(`${m.name} is ${n(s.min)} in every row.`);
      continue;
    }
    let peak = "";
    if (profile.rows <= profile.sample.length) {
      const top = [...profile.sample].sort((a, b) => (num(b[m.name]) ?? -Infinity) - (num(a[m.name]) ?? -Infinity))[0];
      const at = top && (t && unique(t.name) ? day(String(top[t.name])) : key ? String(top[key.name]) : "");
      if (at) peak = `, highest ${t && unique(t.name) ? "on" : "for"} ${at}`;
    }
    out.push(`${m.name} ranges from ${n(s.min)} to ${n(s.max)} (mean ${n(s.avg)})${peak}.`);
  }
  for (const c of cats.filter((x) => !unique(x.name)).slice(0, 2)) {
    const top = c.text?.top ?? [];
    const present = c.present ?? profile.rows;
    if ((c.distinct ?? 0) <= 1) {
      out.push(`${c.name} is "${top[0]?.value ?? ""}" in every row.`);
    } else if ((c.text?.avg_len ?? 0) <= 60) {
      const lead = top.slice(0, 3).map((v) => `${v.value} (${Math.round((v.n / Math.max(1, present)) * 100)}%)`);
      out.push(`${c.name} has ${plural(c.distinct ?? 0, "value")}; most common: ${lead.join(", ")}.`);
    } else {
      out.push(`${c.name} is free text of about ${n(Math.round(c.text?.avg_len ?? 0))} characters.`);
    }
  }
  const sparse = cols.filter((c) => c.fill !== null && c.fill < 0.6).slice(0, 2);
  for (const c of sparse) out.push(`${c.name} is empty in ${Math.round((1 - (c.fill ?? 0)) * 100)}% of rows.`);
  return out.slice(0, 6);
}

function format(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "number") return n(value);
  if (typeof value === "string") return value.length > 40 ? `${value.slice(0, 40)}...` : value;
  if (typeof value === "object") return "{...}";
  return String(value);
}

export interface Starter {
  question: string;
  /** Fully substituted, runnable as it stands. */
  statement: string;
  spec: ViewSpec;
}

const QUESTION: Record<LensId, (f: string[], c: string) => string> = {
  spine: (_f, c) => `How much arrives in ${c} over time?`,
  timeseries: (f) => (f.length ? `How has ${lower(f[0] as string)} changed over time?` : "How has the volume changed over time?"),
  small_multiples: (f) => (f.length > 2 ? `How do ${lower(f[0] as string)} and its ${f.length - 1} siblings compare over time?` : f.length ? `How does ${lower(f[f.length - 1] as string)} differ per ${lower(f[0] as string)}?` : "How do the sibling channels compare?"),
  calendar_heat: (f) => (f.length ? `What was ${lower(f[0] as string)} on each day?` : "Which days were busiest?"),
  weekday_hour: (_f, c) => `When in the week is ${c} busiest?`,
  interval_clock: () => "At what time of day do these start and end?",
  gantt: () => "What overlapped what recently?",
  sawtooth: (f) => `How fast does ${lower(f[0] ?? "the level")} climb before each reset?`,
  histogram: (f) => `How is ${lower(f[0] ?? "the value")} distributed?`,
  top_values: (f) => `Which ${lower(f[0] ?? "value")} is most common?`,
  composition: (f) => `What is ${lower(f[f.length - 1] ?? "the total")} made of, and how does the mix change?`,
  scatter: (f) => `Does ${lower(f[1] ?? "one")} move with ${lower(f[0] ?? "the other")}?`,
  strip: (f) => `Where do the values of ${lower(f[0] ?? "the score")} fall${f[1] ? `, by ${lower(f[1])}` : ""}?`,
  thread_tapestry: () => "Which threads were alive when?",
  speaker_share: () => "Who carries the conversation?",
  reply_latency: () => "How quickly do replies come?",
  status_matrix: (f) => `When did ${lower(f[0] ?? "status")} go wrong, and for which source?`,
  tags: (f) => `Which ${lower(f[0] ?? "tags")} recur most?`,
  json_tree: (f) => `What is inside ${lower(f[0] ?? "the JSON")}?`,
  track_map: () => "Where did the track go?",
  pin_map: () => "Where did these happen?",
  dwell_rank: () => "Where was the most time spent?",
  ego_graph: () => "Who is most active here, and how recently?",
  link_matrix: (f) => (f.length >= 2 ? `Which ${lower(f[0] as string)} goes with which ${lower(f[1] as string)}?` : "Who is in which thread?"),
  reader: () => "What are the latest records?",
  table: () => "What do the latest records look like?",
  stat: () => "What are the totals for this period?",
};

/** The question a candidate answers, in plain words. */
export function questionFor(candidate: LensCandidate, channel = "this channel"): string {
  return QUESTION[candidate.spec.lens](candidate.spec.provenance.fields ?? [], channel);
}

/**
 * Suggested questions for a channel, each with the statement that answers it and the view
 * spec that draws it. They come straight from the ranked lenses, so they need no model.
 */
export function startersForChannel(
  meta: ChannelMeta,
  profile?: ChannelProfile | null,
  opts: EngineOptions & { scope?: Scope; limit?: number } = {},
): Starter[] {
  const analysis = analyzeChannel(meta, profile, null, opts);
  const out: Starter[] = [];
  const seen = new Set<string>();
  for (const c of analysis.candidates) {
    if (c.spec.lens === "table" || c.spec.lens === "spine" || c.score <= 0) continue;
    const question = questionFor(c, meta.name);
    if (seen.has(question)) continue;
    seen.add(question);
    try {
      out.push({ question, statement: compileView(c.spec, opts.scope ?? {}).sql, spec: c.spec });
    } catch {
      continue;
    }
    if (out.length >= (opts.limit ?? 5)) break;
  }
  return out;
}
