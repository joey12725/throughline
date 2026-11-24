import { sampleModulo } from "../sql";
import type { LensVariant } from "../types";
import { type LensDef, channelSpec, col, costOf, fieldOf, fillOf, int, label, rolesOf } from "./common";

export const tags: LensDef = {
  id: "tags",
  group: "structure",
  needs: ["a JSON field holding a list of plain values (labels, tags, participants)"],
  summary: "The most common elements across a list-valued field.",
  channels: { label: "the element", value: "records that carry it" },
  affinity: { document: 0.8, feed_item: 0.6, interval: 0.5, conversation: 0.4, snapshot: 0.4, registry: 0.5 },
  fit(_profile, roles) {
    const t = rolesOf(roles, "json.tags");
    return t.length > 0 ? { fitness: 1, why: `${t[0]?.field} is a list of plain values` } : { fitness: 0, why: "No list-valued field" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const narrow = !ctx.caps.has("jsonb_array_elements_text");
    for (const r of rolesOf(ctx.roles, "json.tags").slice(0, 2)) {
      const f = fieldOf(ctx.profile, r.field);
      const avg = f?.json?.avg_items ?? null;
      const el = ctx.fns.arrayElementsText(`m.${col(r.field)}`, "e");
      // Without the set-returning builtin every row is probed at 50 positions: sample big channels.
      const k = narrow ? sampleModulo(ctx.rows, 20000) : 1;
      const cond = ["m.happened_at >= :from", "m.happened_at < :to", el.where, k > 1 ? "m.id % :k = 0" : null].filter(Boolean).join(" AND ");
      out.push({
        spec: channelSpec(ctx, "tags", {
          title: `Most common ${label(r.field).toLowerCase()}`,
          why: `${label(r.field)} holds a list per record${avg !== null ? ` (about ${avg.toFixed(1)} items)` : ""}; counting the items shows what recurs`,
          sql: `SELECT ${el.value} AS tag, count(*) AS n FROM ${ctx.view} m CROSS JOIN ${el.join} WHERE ${cond} GROUP BY 1 ORDER BY n DESC, 1 LIMIT :top`,
          row_cap: 100,
          encode: { label: "tag", value: "n" },
          params: { top: int(40), ...(k > 1 ? { k: int(k) } : {}) },
          sampled: k > 1,
          fields: [r.field],
        }),
        information: (avg !== null && avg < 0.2 ? 0.15 : 0.75) * Math.max(0.3, fillOf(ctx.profile, r.field)) * r.confidence,
        cost: costOf(ctx.rows, 40, narrow ? 20 : 2),
      });
    }
    return out;
  },
};
