import { isMeasure } from "../roles";
import { freeAlias, quoteIdent } from "../sql";
import type { Role } from "../types";
import { type LensDef, SCOPE, channelSpec, col, costOf, fillOf, liveRoles, resultSpec } from "./common";

function columnOrder(role: Role): number {
  if (role.role === "title") return 0;
  if (role.role.startsWith("category")) return 1;
  if (isMeasure(role.role)) return 2;
  if (role.role === "actor.label" || role.role === "geo.label") return 3;
  if (role.role.startsWith("flag")) return 4;
  if (role.role === "date.text" || role.role.startsWith("time.")) return 5;
  if (role.role === "group_key" || role.role === "url") return 6;
  return 9;
}

export const table: LensDef = {
  id: "table",
  group: "reading",
  needs: ["nothing: always applies"],
  summary: "Rows and columns, cells rendered by role.",
  channels: { columns: "the columns to show, in order" },
  affinity: { registry: 0.8, measurement: 0.4, gauge: 0.4 },
  fit: () => ({ fitness: 1, why: "A table always applies" }),
  build(ctx) {
    const declared = new Set(ctx.meta.fields.map((f) => f.name));
    const author = freeAlias("author", declared);
    const fields = liveRoles(ctx.roles)
      .filter((r) => declared.has(r.field) && !r.plumbing && !r.role.startsWith("json.") && r.role !== "body")
      .map((r) => ({ r, key: columnOrder(r) - Math.min(0.9, fillOf(ctx.profile, r.field)) }))
      .sort((a, b) => a.key - b.key)
      .slice(0, 12)
      .map((x) => x.r.field);
    const hasEnd = ctx.profile.envelope.with_ended_at > 0;
    const hasAuthor = ctx.predicted || Math.max(ctx.profile.envelope.people, ctx.profile.envelope.authors) > 0;
    const head = ["id", "happened_at", ...(hasEnd ? ["ended_at"] : []), ...(hasAuthor ? [author] : []), "content"];
    const sql =
      `SELECT id, happened_at, ${hasEnd ? "ended_at, " : ""}` +
      `${hasAuthor ? `coalesce(display_name, person_handle, author_handle) AS ${quoteIdent(author)}, ` : ""}` +
      `left(content, 200) AS content` +
      fields.map((f) => `, ${col(f)}`).join("") +
      ` FROM ${ctx.view} WHERE ${SCOPE} ORDER BY happened_at DESC, id DESC LIMIT 200`;
    return [
      {
        spec: channelSpec(ctx, "table", {
          title: "Records as a table",
          why: `The latest records with ${fields.length ? `their ${fields.length} most telling fields` : "the envelope columns"}; identifiers and bookkeeping stay out`,
          sql,
          row_cap: 200,
          encode: { columns: [...head, ...fields] },
          options: { keyset: ["happened_at", "id"], order: "desc" },
          drill: { to: "record", key: "id" },
          fields,
        }),
        information: ctx.archetypes.primary === "registry" ? 0.7 : 0.3,
        cost: costOf(ctx.rows, 200),
      },
    ];
  },
  buildResult(ctx) {
    const names = ctx.profile.columns.map((c) => c.name);
    return [
      {
        spec: resultSpec("table", "as_is", {
          title: "Result table",
          why: names.length ? `${ctx.rows.toLocaleString("en-US")} rows by ${names.length} columns, as returned` : "The result as returned",
          sql: ctx.statement.trim() ? ctx.statement : "SELECT 1 AS empty_statement",
          row_cap: 10000,
          encode: { columns: names.length ? names.slice(0, 24) : ["empty_statement"] },
        }),
        information: 0.3,
        cost: 0,
      },
    ];
  },
};
