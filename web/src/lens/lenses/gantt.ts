import type { LensVariant } from "../types";
import { type LensDef, SCOPE, channelSpec, col, colourField, costOf, label, resultSpec, rolesOf } from "./common";
import { fieldIntervals } from "./interval_clock";

export const gantt: LensDef = {
  id: "gantt",
  group: "time",
  needs: ["a start and an end per record", "a title or a category to label the bars"],
  summary: "Labelled bars on a time axis, in lanes; for intervals that last hours or days.",
  channels: { start: "interval start", end: "interval end", label: "bar label", lane: "optional lane category" },
  affinity: { interval: 0.8, feed_item: 0.55, gauge: 0.3, document: 0.3 },
  fit(profile, roles) {
    const share = profile.rows > 0 ? profile.envelope.with_ended_at / profile.rows : 0;
    if (share >= 0.3 || fieldIntervals(roles).length > 0) return { fitness: 0.9, why: "Records are intervals" };
    return { fitness: 0, why: "No start and end" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const env = ctx.profile.envelope;
    const share = ctx.rows > 0 ? env.with_ended_at / ctx.rows : 0;
    const title = rolesOf(ctx.roles, "title")[0];
    const lane = colourField(ctx.profile, ctx.roles, 12);
    const labelSql = title ? `coalesce(${col(title.field)}, left(content, 80))` : "left(content, 80)";
    const laneSql = lane ? `, (${col(lane.field)})::text AS lane` : "";
    const encodeExtra: Record<string, string> = lane ? { lane: "lane" } : {};
    const avg = env.avg_duration_s;
    if (share >= 0.3) {
      const constant = env.min_duration_s !== null && env.max_duration_s !== null && Math.abs(env.max_duration_s - env.min_duration_s) <= 2;
      let information = avg === null ? 0.45 : avg >= 6 * 3600 ? 0.75 : avg >= 900 ? 0.5 : 0.15;
      if (constant) information = 0.1;
      if (!title && !lane) information *= 0.6;
      out.push({
        spec: channelSpec(ctx, "gantt", {
          title: "Latest intervals",
          why: `Records span time${title ? ` and carry a ${title.field}` : ""}; bars show what overlapped what`,
          sql:
            `SELECT id, happened_at, ended_at, ${labelSql} AS label${laneSql} FROM ${ctx.view} ` +
            `WHERE ${SCOPE} AND ended_at IS NOT NULL ORDER BY happened_at DESC, id DESC LIMIT 400`,
          row_cap: 400,
          encode: { start: "happened_at", end: "ended_at", label: "label", ...encodeExtra },
          drill: { to: "record", key: "id" },
          fields: [title?.field, lane?.field].filter((x): x is string => !!x),
        }),
        information,
        cost: costOf(ctx.rows, 400),
      });
    }
    const pair = fieldIntervals(ctx.roles)[0];
    if (pair) {
      const s = col(pair.start.field);
      const e = col(pair.end.field);
      out.push({
        spec: channelSpec(ctx, "gantt", {
          title: `${label(pair.start.field)} to ${pair.end.field}`,
          why: `${pair.start.field} and ${pair.end.field} bound each record${title ? `; ${title.field} names the bar` : ""}`,
          sql:
            `SELECT id, ${s}, ${e}, ${labelSql} AS label${laneSql} FROM ${ctx.view} ` +
            `WHERE ${s} >= :from AND ${s} < :to AND ${e} IS NOT NULL ORDER BY ${s} DESC, id DESC LIMIT 400`,
          row_cap: 400,
          encode: { start: pair.start.field, end: pair.end.field, label: "label", ...encodeExtra },
          drill: { to: "record", key: "id" },
          fields: [pair.start.field, pair.end.field, title?.field, lane?.field].filter((x): x is string => !!x),
        }),
        information: (title ? 0.65 : 0.4) * pair.start.confidence,
        cost: costOf(ctx.rows, 400),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const stamps = ctx.profile.columns.filter((c) => c.kind === "timestamp");
    const start = stamps.find((c) => ctx.roles[c.name]?.role === "time.start");
    const end = stamps.find((c) => ctx.roles[c.name]?.role === "time.end");
    const text = ctx.profile.columns.find((c) => c.kind === "text" && ["title", "body", "category", "actor.label", "geo.label"].includes(ctx.roles[c.name]?.role ?? ""));
    if (!start || !end || !text || ctx.rows < 2 || ctx.rows > 500) return [];
    return [
      {
        spec: resultSpec("gantt", "as_is", {
          title: `${start.name} to ${end.name}, labelled by ${text.name}`,
          why: "The result has a start, an end and a label: bars on a time axis",
          sql: ctx.statement,
          row_cap: 500,
          encode: { start: start.name, end: end.name, label: text.name },
          fields: [start.name, end.name, text.name],
        }),
        information: 0.7,
        cost: 0,
      },
    ];
  },
};
