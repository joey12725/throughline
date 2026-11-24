import type { LensVariant } from "../types";
import { type LensDef, SCOPE, channelSpec, col, costOf, distinctOf, int, label, lower, rolesOf } from "./common";

export const speakerShare: LensDef = {
  id: "speaker_share",
  group: "structure",
  needs: ["a speaker field (role, direction, speaker label) or several authors"],
  summary: "Who carries the channel: records and characters per speaker.",
  channels: { label: "speaker", value: "records", chars: "characters written" },
  affinity: { conversation: 0.9, transcript: 0.9, document: 0.5 },
  fit(profile, roles) {
    if (rolesOf(roles, "category.speaker").length > 0) return { fitness: 1, why: "A speaker field" };
    const speakers = Math.max(profile.envelope.people, profile.envelope.authors);
    return speakers >= 2 ? { fitness: 0.9, why: `${speakers} authors` } : { fitness: 0, why: "Nobody to compare" };
  },
  build(ctx) {
    const out: LensVariant[] = [];
    const tail = `count(*) AS n, coalesce(sum(length(content)), 0)::float8 AS chars FROM ${ctx.view} WHERE ${SCOPE} GROUP BY 1 ORDER BY n DESC, 1 LIMIT :top`;
    for (const s of rolesOf(ctx.roles, "category.speaker").slice(0, 2)) {
      const d = distinctOf(ctx.profile, s);
      if (d !== null && d < 2) continue;
      out.push({
        spec: channelSpec(ctx, "speaker_share", {
          title: `Share by ${lower(s.field)}`,
          why: `${label(s.field)} says who is speaking${s.domain ? ` (${s.domain.slice(0, 4).join(", ")})` : ""}; the bars show who carries the channel`,
          sql: `SELECT coalesce(${col(s.field)}, '(unknown)') AS speaker, ${tail}`,
          row_cap: 50,
          encode: { label: "speaker", value: "n", chars: "chars" },
          params: { top: int(12) },
          fields: [s.field],
        }),
        information: 0.85 * s.confidence,
        cost: costOf(ctx.rows, 12),
      });
    }
    const speakers = Math.max(ctx.profile.envelope.people, ctx.profile.envelope.authors);
    if (speakers >= 2) {
      out.push({
        spec: channelSpec(ctx, "speaker_share", {
          title: "Who writes most",
          why: `${speakers} different authors; the bars rank them by records and characters`,
          sql: `SELECT coalesce(display_name, person_handle, author_handle, '(unknown)') AS speaker, ${tail}`,
          row_cap: 50,
          encode: { label: "speaker", value: "n", chars: "chars" },
          params: { top: int(12) },
        }),
        information: (speakers >= 3 ? 0.8 : 0.5) * (out.length > 0 ? 0.7 : 1),
        cost: costOf(ctx.rows, 12),
      });
    }
    return out;
  },
};
