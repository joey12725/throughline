import { type LensDef, SCOPE, channelSpec, costOf, int } from "./common";

export const egoGraph: LensDef = {
  id: "ego_graph",
  group: "relation",
  needs: ["records attributed to several known people"],
  summary: "The owner at the centre and the people of this channel around: size by volume, distance by recency.",
  channels: { node: "person id", label: "person name", weight: "records", threads: "threads shared", recency: "last record time" },
  affinity: { conversation: 0.75, transcript: 0.6, document: 0.5 },
  fit(profile) {
    if (profile.source === "declared") return { fitness: 0.3, why: "People will show once records arrive" };
    const p = profile.envelope.people;
    return p >= 3 ? { fitness: Math.min(1, 0.5 + p / 20), why: `${p} known people write here` } : { fitness: 0, why: "Fewer than three known people" };
  },
  build(ctx) {
    const p = ctx.profile.envelope.people;
    return [
      {
        spec: channelSpec(ctx, "ego_graph", {
          title: "People in this channel",
          why: ctx.predicted
            ? "Records are attributed to people; they arrange around the owner by volume and recency"
            : `${p} known people write here; they arrange around the owner by how much and how recently`,
          sql:
            `SELECT person_id, coalesce(max(display_name), max(person_handle), person_id::text) AS person, count(*) AS n, ` +
            `count(DISTINCT thread_id) AS threads, max(happened_at) AS last_at FROM ${ctx.view} ` +
            `WHERE ${SCOPE} AND person_id IS NOT NULL GROUP BY 1 ORDER BY n DESC, 1 LIMIT :top`,
          row_cap: 200,
          encode: { node: "person_id", label: "person", weight: "n", threads: "threads", recency: "last_at" },
          params: { top: int(60) },
          options: { centre: "owner" },
        }),
        information: ctx.predicted ? 0.4 : p >= 8 ? 0.8 : 0.55,
        cost: costOf(ctx.rows, 60),
      },
    ];
  },
};
