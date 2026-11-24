import { localDayExpr, quoteIdent } from "../sql";
import type { LensVariant } from "../types";
import { type LensDef, SCOPE, channelSpec, col, costOf, isUnique, label, lower, rankedMeasures, resultMeasures, resultSpec, resultTime, unitOptions } from "./common";

export const calendarHeat: LensDef = {
  id: "calendar_heat",
  group: "time",
  needs: ["the record time", "at least a few weeks of data"],
  summary: "One cell per local day, coloured by record count or by a daily value.",
  channels: { date: "local calendar day (date)", value: "count or value of the day" },
  affinity: { conversation: 0.8, measurement: 0.6, document: 0.6, feed_item: 0.6, interval: 0.6, snapshot: 0.5, transcript: 0.5, geo_track: 0.4, gauge: 0.3, registry: 0.2 },
  fit(profile) {
    if (profile.source === "declared") return { fitness: 0.5, why: "Days will fill in as records arrive" };
    const span = profile.cadence.span_days;
    return span >= 28 ? { fitness: Math.min(1, span / 90 + 0.4), why: `${span} days of records` } : { fitness: 0, why: "Less than four weeks of data" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const cad = ctx.profile.cadence;
    // A channel with one record a day has nothing to say in a count; a busy one does.
    const varies = ctx.predicted ? 0.5 : cad.per_active_day_max > Math.max(2, cad.per_active_day_median) ? 0.85 : cad.coverage < 0.6 ? 0.5 : 0.15;
    out.push({
      spec: channelSpec(ctx, "calendar_heat", {
        title: "Records per day",
        why: ctx.predicted
          ? "A calendar shows which days have records"
          : `Records on ${cad.active_days} of ${cad.span_days} days; a calendar shows the busy days and the gaps`,
        sql: `SELECT ${localDayExpr()} AS day, count(*) AS n FROM ${ctx.view} WHERE ${SCOPE} GROUP BY 1 ORDER BY 1`,
        row_cap: 5000,
        encode: { date: "day", value: "n" },
      }),
      information: varies,
      cost: costOf(ctx.rows, Math.min(5000, cad.active_days)),
    });
    const m = rankedMeasures(ctx.profile, ctx.roles)[0];
    if (m && (ctx.predicted || cad.class === "daily" || cad.class === "regular")) {
      out.push({
        spec: channelSpec(ctx, "calendar_heat", {
          title: `${label(m.field)} by day`,
          why: `About one record a day; the calendar shows ${lower(m.field)} day by day, with weekdays aligned`,
          sql: `SELECT ${localDayExpr()} AS day, avg(${col(m.field)})::float8 AS value FROM ${ctx.view} WHERE ${SCOPE} AND ${col(m.field)} IS NOT NULL GROUP BY 1 ORDER BY 1`,
          row_cap: 5000,
          encode: { date: "day", value: "value" },
          options: { ...unitOptions(ctx.roles, [m.field]), label: label(m.field) },
          fields: [m.field],
        }),
        information: ctx.predicted ? 0.4 : 0.6,
        cost: costOf(ctx.rows, Math.min(5000, cad.active_days)),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const t = resultTime(ctx);
    const m = resultMeasures(ctx)[0];
    if (!t || !m || !t.time || !isUnique(ctx, t.name)) return [];
    const midnight = t.kind === "date" || ctx.profile.sample.every((r) => typeof r[t.name] !== "string" || /T00:00:00/.test(r[t.name] as string));
    const span = (Date.parse(t.time.max ?? "") - Date.parse(t.time.min ?? "")) / 86400000;
    if (!midnight || !(span >= 28) || ctx.rows < 14) return [];
    void quoteIdent;
    return [
      {
        spec: resultSpec("calendar_heat", "as_is", {
          title: `${label(m.name)} by day`,
          why: `One row per day over ${Math.round(span)} days; a calendar aligns the weekdays`,
          sql: ctx.statement,
          row_cap: 10000,
          encode: { date: t.name, value: m.name },
          fields: [t.name, m.name],
        }),
        information: 0.6,
        cost: 0,
      },
    ];
  },
};
