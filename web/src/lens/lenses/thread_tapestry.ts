import { bucketExpr } from "../sql";
import { type LensDef, channelSpec, costOf, int } from "./common";

export const threadTapestry: LensDef = {
  id: "thread_tapestry",
  group: "structure",
  needs: ["threads with several records each"],
  summary: "One row per thread, one mark per time bucket: which conversations lived when, and how busy they were.",
  channels: {
    row: "thread id",
    label: "thread title",
    x: "bucket start (timestamp)",
    value: "records of the thread in the bucket",
    people: "distinct speakers in the bucket",
    order: "the thread's first activity, for sorting rows",
  },
  affinity: { conversation: 1, transcript: 0.6, document: 0.3 },
  fit(profile) {
    if (profile.source === "declared") return { fitness: 0.5, why: "Threads will show once records arrive" };
    const t = profile.threads;
    if (t.count < 3 || t.rows_per_thread < 4) return { fitness: 0, why: "Too few threads, or one record per thread" };
    return { fitness: Math.min(1, 0.5 + t.count / 16), why: `${t.count} threads of about ${Math.round(t.rows_per_thread)} records` };
  },
  build(ctx) {
    const t = ctx.profile.threads;
    const shown = Math.max(3, Math.min(60, t.count || 60));
    const sql =
      `WITH top AS (SELECT thread_id, count(*) AS total, min(happened_at) AS first_at FROM ${ctx.view} ` +
      `WHERE happened_at >= :from AND happened_at < :to GROUP BY 1 ORDER BY total DESC, 1 LIMIT :threads) ` +
      `SELECT m.thread_id, coalesce(max(m.thread_title), max(m.thread_external_id), m.thread_id::text) AS thread, ` +
      `${bucketExpr("m.happened_at")} AS bucket, count(*) AS n, ` +
      `count(DISTINCT coalesce(m.person_id::text, m.author_handle)) AS people, min(top.first_at) AS first_at ` +
      `FROM ${ctx.view} m JOIN top ON top.thread_id = m.thread_id ` +
      `WHERE m.happened_at >= :from AND m.happened_at < :to GROUP BY m.thread_id, 3 ORDER BY first_at, m.thread_id, bucket`;
    return [
      {
        spec: channelSpec(ctx, "thread_tapestry", {
          title: ctx.predicted ? "Threads over time" : `The ${shown} busiest threads over time`,
          why: ctx.predicted
            ? "Records belong to threads; each thread gets a row and each bucket a mark"
            : `${t.count.toLocaleString("en-US")} threads carry ${ctx.rows.toLocaleString("en-US")} records; a row per thread shows which were alive when`,
          sql,
          row_cap: 10000,
          encode: { row: "thread_id", label: "thread", x: "bucket", value: "n", people: "people", order: "first_at" },
          params: { threads: int(shown) },
          options: { bucket_budget: Math.max(40, Math.floor(9000 / shown)) },
        }),
        information: ctx.predicted ? 0.6 : t.count >= 8 ? 0.95 : 0.6,
        cost: costOf(ctx.rows, 6000, 2),
      },
    ];
  },
};
