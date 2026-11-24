import { findLevels } from "../levels";
import { bucketExpr } from "../sql";
import { type LensDef, SCOPE, channelSpec, col, costOf, distinctOf, label, lower, rolesOf } from "./common";

export const sawtooth: LensDef = {
  id: "sawtooth",
  group: "time",
  needs: ["a level that climbs and resets (a percent used, a balance)", "optionally a group key and a reset time"],
  summary: "A level against time, stepped, one line per group, with resets as vertical drops and the next reset marked.",
  channels: { x: "time", y: "level", series: "optional group key", marker: "optional time still to come (the next reset)" },
  affinity: { gauge: 1, measurement: 0.5 },
  fit(profile, roles) {
    const best = findLevels(profile, roles)[0];
    if (!best) return { fitness: 0, why: "No level that resets" };
    return { fitness: best.strength, why: best.tested ? `${best.role.field} climbs and drops back ${best.drops} times in the sample` : `${best.role.field} reads as a level that resets` };
  },
  build(ctx) {
    const levels = findLevels(ctx.profile, ctx.roles);
    // used and left are mirror images: keep the first of a part-of-whole pair.
    const twins = new Set<string>();
    const out = [];
    for (const lv of levels) {
      if (twins.has(lv.role.field) || out.length >= 2) continue;
      for (const w of ctx.wholes) if (w.parts.includes(lv.role.field)) for (const p of w.parts) twins.add(p);
      const y = col(lv.role.field);
      const deadline = rolesOf(ctx.roles, "time.deadline").sort((a, b) => Number(/reset/.test(b.field)) - Number(/reset/.test(a.field)))[0];
      const series = lv.group ? `, coalesce(${col(lv.group.field)}, '(none)') AS series` : "";
      const big = ctx.rows > 5000;
      const marker = deadline && !big ? `, ${col(deadline.field)} AS marker` : "";
      const sql = big
        ? `SELECT ${bucketExpr()} AS t${series}, max(${y})::float8 AS ${y} FROM ${ctx.view} WHERE ${SCOPE} AND ${y} IS NOT NULL GROUP BY 1${lv.group ? ", 2" : ""} ORDER BY 1`
        : `SELECT happened_at AS t${series}, (${y})::float8 AS ${y}${marker} FROM ${ctx.view} WHERE ${SCOPE} AND ${y} IS NOT NULL ORDER BY happened_at, id`;
      const k = lv.group ? (distinctOf(ctx.profile, lv.group) ?? 4) : 1;
      out.push({
        spec: channelSpec(ctx, "sawtooth", {
          title: lv.group ? `${label(lv.role.field)} per ${lower(lv.group.field)}` : `${label(lv.role.field)} over time`,
          why: lv.tested
            ? `${label(lv.role.field)} climbs and falls back${lv.group ? ` within each ${lower(lv.group.field)}` : ""} (${lv.drops} resets in the sample)${deadline ? `; ${deadline.field} marks the next one` : ""}`
            : `${label(lv.role.field)} reads as a level that resets${deadline ? `, with ${deadline.field} as the next reset` : ""}`,
          sql,
          row_cap: 5000,
          encode: { x: "t", y: lv.role.field, ...(lv.group ? { series: "series" } : {}), ...(marker ? { marker: "marker" } : {}) },
          options: {
            step: true,
            ...(lv.role.role === "percent" ? { domain: [0, 100] } : {}),
            ...(big ? { bucket_budget: Math.max(100, Math.floor(4000 / k)) } : {}),
            ...(lv.role.unit ? { units: { [lv.role.field]: lv.role.unit } } : {}),
          },
          fields: [lv.role.field, lv.group?.field, deadline?.field].filter((x): x is string => !!x),
        }),
        fitness: lv.strength,
        information: lv.tested ? 0.95 : 0.8,
        cost: costOf(ctx.rows, Math.min(5000, ctx.rows)),
      });
    }
    return out;
  },
};
