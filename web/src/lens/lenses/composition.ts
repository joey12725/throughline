import { bucketExpr } from "../sql";
import { type LensDef, SCOPE, channelSpec, col, costOf, label, unitOptions } from "./common";
import { partsOfWhole } from "../roles";

export const composition: LensDef = {
  id: "composition",
  group: "distribution",
  needs: ["sibling quantities in one unit that add up to a whole"],
  summary: "Stacked parts of a whole per time bucket.",
  channels: { x: "bucket start (timestamp)", parts: "the part columns, stacked in this order", whole: "optional column holding the total" },
  affinity: { interval: 0.9, measurement: 0.9, gauge: 0.8 },
  fit(profile, roles) {
    const w = partsOfWhole(profile, roles)[0];
    if (!w) return { fitness: 0, why: "No parts that add up to a whole" };
    return { fitness: w.tested ? 1 : 0.6, why: w.evidence[0] ?? "Parts of a whole" };
  },
  build(ctx) {
    return ctx.wholes.slice(0, 2).map((w) => {
      const parts = w.parts.map((p) => `avg(${col(p)})::float8 AS ${col(p)}`).join(", ");
      const whole = typeof w.whole === "string" ? `, avg(${col(w.whole)})::float8 AS ${col(w.whole)}` : "";
      const name = typeof w.whole === "string" ? label(w.whole) : `${w.whole}${w.unit === "%" ? "%" : ""}`;
      const trivial = w.parts.length === 2 && typeof w.whole === "number";
      return {
        spec: channelSpec(ctx, "composition", {
          title: `What ${name.toLowerCase()} is made of`,
          why: `${w.evidence[0] ?? `${w.parts.join(" + ")} make up ${name}`}; stacking shows the mix changing`,
          sql: `SELECT ${bucketExpr()} AS bucket, ${parts}${whole} FROM ${ctx.view} WHERE ${SCOPE} GROUP BY 1 ORDER BY 1`,
          row_cap: 2000,
          encode: { x: "bucket", parts: w.parts, ...(typeof w.whole === "string" ? { whole: w.whole } : {}) },
          options: {
            ...(typeof w.whole === "number" ? { total: w.whole } : {}),
            ...unitOptions(ctx.roles, w.parts),
            tested: w.tested,
          },
          fields: [...w.parts, ...(typeof w.whole === "string" ? [w.whole] : [])],
        }),
        fitness: w.tested ? 1 : 0.6,
        information: trivial ? 0.3 : w.tested ? 0.92 : 0.75,
        cost: costOf(ctx.rows, 600),
      };
    });
  },
};
