import type { SqlFns } from "../sql";
import { quoteIdent } from "../sql";
import type { FieldProfile, LensVariant, ParamValue, Role } from "../types";
import {
  type LensDef,
  SCOPE,
  channelSpec,
  costOf,
  fieldOf,
  int,
  isUnique,
  label,
  measureInterest,
  number,
  rankedMeasures,
  resultMeasures,
  resultSpec,
  resultTime,
  unitOptions,
} from "./common";

export interface Bins {
  lo: number;
  hi: number;
  bins: number;
}

/** Bin edges for a numeric field: whole-number bins for small integer ranges, role domains for bounded roles. */
export function chooseBins(f: FieldProfile | undefined, role: Role | undefined): Bins | null {
  const s = f?.numeric;
  if (s && f.observed >= 5) {
    if (!(s.max > s.min)) return null;
    if (role?.role === "probability" && s.min >= 0 && s.max <= 1) return { lo: 0, hi: 1, bins: 20 };
    if ((role?.role === "percent" || role?.role === "score") && s.min >= 0 && s.max <= 100) return { lo: 0, hi: 100, bins: 20 };
    if (s.integral && s.max - s.min < 40) return { lo: s.min, hi: s.max + 1, bins: s.max - s.min + 1 };
    return { lo: s.min, hi: s.max, bins: 30 };
  }
  if (role?.role === "probability") return { lo: 0, hi: 1, bins: 20 };
  if (role?.role === "percent" || (role?.role === "score" && role.unit === "0-100")) return { lo: 0, hi: 100, bins: 20 };
  return null;
}

/** The binning statement over any relation: one row per non-empty bin with its edges. */
export function histogramSql(fns: SqlFns, from: string, column: string, conditions: string[]): string {
  const c = quoteIdent(column);
  const cond = [...conditions, `${c} IS NOT NULL`].join(" AND ");
  return (
    `SELECT (:lo + (b - 1) * ((:hi - :lo) / :bins))::float8 AS bin_lo, (:lo + b * ((:hi - :lo) / :bins))::float8 AS bin_hi, n ` +
    `FROM (SELECT ${fns.widthBucket(c, ":lo", ":hi", ":bins")} AS b, count(*) AS n FROM ${from} WHERE ${cond} GROUP BY 1) h ORDER BY 1`
  );
}

function params(b: Bins): Record<string, ParamValue> {
  return { lo: number(b.lo), hi: number(b.hi), bins: int(b.bins) };
}

export const histogram: LensDef = {
  id: "histogram",
  group: "distribution",
  needs: ["a quantity with a known range"],
  summary: "How the values of one quantity are distributed, in equal bins.",
  channels: { x0: "lower bin edge", x1: "upper bin edge", y: "records in the bin" },
  affinity: { measurement: 0.8, interval: 0.6, feed_item: 0.5, gauge: 0.5, transcript: 0.4, document: 0.4, conversation: 0.3 },
  fit(profile, roles) {
    const usable = rankedMeasures(profile, roles).filter((m) => chooseBins(fieldOf(profile, m.field), m) !== null);
    return usable.length > 0 ? { fitness: 1, why: `${usable.length} quantities with a range` } : { fitness: 0, why: "No quantity with a known range" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    for (const m of rankedMeasures(ctx.profile, ctx.roles)) {
      if (out.length >= 3) break;
      const f = fieldOf(ctx.profile, m.field);
      const b = chooseBins(f, m);
      if (!b) continue;
      const distinct = f?.distinct ?? null;
      const flat = distinct !== null && distinct <= 3 ? 0.3 : 1;
      out.push({
        spec: channelSpec(ctx, "histogram", {
          title: `Distribution of ${label(m.field).toLowerCase()}`,
          why: f?.numeric
            ? `${label(m.field)} ranges from ${trim(f.numeric.min)} to ${trim(f.numeric.max)}; the bins show where it usually sits`
            : `${label(m.field)} is a ${m.role}; the bins show where it usually sits`,
          sql: histogramSql(ctx.fns, ctx.view, m.field, [SCOPE]),
          row_cap: 200,
          encode: { x0: "bin_lo", x1: "bin_hi", y: "n" },
          params: params(b),
          options: { ...unitOptions(ctx.roles, [m.field]), label: label(m.field) },
          fields: [m.field],
        }),
        information: Math.min(1, measureInterest(ctx.profile, m)) * 0.75 * flat,
        cost: costOf(ctx.rows, b.bins),
      });
    }
    return out;
  },
  buildResult(ctx) {
    if (!ctx.wrapped || ctx.rows < 30) return [];
    const t = resultTime(ctx);
    const series = t !== undefined && isUnique(ctx, t.name);
    const out: LensVariant[] = [];
    for (const m of resultMeasures(ctx)) {
      if (out.length >= 2) break;
      if ((m.distinct ?? 0) < 8) continue;
      const b = chooseBins(m, ctx.roles[m.name]);
      if (!b) continue;
      out.push({
        spec: resultSpec("histogram", "wrapped", {
          title: `Distribution of ${m.name}`,
          why: `${m.distinct} different values of ${m.name} in ${ctx.rows} rows; the bins show their spread`,
          sql: histogramSql(ctx.fns, ctx.wrapped, m.name, []),
          row_cap: 200,
          encode: { x0: "bin_lo", x1: "bin_hi", y: "n" },
          params: params(b),
          options: { label: m.name },
          fields: [m.name],
        }),
        information: series ? 0.4 : 0.7,
        cost: 0.1,
      });
    }
    return out;
  },
};

function trim(n: number): string {
  if (Number.isInteger(n)) return n.toLocaleString("en-US");
  const a = Math.abs(n);
  return a >= 100 ? n.toFixed(0) : a >= 1 ? n.toFixed(1) : String(Number(n.toPrecision(2)));
}
