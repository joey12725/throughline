import { bucketExpr, quoteIdent } from "../sql";
import type { LensVariant } from "../types";
import {
  type LensDef,
  SCOPE,
  channelSpec,
  col,
  costOf,
  distinctOf,
  isUnique,
  label,
  lower,
  rankedMeasures,
  resultCategories,
  resultMeasures,
  resultSpec,
  resultTime,
  rolesOf,
  unitOptions,
} from "./common";
import { fieldFamilies } from "../roles";

export const smallMultiples: LensDef = {
  id: "small_multiples",
  group: "time",
  needs: ["a family of sibling quantities, a quantity per group key, or sibling channels"],
  summary: "The same small time chart repeated per family member, per group or per sibling channel.",
  channels: {
    x: "bucket start (timestamp)",
    y: "value column; a list of columns makes one panel per column",
    facet: "optional column whose values make the panels",
  },
  affinity: { measurement: 0.9, snapshot: 0.8, gauge: 0.7, conversation: 0.55, interval: 0.5, feed_item: 0.4 },
  fit(profile, roles) {
    const families = fieldFamilies(profile.fields, roles);
    if (families.length > 0) return { fitness: 1, why: `${families.length} families of sibling fields` };
    if (rolesOf(roles, "group_key").length > 0 && rankedMeasures(profile, roles).length > 0) return { fitness: 0.7, why: "A quantity per group key" };
    return { fitness: 0.5, why: "Sibling channels share this shape" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const partOf = new Set(ctx.wholes.filter((w) => w.tested).flatMap((w) => w.parts));
    for (const fam of ctx.families.slice(0, 3)) {
      const members = fam.members.slice(0, 12);
      const cols = members.map((m) => `avg(${col(m)})::float8 AS ${col(m)}`).join(", ");
      const sql = `SELECT ${bucketExpr()} AS bucket, ${cols} FROM ${ctx.view} WHERE ${SCOPE} GROUP BY 1 ORDER BY 1`;
      const composed = members.filter((m) => partOf.has(m)).length >= 2;
      out.push({
        spec: channelSpec(ctx, "small_multiples", {
          title: `${label(fam.id.replace(/\*/g, "").replace(/^_|_$/g, ""))}: ${members.length} fields side by side`,
          why: `${members.length} fields share the name ${fam.id} and the role ${fam.role}; one small chart each`,
          sql,
          row_cap: 2000,
          encode: { x: "bucket", y: members },
          options: { bucket_budget: 400, ...unitOptions(ctx.roles, members) },
          fields: members,
        }),
        information: composed ? 0.4 : 0.75,
        cost: costOf(ctx.rows, 400),
      });
    }
    const group = rolesOf(ctx.roles, "group_key").find((g) => {
      const d = distinctOf(ctx.profile, g);
      return d === null ? ctx.predicted : d >= 2 && d <= 12;
    });
    const measure = rankedMeasures(ctx.profile, ctx.roles)[0];
    if (group && measure) {
      const k = distinctOf(ctx.profile, group) ?? 6;
      const sql =
        `SELECT ${bucketExpr()} AS bucket, coalesce(${col(group.field)}, '(none)') AS facet, avg(${col(measure.field)})::float8 AS ${col(measure.field)} ` +
        `FROM ${ctx.view} WHERE ${SCOPE} AND ${col(measure.field)} IS NOT NULL GROUP BY 1, 2 ORDER BY 2, 1`;
      out.push({
        spec: channelSpec(ctx, "small_multiples", {
          title: `${label(measure.field)} per ${lower(group.field)}`,
          why: `${label(group.field)} splits the records into ${k} groups; each gets its own chart of ${lower(measure.field)}`,
          sql,
          row_cap: 6000,
          encode: { x: "bucket", y: measure.field, facet: "facet" },
          options: { bucket_budget: Math.max(60, Math.floor(1500 / k)), ...unitOptions(ctx.roles, [measure.field]) },
          fields: [group.field, measure.field],
        }),
        information: 0.6,
        cost: costOf(ctx.rows, 1500),
      });
    }
    if (ctx.siblings.length >= 2) {
      const names = ctx.siblings.map((s) => s.name).sort();
      const firsts = ctx.siblings.map((s) => s.first_at).filter((x): x is string => !!x).sort();
      const lasts = ctx.siblings.map((s) => s.last_at).filter((x): x is string => !!x).sort();
      const sql =
        `SELECT channel AS facet, ${bucketExpr()} AS bucket, count(*) AS n FROM api.messages ` +
        `WHERE channel = ANY(:channels) AND ${SCOPE} GROUP BY 1, 2 ORDER BY 1, 2`;
      out.push({
        spec: channelSpec(ctx, "small_multiples", {
          title: `Records in the ${names.length} channels of this family`,
          why: `${names.length} channels declare the same fields; their rhythms compare directly`,
          sql,
          row_cap: 8000,
          encode: { x: "bucket", y: "n", facet: "facet" },
          params: { channels: { type: "text[]", value: names } },
          options: {
            bucket_budget: Math.max(40, Math.floor(1500 / names.length)),
            extent: [firsts[0] ?? null, lasts[lasts.length - 1] ?? null],
          },
        }),
        information: 0.6,
        cost: costOf(ctx.siblings.reduce((a, s) => a + (s.rows ?? 0), 0), 2000),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const t = resultTime(ctx);
    const measures = resultMeasures(ctx);
    if (!t || measures.length === 0) return [];
    if (isUnique(ctx, t.name) && measures.length >= 3) {
      const names = measures.slice(0, 12).map((m) => m.name);
      return [
        {
          spec: resultSpec("small_multiples", "as_is", {
            title: `${names.length} columns over ${t.name}`,
            why: `One row per ${t.name} and ${names.length} numeric columns; one small chart each keeps their scales apart`,
            sql: ctx.statement,
            row_cap: 10000,
            encode: { x: t.name, y: names },
            fields: [t.name, ...names],
          }),
          information: 0.7,
          cost: 0,
        },
      ];
    }
    const facet = resultCategories(ctx).find((c) => isUnique(ctx, t.name, c.name) && (c.distinct ?? 99) <= 16 && (c.distinct ?? 0) >= 2);
    if (facet) {
      const m = (measures[0] as { name: string }).name;
      return [
        {
          spec: resultSpec("small_multiples", "as_is", {
            title: `${label(m)} over ${t.name}, one panel per ${facet.name}`,
            why: `One row per ${t.name} and ${facet.name}; a panel for each ${facet.name}`,
            sql: ctx.statement,
            row_cap: 10000,
            encode: { x: t.name, y: m, facet: facet.name },
            fields: [t.name, facet.name, m],
          }),
          information: 0.7,
          cost: 0,
        },
      ];
    }
    void quoteIdent;
    return [];
  },
};
