import type { LensVariant, Role } from "../types";
import { type LensDef, SCOPE, channelSpec, col, colourField, costOf, fillOf, label, liveRoles, lower, resultSpec } from "./common";

/** Start/end field pairs, most confident first. */
export function fieldIntervals(roles: Record<string, Role>): { start: Role; end: Role }[] {
  return liveRoles(roles)
    .filter((r) => r.role === "time.start" && r.pair !== undefined && roles[r.pair]?.role === "time.end" && !roles[r.pair]?.hidden)
    .sort((a, b) => b.confidence - a.confidence)
    .map((start) => ({ start, end: roles[start.pair as string] as Role }));
}

export const intervalClock: LensDef = {
  id: "interval_clock",
  group: "time",
  needs: ["a start and an end per record (the envelope's, or a start/end field pair)"],
  summary: "Date against time of day; each record a bar from its start to its end.",
  channels: { start: "interval start (timestamp)", end: "interval end (timestamp)", color: "optional category" },
  affinity: { interval: 1, measurement: 0.5, geo_track: 0.5, feed_item: 0.45, transcript: 0.3, gauge: 0.3 },
  fit(profile, roles) {
    const share = profile.rows > 0 ? profile.envelope.with_ended_at / profile.rows : 0;
    if (share >= 0.3) return { fitness: Math.min(1, share + 0.1), why: `${Math.round(share * 100)}% of records have an end time` };
    if (fieldIntervals(roles).length > 0) return { fitness: 0.8, why: "A start/end field pair" };
    return { fitness: 0, why: "No start and end" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const env = ctx.profile.envelope;
    const share = ctx.rows > 0 ? env.with_ended_at / ctx.rows : 0;
    const colour = colourField(ctx.profile, ctx.roles, 8);
    const colourSql = colour ? `, (${col(colour.field)})::text AS ${col(colour.field)}` : "";
    if (share >= 0.3) {
      const avg = env.avg_duration_s;
      const constant = env.min_duration_s !== null && env.max_duration_s !== null && Math.abs(env.max_duration_s - env.min_duration_s) <= 2;
      // The picture works for things that last minutes to hours: nights, meetings, stays.
      let information = 0.9;
      if (constant) information = 0.12;
      else if (avg !== null && avg < 120) information = 0.2;
      else if (avg !== null && avg > 3 * 86400) information = 0.35;
      out.push({
        spec: channelSpec(ctx, "interval_clock", {
          title: "Start to end, by time of day",
          why:
            avg !== null && !constant
              ? `Every record has a start and an end (about ${humanDuration(avg)}); time of day is the pattern`
              : "Every record has a start and an end; time of day is the pattern",
          sql: `SELECT id, happened_at, ended_at${colourSql} FROM ${ctx.view} WHERE ${SCOPE} AND ended_at IS NOT NULL ORDER BY happened_at`,
          row_cap: 2000,
          encode: { start: "happened_at", end: "ended_at", ...(colour ? { color: colour.field } : {}) },
          drill: { to: "record", key: "id" },
          fields: colour ? [colour.field] : [],
        }),
        information,
        cost: costOf(ctx.rows, Math.min(2000, ctx.rows)) + (ctx.rows > 2000 ? 0.15 : 0),
      });
    }
    for (const pair of fieldIntervals(ctx.roles).slice(0, 2)) {
      const s = col(pair.start.field);
      const e = col(pair.end.field);
      out.push({
        spec: channelSpec(ctx, "interval_clock", {
          title: `${label(pair.start.field)} to ${lower(pair.end.field)}, by time of day`,
          why: `${pair.start.field} and ${pair.end.field} are a start/end pair; time of day is the pattern`,
          sql: `SELECT id, ${s}, ${e}${colourSql} FROM ${ctx.view} WHERE ${s} >= :from AND ${s} < :to AND ${e} IS NOT NULL ORDER BY ${s}`,
          row_cap: 2000,
          encode: { start: pair.start.field, end: pair.end.field, ...(colour ? { color: colour.field } : {}) },
          drill: { to: "record", key: "id" },
          fields: [pair.start.field, pair.end.field, ...(colour ? [colour.field] : [])],
        }),
        information: 0.75 * pair.start.confidence * Math.max(0.3, fillOf(ctx.profile, pair.start.field)),
        cost: costOf(ctx.rows, Math.min(2000, ctx.rows)),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const stamps = ctx.profile.columns.filter((c) => c.kind === "timestamp");
    const start = stamps.find((c) => ctx.roles[c.name]?.role === "time.start");
    const end = stamps.find((c) => ctx.roles[c.name]?.role === "time.end");
    if (!start || !end || ctx.rows < 2 || ctx.rows > 5000) return [];
    return [
      {
        spec: resultSpec("interval_clock", "as_is", {
          title: `${start.name} to ${end.name}, by time of day`,
          why: "The result has a start and an end column; time of day is the pattern",
          sql: ctx.statement,
          row_cap: 5000,
          encode: { start: start.name, end: end.name },
          fields: [start.name, end.name],
        }),
        information: 0.85,
        cost: 0,
      },
    ];
  },
};

export function humanDuration(seconds: number): string {
  const s = Math.abs(seconds);
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 2 * 86400) return `${Math.round((s / 3600) * 10) / 10} h`;
  return `${Math.round(s / 86400)} days`;
}
