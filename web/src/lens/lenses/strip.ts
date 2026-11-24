import { sampleModulo } from "../sql";
import type { LensVariant } from "../types";
import { type LensDef, SCOPE, channelSpec, col, colourField, costOf, fieldOf, int, label, lower, measureInterest, resultSpec, rolesOf } from "./common";

export const strip: LensDef = {
  id: "strip",
  group: "distribution",
  needs: ["a probability, or another quantity with fixed bounds"],
  summary: "Every record as a tick on one bounded axis, coloured by a category: where the decisions fall.",
  channels: { x: "the bounded quantity", color: "optional category" },
  affinity: { feed_item: 0.95, measurement: 0.7, gauge: 0.5, interval: 0.5 },
  fit(_profile, roles) {
    if (rolesOf(roles, "probability").length > 0) return { fitness: 1, why: "A probability between 0 and 1" };
    if (rolesOf(roles, "percent", "score").length > 0) return { fitness: 0.5, why: "A quantity between 0 and 100" };
    return { fitness: 0, why: "No bounded quantity" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const k = sampleModulo(ctx.rows, 3000);
    const bounded = [...rolesOf(ctx.roles, "probability"), ...rolesOf(ctx.roles, "percent", "score")]
      .map((r) => ({ r, s: measureInterest(ctx.profile, r) * (r.role === "probability" ? 1.6 : 0.6) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, 2);
    for (const { r, s } of bounded) {
      const f = fieldOf(ctx.profile, r.field);
      const hi = r.role === "probability" ? 1 : 100;
      if (f?.numeric && f.observed >= 5 && (f.numeric.min < 0 || f.numeric.max > hi)) continue;
      const colour = colourField(ctx.profile, ctx.roles, 6, [r.field]);
      const c = col(r.field);
      out.push({
        spec: channelSpec(ctx, "strip", {
          title: colour ? `${label(r.field)} by ${lower(colour.field)}` : `${label(r.field)}, record by record`,
          why: `${label(r.field)} is a ${r.role} bounded at ${hi}${colour ? `; colouring by ${lower(colour.field)} shows where the line was drawn` : "; each record is one tick"}`,
          sql:
            `SELECT id, (${c})::float8 AS ${c}${colour ? `, (${col(colour.field)})::text AS ${col(colour.field)}` : ""} ` +
            `FROM ${ctx.view} WHERE ${SCOPE} AND ${c} IS NOT NULL AND id % :k = 0 ORDER BY id`,
          row_cap: 3500,
          encode: { x: r.field, ...(colour ? { color: colour.field } : {}) },
          params: { k: int(k) },
          options: { domain: [0, hi] },
          sampled: k > 1,
          drill: { to: "record", key: "id" },
          fields: [r.field, ...(colour ? [colour.field] : [])],
        }),
        fitness: r.role === "probability" ? 1 : 0.5,
        information: Math.min(1, s),
        cost: costOf(ctx.rows, Math.min(3000, ctx.rows)),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const p = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "probability" && c.numeric);
    if (!p || ctx.rows < 5 || ctx.rows > 5000) return [];
    return [
      {
        spec: resultSpec("strip", "as_is", {
          title: `${p.name}, row by row`,
          why: `${p.name} stays between 0 and 1: each row is one tick`,
          sql: ctx.statement,
          row_cap: 5000,
          encode: { x: p.name },
          options: { domain: [0, 1] },
          fields: [p.name],
        }),
        information: 0.7,
        cost: 0,
      },
    ];
  },
};
