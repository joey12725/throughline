import { quoteIdent } from "../sql";
import type { LensVariant } from "../types";
import {
  type LensDef,
  SCOPE,
  categoryInterest,
  channelSpec,
  col,
  costOf,
  distinctOf,
  int,
  isUnique,
  label,
  lower,
  rankedCategories,
  resultCategories,
  resultMeasures,
  resultSpec,
} from "./common";

export const topValues: LensDef = {
  id: "top_values",
  group: "distribution",
  needs: ["a category, a group key or a name"],
  summary: "The most common values of one field as ranked bars.",
  channels: { label: "the value", value: "records with that value (or the measure of that row)" },
  affinity: { registry: 0.9, feed_item: 0.8, gauge: 0.7, document: 0.7, interval: 0.7, snapshot: 0.6, conversation: 0.6, measurement: 0.5, geo_track: 0.5, transcript: 0.5 },
  fit(profile, roles) {
    const cats = rankedCategories(profile, roles, ["group_key", "actor.label", "geo.label"]);
    return cats.length > 0 ? { fitness: 1, why: `${cats.length} fields with repeating values` } : { fitness: 0, why: "No field with repeating values" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    for (const c of rankedCategories(ctx.profile, ctx.roles, ["group_key", "actor.label", "geo.label"]).slice(0, 4)) {
      const d = distinctOf(ctx.profile, c);
      const status = c.role === "category.status";
      out.push({
        spec: channelSpec(ctx, "top_values", {
          title: status ? `Records by ${lower(c.field)}` : `Most common ${lower(c.field)}`,
          why:
            d !== null
              ? `${label(c.field)} takes ${d} values${c.domain && !ctx.predicted ? "" : c.domain ? ` (${c.domain.slice(0, 4).join(", ")}${c.domain.length > 4 ? ", ..." : ""})` : ""}; bars rank them`
              : `${label(c.field)} repeats across records; bars rank its values`,
          sql:
            `SELECT coalesce((${col(c.field)})::text, '(missing)') AS value, count(*) AS n FROM ${ctx.view} ` +
            `WHERE ${SCOPE} GROUP BY 1 ORDER BY n DESC, 1 LIMIT :top`,
          row_cap: 100,
          encode: { label: "value", value: "n" },
          params: { top: int(20) },
          fields: [c.field],
        }),
        information: Math.min(1, categoryInterest(ctx.profile, c) * (c.role === "group_key" ? 0.8 : 1)),
        cost: costOf(ctx.rows, 20),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const cats = resultCategories(ctx);
    const measures = resultMeasures(ctx);
    const out: LensVariant[] = [];
    const key = cats.find((c) => isUnique(ctx, c.name));
    if (key && measures.length > 0 && ctx.rows >= 2 && ctx.rows <= 2000) {
      const m = measures[0] as { name: string };
      out.push({
        spec: resultSpec("top_values", "as_is", {
          title: `${label(m.name)} by ${key.name}`,
          why: `One row per ${key.name}: bars compare ${m.name}`,
          sql: ctx.statement,
          row_cap: 2000,
          encode: { label: key.name, value: m.name },
          fields: [key.name, m.name],
        }),
        information: 0.9,
        cost: 0,
      });
      return out;
    }
    if (!ctx.wrapped || ctx.rows < 10) return out;
    for (const c of cats.filter((x) => (x.distinct ?? 0) >= 2 && (x.distinct ?? 0) < ctx.rows * 0.7).slice(0, 2)) {
      out.push({
        spec: resultSpec("top_values", "wrapped", {
          title: `Most common ${c.name}`,
          why: `${c.name} takes ${c.distinct} values over ${ctx.rows} rows; bars rank them`,
          sql: `SELECT coalesce((${quoteIdent(c.name)})::text, '(missing)') AS value, count(*) AS n FROM ${ctx.wrapped} GROUP BY 1 ORDER BY n DESC, 1 LIMIT :top`,
          row_cap: 100,
          encode: { label: "value", value: "n" },
          params: { top: int(20) },
          fields: [c.name],
        }),
        information: 0.6,
        cost: 0.1,
      });
    }
    return out;
  },
};
