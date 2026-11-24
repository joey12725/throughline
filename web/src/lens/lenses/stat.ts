import type { LensVariant } from "../types";
import { type LensDef, SCOPE, channelSpec, col, costOf, label, rankedMeasures, resultSpec, unitOptions } from "./common";

export const stat: LensDef = {
  id: "stat",
  group: "reading",
  needs: ["records; quantities make it richer"],
  summary: "A few large numbers: totals over the scope, or the latest values.",
  channels: { values: "the columns to show as numbers, in order", time: "optional time the values are from" },
  affinity: { registry: 0.7, gauge: 0.65, measurement: 0.5, snapshot: 0.4 },
  fit: () => ({ fitness: 1, why: "Totals always exist" }),
  build(ctx) {
    const out: LensVariant[] = [];
    const measures = rankedMeasures(ctx.profile, ctx.roles).slice(0, 4);
    const avgs = measures.map((m) => `, avg(${col(m.field)})::float8 AS ${col(m.field)}`).join("");
    out.push({
      spec: channelSpec(ctx, "stat", {
        title: "In this period",
        why: measures.length
          ? `Record and thread counts with the average ${measures.map((m) => label(m.field).toLowerCase()).slice(0, 2).join(" and ")}`
          : "How many records and threads fall in the period, and when the first and last happened",
        sql:
          `SELECT count(*) AS records, count(DISTINCT thread_id) AS threads, min(happened_at) AS first_at, max(happened_at) AS last_at${avgs} ` +
          `FROM ${ctx.view} WHERE ${SCOPE}`,
        row_cap: 1,
        encode: { values: ["records", "threads", ...measures.map((m) => m.field)] },
        options: { agg: "avg", ...unitOptions(ctx.roles, measures.map((m) => m.field)) },
        fields: measures.map((m) => m.field),
      }),
      information: 0.3,
      cost: costOf(ctx.rows, 1),
    });
    if (measures.length > 0) {
      out.push({
        spec: channelSpec(ctx, "stat", {
          title: "Latest values",
          why: `The most recent record's ${measures.map((m) => label(m.field).toLowerCase()).slice(0, 3).join(", ")}`,
          sql:
            `SELECT happened_at${measures.map((m) => `, (${col(m.field)})::float8 AS ${col(m.field)}`).join("")} ` +
            `FROM ${ctx.view} WHERE ${SCOPE} ORDER BY happened_at DESC, id DESC LIMIT 1`,
          row_cap: 1,
          encode: { values: measures.map((m) => m.field), time: "happened_at" },
          options: unitOptions(ctx.roles, measures.map((m) => m.field)),
          fields: measures.map((m) => m.field),
        }),
        information: 0.4,
        cost: 0.02,
      });
    }
    return out;
  },
  buildResult(ctx) {
    if (ctx.rows !== 1) return [];
    const nums = ctx.profile.columns.filter((c) => (c.kind === "integer" || c.kind === "number") && c.present === 1);
    if (nums.length === 0) return [];
    return [
      {
        spec: resultSpec("stat", "as_is", {
          title: nums.length === 1 ? label((nums[0] as { name: string }).name) : "Totals",
          why: `One row with ${nums.length === 1 ? "one number" : `${nums.length} numbers`}: shown large`,
          sql: ctx.statement,
          row_cap: 1,
          encode: { values: nums.slice(0, 8).map((c) => c.name) },
        }),
        information: 1,
        cost: 0,
      },
    ];
  },
};
