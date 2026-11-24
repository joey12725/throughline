import { quoteIdent } from "../sql";
import { type LensDef, SCOPE, channelSpec, costOf, resultMeasures, resultSpec, resultTime } from "./common";

export const weekdayHour: LensDef = {
  id: "weekday_hour",
  group: "time",
  needs: ["record times that carry a time of day"],
  summary: "A 7 by 24 grid of record counts: weekday against hour of day, in the viewer's zone.",
  channels: { weekday: "ISO weekday 1 (Monday) to 7", hour: "hour of day 0 to 23", value: "records in the cell" },
  affinity: { conversation: 0.9, transcript: 0.7, interval: 0.6, geo_track: 0.6, document: 0.6, feed_item: 0.6, gauge: 0.4, measurement: 0.2, registry: 0.15, snapshot: 0.1 },
  fit(profile) {
    if (profile.source === "declared") return { fitness: 0.5, why: "A weekly rhythm appears once records arrive" };
    if (profile.rows < 50) return { fitness: 0, why: "Too few records for a weekly rhythm" };
    const hours = profile.envelope.hours_used;
    if (hours !== null && hours <= 2) return { fitness: 0.1, why: "Records arrive at a fixed time of day" };
    return { fitness: 1, why: "Record times spread over the day" };
  },
  build(ctx) {
    const hours = ctx.profile.envelope.hours_used;
    const cad = ctx.profile.cadence;
    let information = hours === null ? 0.5 : hours >= 8 ? 0.85 : hours >= 4 ? 0.5 : 0.05;
    if (!ctx.predicted && cad.per_active_day_median <= 1.5 && cad.class === "daily") information *= 0.4;
    const sql =
      `SELECT extract(isodow FROM happened_at AT TIME ZONE :tz)::int AS weekday, ` +
      `extract(hour FROM happened_at AT TIME ZONE :tz)::int AS hour, count(*) AS n ` +
      `FROM ${ctx.view} WHERE ${SCOPE} GROUP BY 1, 2 ORDER BY 1, 2`;
    return [
      {
        spec: channelSpec(ctx, "weekday_hour", {
          title: "When in the week",
          why: hours === null ? "Record times carry a time of day; the grid shows the weekly rhythm" : `Records fall in ${hours} different hours of the day; the grid shows the weekly rhythm`,
          sql,
          row_cap: 200,
          encode: { weekday: "weekday", hour: "hour", value: "n" },
        }),
        information,
        cost: costOf(ctx.rows, 168),
      },
    ];
  },
  buildResult(ctx) {
    const ints = ctx.profile.columns.filter((c) => c.kind === "integer" && c.numeric);
    const wd = ints.find((c) => /^(weekday|dow|isodow|day_of_week)$/.test(c.name) && (c.numeric?.max ?? 9) <= 7);
    const hr = ints.find((c) => /^(hour|hour_of_day|hod)$/.test(c.name) && (c.numeric?.max ?? 99) <= 23);
    const m = resultMeasures(ctx).find((c) => c !== wd && c !== hr);
    if (wd && hr && m) {
      return [
        {
          spec: resultSpec("weekday_hour", "as_is", {
            title: `${m.name} by weekday and hour`,
            why: "The result has a weekday and an hour column: a week grid",
            sql: ctx.statement,
            row_cap: 10000,
            encode: { weekday: wd.name, hour: hr.name, value: m.name },
            options: { weekday_base: (wd.numeric?.min ?? 1) === 0 ? 0 : 1 },
          }),
          information: 0.95,
          cost: 0,
        },
      ];
    }
    const t = resultTime(ctx);
    if (!t || t.kind !== "timestamp" || !ctx.wrapped || ctx.rows < 100) return [];
    const c = `(${quoteIdent(t.name)})::timestamptz`;
    return [
      {
        spec: resultSpec("weekday_hour", "wrapped", {
          title: `When in the week (${t.name})`,
          why: `${ctx.rows} rows with a timestamp; the grid counts them by weekday and hour`,
          sql:
            `SELECT extract(isodow FROM ${c} AT TIME ZONE :tz)::int AS weekday, extract(hour FROM ${c} AT TIME ZONE :tz)::int AS hour, ` +
            `count(*) AS n FROM ${ctx.wrapped} GROUP BY 1, 2 ORDER BY 1, 2`,
          row_cap: 200,
          encode: { weekday: "weekday", hour: "hour", value: "n" },
        }),
        information: 0.4,
        cost: 0.1,
      },
    ];
  },
};
