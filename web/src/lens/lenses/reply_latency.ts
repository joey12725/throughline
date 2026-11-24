import { type LensDef, SCOPE, channelSpec, col, costOf, lower, rolesOf } from "./common";

export const REPLY_BINS = ["under 1 min", "1-5 min", "5-30 min", "30 min-2 h", "2-8 h", "8-24 h", "1-7 days", "over a week"] as const;
const EDGES = [60, 300, 1800, 7200, 28800, 86400, 604800];

export const replyLatency: LensDef = {
  id: "reply_latency",
  group: "structure",
  needs: ["threads where the speaker changes from one record to the next"],
  summary: "How long a reply takes: the gap between consecutive records of a thread when the speaker changes.",
  channels: { bin: "latency class, 0 (under a minute) to 7 (over a week); labels in options.bins", value: "replies in the class" },
  affinity: { conversation: 0.85, transcript: 0.35 },
  fit(profile, roles) {
    const speakers = rolesOf(roles, "category.speaker").length > 0 || Math.max(profile.envelope.people, profile.envelope.authors) >= 2;
    if (!speakers) return { fitness: 0, why: "Only one side speaks" };
    if (profile.source === "declared") return { fitness: 0.5, why: "Replies will show once records arrive" };
    return profile.threads.rows_per_thread >= 4 ? { fitness: 1, why: "Threads with turns" } : { fitness: 0, why: "Threads are too short for replies" };
  },
  build(ctx) {
    const speaker = rolesOf(ctx.roles, "category.speaker")[0];
    const who = speaker ? `coalesce(${col(speaker.field)}, '')` : "coalesce(person_id::text, author_handle, '')";
    const w = { partitionBy: "thread_id", orderBy: "happened_at, id" };
    const cases = EDGES.map((edge, i) => `WHEN gap_s < ${edge} THEN ${i}`).join(" ");
    const sql =
      `WITH o AS (SELECT happened_at, ${who} AS who, ${ctx.fns.lag("happened_at", w)} AS prev_at, ${ctx.fns.lag(who, w)} AS prev_who ` +
      `FROM ${ctx.view} WHERE ${SCOPE}), ` +
      `g AS (SELECT extract(epoch FROM happened_at - prev_at)::float8 AS gap_s FROM o WHERE prev_at IS NOT NULL AND who IS DISTINCT FROM prev_who) ` +
      `SELECT bin, count(*) AS n FROM (SELECT CASE ${cases} ELSE ${EDGES.length} END AS bin FROM g) b GROUP BY 1 ORDER BY 1`;
    return [
      {
        spec: channelSpec(ctx, "reply_latency", {
          title: "How fast replies come",
          why: speaker
            ? `When ${lower(speaker.field)} changes inside a thread, the gap is a reply; the bars show how long replies take`
            : "When the author changes inside a thread, the gap is a reply; the bars show how long replies take",
          sql,
          row_cap: 20,
          encode: { bin: "bin", value: "n" },
          options: { bins: [...REPLY_BINS] },
          fields: speaker ? [speaker.field] : [],
        }),
        information: 0.8,
        cost: costOf(ctx.rows, 8, 4),
      },
    ];
  },
};
