import { bucketExpr, lit } from "../sql";
import type { LensVariant } from "../types";
import { type LensDef, SCOPE, channelSpec, col, costOf, distinctOf, label, lower, rankedCategories, resultCategories, resultSpec, resultTime, rolesOf } from "./common";

export const statusMatrix: LensDef = {
  id: "status_matrix",
  group: "structure",
  needs: ["a status field", "optionally a source or group to give the rows"],
  summary: "Rows of sources against days, each cell coloured by status: where and when things failed.",
  channels: { x: "bucket start (timestamp)", lane: "row label", color: "status", value: "records in the cell" },
  affinity: { snapshot: 1, gauge: 0.5, document: 0.5, feed_item: 0.4, registry: 0.4 },
  fit(_profile, roles) {
    const s = rolesOf(roles, "category.status")[0];
    return s ? { fitness: 1, why: `${s.field} is a status` } : { fitness: 0, why: "No status field" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const status = rolesOf(ctx.roles, "category.status")[0];
    if (!status) return out;
    const d = distinctOf(ctx.profile, status);
    const lane = rankedCategories(ctx.profile, ctx.roles, ["group_key"]).find((r) => {
      if (r.field === status.field || r.role.startsWith("flag")) return false;
      const k = distinctOf(ctx.profile, r);
      return k === null ? /source|name|kind|stream|search/.test(r.field) : k >= 2 && k <= 30;
    });
    const lanes = lane ? (distinctOf(ctx.profile, lane) ?? 8) : 1;
    out.push({
      spec: channelSpec(ctx, "status_matrix", {
        title: lane ? `${label(status.field)} per ${lower(lane.field)}, over time` : `${label(status.field)} over time`,
        why: `${label(status.field)} is a status${status.domain ? ` (${status.domain.slice(0, 4).join(", ")}${status.domain.length > 4 ? ", ..." : ""})` : ""}${lane ? `; one row per ${lower(lane.field)} shows which ones fail and when` : "; the strip shows when it changed"}`,
        sql:
          `SELECT ${bucketExpr()} AS bucket, ${lane ? `coalesce((${col(lane.field)})::text, '(none)')` : "(:channel)::text"} AS lane, ` +
          `coalesce(${col(status.field)}, '(none)') AS status, count(*) AS n FROM ${ctx.view} WHERE ${SCOPE} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3`,
        row_cap: 10000,
        encode: { x: "bucket", lane: "lane", color: "status", value: "n" },
        options: { min_grain: "day", bucket_budget: Math.max(60, Math.floor(4000 / lanes)) },
        fields: [status.field, ...(lane ? [lane.field] : [])],
      }),
      information: d !== null && d < 2 ? 0.25 : 0.95,
      cost: costOf(ctx.rows, 3000),
    });
    if (ctx.siblings.length >= 2 && ctx.siblings.every((s) => s.fields.some((f) => f.name === status.field))) {
      const names = ctx.siblings.map((s) => s.name).sort();
      const firsts = ctx.siblings.map((s) => s.first_at).filter((x): x is string => !!x).sort();
      const lasts = ctx.siblings.map((s) => s.last_at).filter((x): x is string => !!x).sort();
      out.push({
        spec: channelSpec(ctx, "status_matrix", {
          title: `${label(status.field)} across the ${names.length} channels of this family`,
          why: `${names.length} channels declare the same fields; one row per channel shows which sources fail and when`,
          sql:
            `SELECT ${bucketExpr()} AS bucket, channel AS lane, coalesce(fields ->> ${lit.text(status.field)}, '(none)') AS status, count(*) AS n ` +
            `FROM api.messages WHERE channel = ANY(:channels) AND ${SCOPE} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3`,
          row_cap: 10000,
          encode: { x: "bucket", lane: "lane", color: "status", value: "n" },
          params: { channels: { type: "text[]", value: names } },
          options: {
            min_grain: "day",
            bucket_budget: Math.max(60, Math.floor(4000 / names.length)),
            extent: [firsts[0] ?? null, lasts[lasts.length - 1] ?? null],
          },
          fields: [status.field],
        }),
        information: 0.7,
        cost: costOf(ctx.siblings.reduce((a, s) => a + (s.rows ?? 0), 0), 4000),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const t = resultTime(ctx);
    const status = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "category.status");
    if (!t || !status || ctx.rows < 4) return [];
    const lane = resultCategories(ctx).find((c) => c.name !== status.name && (c.distinct ?? 99) <= 40);
    const n = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "count");
    return [
      {
        spec: resultSpec("status_matrix", "as_is", {
          title: `${status.name} over ${t.name}`,
          why: `The result has a time, a status${lane ? ` and ${lane.name} for the rows` : ""}`,
          sql: ctx.statement,
          row_cap: 10000,
          encode: { x: t.name, color: status.name, ...(lane ? { lane: lane.name } : {}), ...(n ? { value: n.name } : {}) },
          fields: [t.name, status.name],
        }),
        information: 0.8,
        cost: 0,
      },
    ];
  },
};
