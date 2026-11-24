import type { LensVariant } from "../types";
import { type LensDef, SCOPE, channelSpec, col, costOf, distinctOf, int, isUnique, label, lower, rankedCategories, resultCategories, resultMeasures, resultSpec } from "./common";

export const linkMatrix: LensDef = {
  id: "link_matrix",
  group: "relation",
  needs: ["two ways of grouping the same records: people and threads, or two categories"],
  summary: "A grid of one grouping against another, each cell the number of records the two share.",
  channels: { row: "row label", col: "column label", value: "records shared" },
  affinity: { conversation: 0.6, registry: 0.6, feed_item: 0.5, document: 0.4, gauge: 0.4, snapshot: 0.4 },
  fit(profile, roles) {
    const people = profile.envelope.people >= 3 && profile.threads.count >= 3 && profile.threads.rows_per_thread >= 3;
    const cats = rankedCategories(profile, roles, ["group_key"]).filter((c) => {
      const d = distinctOf(profile, c);
      return d !== null && d >= 2 && d <= 20;
    });
    if (people) return { fitness: 0.9, why: "People and threads" };
    return cats.length >= 2 ? { fitness: 0.7, why: `${cats[0]?.field} against ${cats[1]?.field}` } : { fitness: 0, why: "Fewer than two groupings" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const env = ctx.profile.envelope;
    if (env.people >= 3 && ctx.profile.threads.count >= 3 && ctx.profile.threads.rows_per_thread >= 3) {
      const scope = "happened_at >= :from AND happened_at < :to";
      out.push({
        spec: channelSpec(ctx, "link_matrix", {
          title: "Who is in which thread",
          why: `${env.people} people across ${ctx.profile.threads.count} threads; the grid shows who talks where`,
          sql:
            `WITH p AS (SELECT person_id FROM ${ctx.view} WHERE ${scope} AND person_id IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT :side), ` +
            `t AS (SELECT thread_id FROM ${ctx.view} WHERE ${scope} GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT :side) ` +
            `SELECT coalesce(max(m.display_name), max(m.person_handle), m.person_id::text) AS person, ` +
            `coalesce(max(m.thread_title), max(m.thread_external_id), m.thread_id::text) AS thread, count(*) AS n ` +
            `FROM ${ctx.view} m WHERE m.happened_at >= :from AND m.happened_at < :to ` +
            `AND m.person_id IN (SELECT person_id FROM p) AND m.thread_id IN (SELECT thread_id FROM t) ` +
            `GROUP BY m.person_id, m.thread_id ORDER BY n DESC`,
          row_cap: 1000,
          encode: { row: "person", col: "thread", value: "n" },
          params: { side: int(24) },
        }),
        information: 0.65,
        cost: costOf(ctx.rows, 400, 3),
      });
    }
    const cats = rankedCategories(ctx.profile, ctx.roles, ["group_key"]).filter((c) => {
      const d = distinctOf(ctx.profile, c);
      return d !== null && d >= 2 && d <= 20 && c.role !== "flag.tombstone";
    });
    const a = cats[0];
    // Skip a partner that is another name for the same thing (project and project_id).
    const b = cats.find((c) => a && c.field !== a.field && !c.field.startsWith(a.field) && !a.field.startsWith(c.field));
    if (a && b) {
      out.push({
        spec: channelSpec(ctx, "link_matrix", {
          title: `${label(a.field)} against ${lower(b.field)}`,
          why: `${label(a.field)} has ${distinctOf(ctx.profile, a)} values and ${lower(b.field)} ${distinctOf(ctx.profile, b)}; the grid shows which combinations occur`,
          sql:
            `SELECT coalesce((${col(a.field)})::text, '(none)') AS a, coalesce((${col(b.field)})::text, '(none)') AS b, count(*) AS n ` +
            `FROM ${ctx.view} WHERE ${SCOPE} GROUP BY 1, 2 ORDER BY n DESC LIMIT 400`,
          row_cap: 400,
          encode: { row: "a", col: "b", value: "n" },
          fields: [a.field, b.field],
        }),
        information: 0.45,
        cost: costOf(ctx.rows, 200),
      });
    }
    return out;
  },
  buildResult(ctx) {
    const cats = resultCategories(ctx);
    const m = resultMeasures(ctx)[0];
    if (!m || cats.length < 2 || ctx.rows < 4) return [];
    for (let i = 0; i < cats.length; i++) {
      for (let j = i + 1; j < cats.length; j++) {
        const a = cats[i] as { name: string; distinct: number | null };
        const b = cats[j] as { name: string; distinct: number | null };
        if (!isUnique(ctx, a.name, b.name) || (a.distinct ?? 99) > 60 || (b.distinct ?? 99) > 60) continue;
        return [
          {
            spec: resultSpec("link_matrix", "as_is", {
              title: `${m.name} by ${a.name} and ${b.name}`,
              why: `One row per ${a.name} and ${b.name}: a grid of ${m.name}`,
              sql: ctx.statement,
              row_cap: 5000,
              encode: { row: a.name, col: b.name, value: m.name },
              fields: [a.name, b.name, m.name],
            }),
            information: 0.8,
            cost: 0,
          },
        ];
      }
    }
    return [];
  },
};
