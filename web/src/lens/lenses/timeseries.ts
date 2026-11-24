import { bucketExpr, freeAlias, quoteIdent } from "../sql";
import {
  type LensDef,
  SCOPE,
  channelSpec,
  col,
  colourField,
  costOf,
  distinctOf,
  fieldOf,
  isUnique,
  label,
  lower,
  measureInterest,
  rankedMeasures,
  resultCategories,
  resultMeasures,
  resultSpec,
  resultTime,
  unitOptions,
} from "./common";

const pct = (x: number | null | undefined) => (x === null || x === undefined ? "some" : `${Math.round(x * 100)}%`);

export const timeseries: LensDef = {
  id: "timeseries",
  group: "time",
  needs: ["the record time", "a quantity, or a category to count by"],
  summary: "A quantity (or a count per category) per time bucket.",
  channels: {
    x: "bucket start (timestamp)",
    y: "value column, or several value columns drawn as separate lines",
    lo: "optional lower band (minimum in the bucket)",
    hi: "optional upper band (maximum in the bucket)",
    series: "optional category that splits the line",
  },
  affinity: { measurement: 1, gauge: 0.7, snapshot: 0.65, interval: 0.6, feed_item: 0.4, conversation: 0.4, geo_track: 0.4, document: 0.35, registry: 0.2, transcript: 0.2 },
  fit(profile, roles) {
    const measures = rankedMeasures(profile, roles);
    if (measures.length > 0) return { fitness: 1, why: `${measures.length} quantities recorded over time` };
    return colourField(profile, roles) ? { fitness: 0.6, why: "A category to count by over time" } : { fitness: 0, why: "No quantity and no small category" };
  },
  build(ctx) {
    const out = [];
    const span = ctx.predicted ? 1 : ctx.profile.cadence.span_days >= 14 ? 1 : 0.5;
    const few = ctx.predicted || ctx.rows >= 10 ? 1 : 0.4;
    const cad = ctx.profile.cadence;
    const perDay = cad.per_active_day_median;
    // A per-period measurement sampled about once a day over a long span is noisy
    // point-to-point; a rolling mean reveals the trend while the raw series stays
    // faint beneath. Decided from cadence + role, never by channel name.
    const SMOOTHABLE = new Set(["score", "percent", "measure", "probability", "mass", "duration", "distance"]);
    const dailyish =
      !ctx.predicted &&
      cad.span_days >= 21 &&
      perDay > 0 &&
      perDay <= 2 &&
      (cad.class === "daily" || cad.class === "regular" || cad.class === "continuous");
    for (const m of rankedMeasures(ctx.profile, ctx.roles).slice(0, 4)) {
      const c = col(m.field);
      const name = freeAlias(m.field, ["bucket", "lo", "hi", "n"]);
      const agg = m.role === "count" && perDay > 1.5 ? "sum" : "avg";
      const sql =
        `SELECT ${bucketExpr()} AS bucket, ${agg}(${c})::float8 AS ${quoteIdent(name)}, min(${c})::float8 AS lo, ` +
        `max(${c})::float8 AS hi, count(${c}) AS n FROM ${ctx.view} WHERE ${SCOPE} AND ${c} IS NOT NULL GROUP BY 1 ORDER BY 1`;
      const fill = fieldOf(ctx.profile, m.field)?.fill;
      const smooth = dailyish && SMOOTHABLE.has(m.role);
      out.push({
        spec: channelSpec(ctx, "timeseries", {
          title: `${label(m.field)} over time`,
          why: ctx.predicted
            ? `${label(m.field)} is a ${m.role} recorded with each record`
            : smooth
              ? `${label(m.field)} is a noisy ${m.role} on ${pct(fill)} of records; the bold line is a 7-point rolling mean`
              : `${label(m.field)} is a ${m.role} on ${pct(fill)} of records; the line shows how it moves`,
          sql,
          row_cap: 2000,
          encode: { x: "bucket", y: name, lo: "lo", hi: "hi" },
          options: { agg, ...(smooth ? { rolling: 7 } : {}), ...unitOptions(ctx.roles, [m.field]) },
          fields: [m.field],
        }),
        information: Math.min(1, measureInterest(ctx.profile, m) * 1.15) * span * few,
        cost: costOf(ctx.rows, 600),
      });
    }
    const cat = colourField(ctx.profile, ctx.roles, 8);
    if (cat) {
      const k = Math.max(2, distinctOf(ctx.profile, cat) ?? 6);
      const sql =
        `SELECT ${bucketExpr()} AS bucket, coalesce((${col(cat.field)})::text, '(none)') AS series, count(*) AS n ` +
        `FROM ${ctx.view} WHERE ${SCOPE} GROUP BY 1, 2 ORDER BY 1, 2`;
      out.push({
        spec: channelSpec(ctx, "timeseries", {
          title: `Records by ${lower(cat.field)} over time`,
          why: `${label(cat.field)} has ${k} values; their mix changes over time`,
          sql,
          row_cap: 4000,
          encode: { x: "bucket", y: "n", series: "series" },
          options: { agg: "count", stacked: true, bucket_budget: Math.max(60, Math.floor(1500 / k)) },
          fields: [cat.field],
        }),
        information: 0.55 * span * few,
        cost: costOf(ctx.rows, 1200),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const t = resultTime(ctx);
    const measures = resultMeasures(ctx);
    if (!t || measures.length === 0 || ctx.rows < 2) return [];
    const names = measures.slice(0, 6).map((m) => m.name);
    if (isUnique(ctx, t.name)) {
      return [
        {
          spec: resultSpec("timeseries", "as_is", {
            title: `${names.map(label).join(", ")} over ${t.name}`,
            why: `One row per ${t.name}: the result is already a series`,
            sql: ctx.statement,
            row_cap: 10000,
            encode: { x: t.name, y: names.length === 1 ? (names[0] as string) : names },
            fields: [t.name, ...names],
          }),
          information: 0.95,
          cost: 0,
        },
      ];
    }
    const series = resultCategories(ctx).find((c) => isUnique(ctx, t.name, c.name) && (c.distinct ?? 99) <= 12);
    if (series) {
      return [
        {
          spec: resultSpec("timeseries", "as_is", {
            title: `${label(names[0] as string)} over ${t.name} by ${series.name}`,
            why: `One row per ${t.name} and ${series.name}: a line for each ${series.name}`,
            sql: ctx.statement,
            row_cap: 10000,
            encode: { x: t.name, y: names[0] as string, series: series.name },
            fields: [t.name, series.name, names[0] as string],
          }),
          information: 0.9,
          cost: 0,
        },
      ];
    }
    if (!ctx.wrapped || !t.time) return [];
    const m = names[0] as string;
    const sql =
      `SELECT ${bucketExpr(`(${quoteIdent(t.name)})::timestamptz`)} AS bucket, avg(${quoteIdent(m)})::float8 AS ${quoteIdent(freeAlias(m, ["bucket"]))} ` +
      `FROM ${ctx.wrapped} WHERE ${quoteIdent(m)} IS NOT NULL GROUP BY 1 ORDER BY 1`;
    return [
      {
        spec: resultSpec("timeseries", "wrapped", {
          title: `${label(m)} over ${t.name}`,
          why: `Several rows share a ${t.name}; this averages ${m} per bucket`,
          sql,
          row_cap: 2000,
          encode: { x: "bucket", y: freeAlias(m, ["bucket"]) },
          options: { agg: "avg", extent: [t.time.min, t.time.max] },
          fields: [t.name, m],
        }),
        information: 0.7,
        cost: 0.1,
      },
    ];
  },
};
