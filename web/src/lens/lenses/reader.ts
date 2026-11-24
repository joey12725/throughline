import { freeAlias, quoteIdent } from "../sql";
import type { ArchetypeId, Role } from "../types";
import { type LensDef, SCOPE, channelSpec, col, costOf, resultSpec, resultTime, rolesOf } from "./common";

export type ReaderVariant = "bubbles" | "transcript" | "document" | "feed" | "cards";

export function readerVariant(primary: ArchetypeId): ReaderVariant {
  switch (primary) {
    case "conversation": return "bubbles";
    case "transcript": return "transcript";
    case "document": return "document";
    case "feed_item": return "feed";
    default: return "cards";
  }
}

const CLIP: Record<ReaderVariant, number> = { bubbles: 4000, transcript: 4000, document: 1200, feed: 600, cards: 600 };
const SAY: Record<ReaderVariant, string> = {
  bubbles: "Records read as a conversation: bubbles per speaker, threads in order",
  transcript: "Records are turns of a recording: speaker, offset and text in order",
  document: "Each record is a text to read: a list of documents to open",
  feed: "Each record is an item with a title and a link: a feed",
  cards: "The records themselves, newest first",
};

export const reader: LensDef = {
  id: "reader",
  group: "reading",
  needs: ["records"],
  summary: "The records themselves in the form their archetype reads best: bubbles, transcript, documents, feed or cards.",
  channels: {
    id: "record id",
    time: "record time",
    text: "content (clipped; the drawer loads the whole record)",
    author: "resolved author",
    thread: "thread id",
    title: "optional title field",
    speaker: "optional speaker field",
    url: "optional link field (inert until clicked)",
    actor: "optional publisher or organiser field",
    status: "optional status field",
    place: "optional place field",
    end: "optional end time",
    start_offset: "optional start offset in seconds",
    end_offset: "optional end offset in seconds",
  },
  affinity: { document: 0.95, transcript: 0.9, feed_item: 0.85, conversation: 0.8, snapshot: 0.6, registry: 0.6, interval: 0.5, gauge: 0.3, measurement: 0.25, geo_track: 0.2 },
  fit: () => ({ fitness: 1, why: "Records can always be read" }),
  build(ctx) {
    const variant = readerVariant(ctx.archetypes.primary);
    const declared = new Set(ctx.meta.fields.map((f) => f.name));
    const author = freeAlias("author", declared);
    const chars = freeAlias("content_chars", declared);
    const pick = (role: Role | undefined) => (role && declared.has(role.field) ? role.field : undefined);
    const title = pick(rolesOf(ctx.roles, "title")[0]);
    const speaker = pick(rolesOf(ctx.roles, "category.speaker")[0]);
    const url = pick(rolesOf(ctx.roles, "url")[0]);
    const actor = pick(rolesOf(ctx.roles, "actor.label")[0]);
    const status = pick(rolesOf(ctx.roles, "category.status")[0]);
    const place = pick(rolesOf(ctx.roles, "geo.label")[0]);
    const offsetStart = rolesOf(ctx.roles, "time.offset").find((r) => r.pair && /start/.test(r.field));
    const offsetEnd = offsetStart?.pair ? ctx.roles[offsetStart.pair] : undefined;
    const extras = [title, speaker, url, actor, status, place, pick(offsetStart), pick(offsetEnd)].filter((x): x is string => !!x);
    const unique = [...new Set(extras)];
    const hasEnd = ctx.profile.envelope.with_ended_at > 0;
    const sql =
      `SELECT id, thread_id, thread_no, happened_at, ${hasEnd ? "ended_at, " : ""}` +
      `coalesce(display_name, person_handle, author_handle) AS ${quoteIdent(author)}, person_id, ` +
      `left(content, ${CLIP[variant]}) AS content, length(content) AS ${quoteIdent(chars)}` +
      unique.map((f) => `, ${col(f)}`).join("") +
      ` FROM ${ctx.view} WHERE ${SCOPE} ORDER BY happened_at DESC, id DESC LIMIT 200`;
    const chars_ = ctx.profile.envelope.avg_content_chars;
    return [
      {
        spec: channelSpec(ctx, "reader", {
          title: variant === "cards" ? "Latest records" : variant === "bubbles" ? "Latest messages" : variant === "transcript" ? "Latest turns" : variant === "document" ? "Latest documents" : "Latest items",
          why: SAY[variant],
          sql,
          row_cap: 200,
          encode: {
            id: "id",
            time: "happened_at",
            text: "content",
            author,
            thread: "thread_id",
            ...(hasEnd ? { end: "ended_at" } : {}),
            ...(title ? { title } : {}),
            ...(speaker ? { speaker } : {}),
            ...(url ? { url } : {}),
            ...(actor ? { actor } : {}),
            ...(status ? { status } : {}),
            ...(place ? { place } : {}),
            ...(offsetStart && offsetEnd ? { start_offset: offsetStart.field, end_offset: offsetEnd.field } : {}),
          },
          options: { variant, keyset: ["happened_at", "id"], order: "desc", clipped_at: CLIP[variant], chars },
          drill: { to: "record", key: "id" },
          fields: unique,
        }),
        information: variant === "cards" ? (ctx.archetypes.primary === "registry" ? 0.6 : chars_ !== null && chars_ < 60 ? 0.3 : 0.45) : 0.7,
        cost: costOf(ctx.rows, 200),
      },
    ];
  },
  buildResult(ctx) {
    const body = ctx.profile.columns.find((c) => c.kind === "text" && (c.text?.avg_len ?? 0) >= 60);
    if (!body || ctx.rows < 1) return [];
    const t = resultTime(ctx);
    const title = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "title");
    const author = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "actor.label");
    const id = ctx.profile.columns.find((c) => c.name === "id" || c.name === "message_id");
    return [
      {
        spec: resultSpec("reader", "as_is", {
          title: "Read the rows",
          why: `${body.name} is long text (about ${Math.round(body.text?.avg_len ?? 0)} characters): better read than charted`,
          sql: ctx.statement,
          row_cap: 1000,
          encode: {
            text: body.name,
            ...(t ? { time: t.name } : {}),
            ...(title ? { title: title.name } : {}),
            ...(author ? { author: author.name } : {}),
            ...(id ? { id: id.name } : {}),
          },
          options: { variant: "cards" },
          fields: [body.name],
        }),
        information: 0.75,
        cost: 0,
      },
    ];
  },
};
