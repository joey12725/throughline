import type { LensVariant } from "../types";
import { type LensDef, SCOPE, channelSpec, col, fieldOf, fillOf, label, resultSpec, rolesOf } from "./common";

export const jsonTree: LensDef = {
  id: "json_tree",
  group: "structure",
  needs: ["a JSON field holding objects or lists of objects"],
  summary: "The structure of a JSON field, merged over the latest records: keys, types and example values.",
  channels: { value: "the JSON value", id: "record id", time: "record time" },
  affinity: { snapshot: 0.8, registry: 0.6, document: 0.5 },
  fit(_profile, roles) {
    const t = rolesOf(roles, "json.object", "json.items");
    return t.length > 0 ? { fitness: 1, why: `${t[0]?.field} is structured JSON` } : { fitness: 0, why: "No structured JSON field" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const fields = rolesOf(ctx.roles, "json.object", "json.items")
      .map((r) => ({ r, keys: fieldOf(ctx.profile, r.field)?.json?.keys ?? [], fill: fillOf(ctx.profile, r.field) }))
      .sort((a, b) => b.fill * (1 + b.keys.length / 10) - a.fill * (1 + a.keys.length / 10))
      .slice(0, 2);
    for (const { r, keys, fill } of fields) {
      const c = col(r.field);
      out.push({
        spec: channelSpec(ctx, "json_tree", {
          title: `Inside ${label(r.field).toLowerCase()}`,
          why:
            keys.length > 0
              ? `${label(r.field)} is ${r.role === "json.items" ? "a list of objects" : "an object"} with keys such as ${keys.slice(0, 4).join(", ")}; the tree shows its shape`
              : `${label(r.field)} is structured JSON; the tree shows its shape`,
          sql: `SELECT id, happened_at, ${c} AS value FROM ${ctx.view} WHERE ${SCOPE} AND ${c} IS NOT NULL ORDER BY happened_at DESC, id DESC LIMIT 25`,
          row_cap: 25,
          encode: { value: "value", id: "id", time: "happened_at" },
          options: keys.length ? { keys: keys.slice(0, 20) } : {},
          drill: { to: "record", key: "id" },
          fields: [r.field],
        }),
        information: (r.role === "json.object" ? 0.6 : 0.45) * Math.max(0.3, fill) * (0.5 + 0.5 * r.confidence),
        cost: 0.02,
      });
    }
    return out;
  },
  buildResult(ctx) {
    const j = ctx.profile.columns.find((c) => c.kind === "json" && c.json && c.json.shape !== "scalar");
    if (!j || ctx.rows < 1) return [];
    return [
      {
        spec: resultSpec("json_tree", "as_is", {
          title: `Inside ${j.name}`,
          why: `${j.name} is structured JSON; the tree shows its shape`,
          sql: ctx.statement,
          row_cap: 50,
          encode: { value: j.name },
          fields: [j.name],
        }),
        information: ctx.profile.columns.length <= 3 ? 0.7 : 0.4,
        cost: 0,
      },
    ];
  },
};
