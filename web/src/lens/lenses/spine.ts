import { bucketExpr, quoteIdent } from "../sql";
import { type LensDef, channelSpec, costOf, isUnique, resultSpec, resultTime } from "./common";

export const spine: LensDef = {
  id: "spine",
  group: "time",
  needs: ["the record time"],
  summary: "Records per time bucket over the whole life of the data; the axis every brush starts from.",
  channels: { x: "bucket start (timestamp)", y: "records in the bucket" },
  affinity: {},
  fit: () => ({ fitness: 1, why: "Every record has a time" }),
  build(ctx) {
    const sql = `SELECT ${bucketExpr()} AS bucket, count(*) AS n FROM ${ctx.view} GROUP BY 1 ORDER BY 1`;
    // A channel that keeps a fixed cadence (about one record per bucket, e.g. one
    // Oura summary per day) has a count that is always ~1: a filled band at 1 says
    // nothing. Draw presence/coverage — ticks where a period is present, gaps where
    // it is missing — instead of a meaningless count. Decided from cadence, not name.
    const cad = ctx.profile.cadence;
    const fixedCadence =
      !ctx.predicted &&
      (cad.class === "daily" || cad.class === "regular") &&
      cad.per_active_day_median > 0 &&
      cad.per_active_day_median <= 1.5;
    return [
      {
        spec: channelSpec(ctx, "spine", {
          title: "Records over time",
          why: fixedCadence
            ? "One record per period on a fixed cadence; the strip shows coverage, with gaps where a period is missing"
            : "Every record has a time; this is the channel's whole life",
          sql,
          row_cap: 2000,
          encode: { x: "bucket", y: "n" },
          options: fixedCadence ? { mode: "coverage" } : undefined,
        }),
        information: ctx.predicted ? 0.3 : 0.45,
        cost: costOf(ctx.rows, 400),
      },
    ];
  },
  buildResult(ctx) {
    const t = resultTime(ctx);
    if (!t || !t.time) return [];
    const extent = [t.time.min, t.time.max];
    const counts = ctx.profile.columns.filter((c) => ctx.roles[c.name]?.role === "count" && /^(n|count|total|rows|records|messages)$/.test(c.name));
    if (isUnique(ctx, t.name) && counts.length > 0) {
      return [
        {
          spec: resultSpec("spine", "as_is", {
            title: "Rows over time",
            why: "The result already has one row per time bucket with a count",
            sql: ctx.statement,
            row_cap: 10000,
            encode: { x: t.name, y: (counts[0] as { name: string }).name },
          }),
          information: 0.4,
          cost: 0,
        },
      ];
    }
    if (!ctx.wrapped || ctx.rows < 3) return [];
    const sql = `SELECT ${bucketExpr(`(${quoteIdent(t.name)})::timestamptz`)} AS bucket, count(*) AS n FROM ${ctx.wrapped} GROUP BY 1 ORDER BY 1`;
    return [
      {
        spec: resultSpec("spine", "wrapped", {
          title: `Rows over ${t.name}`,
          why: "The result has a time column; this counts its rows per bucket",
          sql,
          row_cap: 2000,
          encode: { x: "bucket", y: "n" },
          options: { extent },
        }),
        information: 0.35,
        cost: 0.1,
      },
    ];
  },
};
