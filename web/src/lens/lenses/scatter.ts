import { num } from "../profile";
import { quoteIdent, sampleModulo } from "../sql";
import type { ChannelProfile, LensVariant, Role, RoleMap } from "../types";
import {
  type LensDef,
  SCOPE,
  channelSpec,
  col,
  colourField,
  costOf,
  int,
  isUnique,
  label,
  lower,
  measureInterest,
  rankedMeasures,
  resultMeasures,
  resultSpec,
  resultTime,
  unitOptions,
} from "./common";
import { partsOfWhole } from "../roles";

function correlation(xs: number[], ys: number[]): number | null {
  const n = xs.length;
  if (n < 12) return null;
  let sx = 0, sy = 0;
  for (let i = 0; i < n; i++) {
    sx += xs[i] as number;
    sy += ys[i] as number;
  }
  const mx = sx / n, my = sy / n;
  let sxx = 0, syy = 0, sxy = 0;
  for (let i = 0; i < n; i++) {
    const dx = (xs[i] as number) - mx, dy = (ys[i] as number) - my;
    sxx += dx * dx;
    syy += dy * dy;
    sxy += dx * dy;
  }
  if (sxx === 0 || syy === 0) return null;
  return sxy / Math.sqrt(sxx * syy);
}

export interface Pair {
  x: Role;
  y: Role;
  r: number | null;
  score: number;
}

/** Pairs of quantities worth plotting against each other, best first. */
export function findPairs(profile: ChannelProfile, roles: RoleMap, limit = 3): Pair[] {
  const measures = rankedMeasures(profile, roles).slice(0, 9);
  const wholes = partsOfWhole(profile, roles);
  const sameWhole = (a: string, b: string) => wholes.some((w) => {
    const all = [...w.parts, ...(typeof w.whole === "string" ? [w.whole] : [])];
    return all.includes(a) && all.includes(b);
  });
  const pairs: Pair[] = [];
  for (let i = 0; i < measures.length; i++) {
    for (let j = i + 1; j < measures.length; j++) {
      const a = measures[i] as Role;
      const b = measures[j] as Role;
      if (sameWhole(a.field, b.field)) continue;
      const pts = profile.sample
        .map((row) => [num(row[a.field]), num(row[b.field])] as const)
        .filter((p): p is readonly [number, number] => p[0] !== null && p[1] !== null);
      const r = correlation(pts.map((p) => p[0]), pts.map((p) => p[1]));
      let shape: number;
      if (r === null) {
        // Nothing sampled: prefer pairs that differ in kind, and probabilities against each other.
        shape = a.role === "probability" && b.role === "probability" ? 0.7 : a.role !== b.role ? 0.55 : a.family && a.family === b.family ? 0.2 : 0.4;
        if (pts.length >= 12) shape = 0;
      } else {
        const abs = Math.abs(r);
        shape = abs > 0.98 ? 0 : abs >= 0.25 ? 0.6 + 0.4 * abs : 0.35;
      }
      const score = shape * Math.sqrt(measureInterest(profile, a) * measureInterest(profile, b));
      if (score > 0) pairs.push({ x: b, y: a, r, score });
    }
  }
  // Keep the list varied: a field appears in at most two pairs.
  const used = new Map<string, number>();
  const out: Pair[] = [];
  for (const p of pairs.sort((p1, p2) => p2.score - p1.score)) {
    if ((used.get(p.x.field) ?? 0) >= 2 || (used.get(p.y.field) ?? 0) >= 2) continue;
    used.set(p.x.field, (used.get(p.x.field) ?? 0) + 1);
    used.set(p.y.field, (used.get(p.y.field) ?? 0) + 1);
    out.push(p);
    if (out.length >= limit) break;
  }
  return out;
}

export const scatter: LensDef = {
  id: "scatter",
  group: "distribution",
  needs: ["two quantities on the same records"],
  summary: "One dot per record: one quantity against another.",
  channels: { x: "horizontal quantity", y: "vertical quantity", color: "optional category" },
  affinity: { measurement: 0.9, interval: 0.7, feed_item: 0.7, gauge: 0.7 },
  fit(profile, roles) {
    const pairs = findPairs(profile, roles, 1);
    return pairs.length > 0 ? { fitness: 1, why: "Two quantities on the same records" } : { fitness: 0, why: "Fewer than two independent quantities" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const k = sampleModulo(ctx.rows, 2000);
    for (const p of findPairs(ctx.profile, ctx.roles, 2)) {
      const colour = colourField(ctx.profile, ctx.roles, 6, [p.x.field, p.y.field]);
      const x = col(p.x.field);
      const y = col(p.y.field);
      const tie = p.r === null ? "" : Math.abs(p.r) >= 0.25 ? ` (they move ${p.r > 0 ? "together" : "against each other"}, r = ${p.r.toFixed(2)})` : " (no clear relation in the sample)";
      out.push({
        spec: channelSpec(ctx, "scatter", {
          title: `${label(p.y.field)} against ${lower(p.x.field)}`,
          why: `Both are on the same records${tie}; one dot per record shows the shape`,
          sql:
            `SELECT id, (${x})::float8 AS ${x}, (${y})::float8 AS ${y}${colour ? `, (${col(colour.field)})::text AS ${col(colour.field)}` : ""} ` +
            `FROM ${ctx.view} WHERE ${SCOPE} AND ${x} IS NOT NULL AND ${y} IS NOT NULL AND id % :k = 0 ORDER BY id`,
          row_cap: 2500,
          encode: { x: p.x.field, y: p.y.field, ...(colour ? { color: colour.field } : {}) },
          params: { k: int(k) },
          options: unitOptions(ctx.roles, [p.x.field, p.y.field]),
          sampled: k > 1,
          drill: { to: "record", key: "id" },
          fields: [p.x.field, p.y.field, ...(colour ? [colour.field] : [])],
        }),
        information: Math.min(1, p.score * 1.25),
        cost: costOf(ctx.rows, Math.min(2000, ctx.rows)),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const measures = resultMeasures(ctx);
    if (measures.length < 2 || ctx.rows < 8 || ctx.rows > 5000) return [];
    const a = measures[0] as { name: string };
    const b = measures[1] as { name: string };
    const t = resultTime(ctx);
    const series = t !== undefined && isUnique(ctx, t.name);
    void quoteIdent;
    return [
      {
        spec: resultSpec("scatter", "as_is", {
          title: `${label(b.name)} against ${a.name}`,
          why: `Two numeric columns over ${ctx.rows} rows: one dot per row`,
          sql: ctx.statement,
          row_cap: 5000,
          encode: { x: a.name, y: b.name },
          fields: [a.name, b.name],
        }),
        information: series ? 0.45 : 0.75,
        cost: 0,
      },
    ];
  },
};
