/*
 * Turn the raw rows of a reader query into a channel-agnostic record shape the
 * archetype readers can draw. Field resolution comes from the engine's own reader
 * binding (the `reader` lens candidate's encode) when present, and otherwise from
 * the inferred roles, so nothing here names a column by hand.
 */
import type { ApiRowLike } from "./readerTypes";
import type { LensCandidate, RoleMap } from "@/lens";

export interface ReaderRecord {
  id: string;
  time: string | null;
  endedAt: string | null;
  threadId: string | null;
  threadNo: number | null;
  threadTitle: string | null;
  author: string | null;
  text: string | null;
  title: string | null;
  url: string | null;
  speaker: string | null;
  status: string | null;
  place: string | null;
  startOffset: number | null;
  actor: string | null;
  lat: number | null;
  lon: number | null;
  mine: boolean;
  raw: ApiRowLike;
}

export interface ReaderBinding {
  id?: string;
  time?: string;
  text?: string;
  author?: string;
  title?: string;
  url?: string;
  speaker?: string;
  status?: string;
  place?: string;
  start_offset?: string;
  actor?: string;
}

const CHANNELS = ["id", "time", "text", "author", "title", "url", "speaker", "status", "place", "start_offset", "actor"] as const;

function first(v: unknown): string | undefined {
  if (typeof v === "string") return v;
  if (Array.isArray(v) && typeof v[0] === "string") return v[0];
  return undefined;
}

/** The reader binding from the `reader` candidate, falling back to roles + envelope. */
export function readerBinding(candidate: LensCandidate | undefined, roles: RoleMap): ReaderBinding {
  const b: ReaderBinding = {};
  const enc = candidate?.spec.encode ?? {};
  for (const ch of CHANNELS) {
    const col = first(enc[ch]);
    if (col) (b as Record<string, string>)[ch] = col;
  }
  b.id ??= "id";
  b.time ??= "happened_at";
  b.text ??= "content";
  if (!b.author) {
    const speaker = Object.values(roles).find((r) => r.role === "category.speaker");
    const actor = Object.values(roles).find((r) => r.role === "actor.label");
    b.author = speaker?.field ?? actor?.field ?? "author_handle";
  }
  if (!b.title) {
    const title = Object.values(roles).find((r) => r.role === "title");
    if (title) b.title = title.field;
  }
  if (!b.url) {
    const url = Object.values(roles).find((r) => r.role === "url");
    if (url) b.url = url.field;
  }
  if (!b.status) {
    const st = Object.values(roles).find((r) => r.role === "category.status");
    if (st) b.status = st.field;
  }
  return b;
}

/** The distinct columns a reader needs to SELECT for a binding. */
export function bindingColumns(b: ReaderBinding): string[] {
  const out = new Set<string>();
  for (const ch of CHANNELS) {
    const col = (b as Record<string, string | undefined>)[ch];
    if (col) out.add(col);
  }
  return [...out];
}

function str(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "string") return v.length ? v : null;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return null;
}
function n(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const x = Number(v);
    return Number.isFinite(x) ? x : null;
  }
  return null;
}

const MINE = /^(me|self|owner|outgoing|sent|you|assistant)$/i;

export function normalizeRecord(raw: ApiRowLike, b: ReaderBinding): ReaderRecord {
  const get = (col?: string): unknown => (col ? raw[col] : undefined);
  const author = str(get(b.author)) ?? str(raw.author_handle) ?? str(raw.person_handle) ?? str(raw.display_name);
  const speaker = str(get(b.speaker)) ?? author;
  return {
    id: str(raw.id) ?? str(get(b.id)) ?? "",
    time: str(get(b.time)) ?? str(raw.happened_at),
    endedAt: str(raw.ended_at),
    threadId: str(raw.thread_id),
    threadNo: n(raw.thread_no),
    threadTitle: str(raw.thread_title),
    author,
    text: str(get(b.text)) ?? str(raw.content),
    title: str(get(b.title)),
    url: str(get(b.url)),
    speaker,
    status: str(get(b.status)),
    place: str(get(b.place)),
    startOffset: n(get(b.start_offset)),
    actor: str(get(b.actor)),
    lat: n(raw.lat),
    lon: n(raw.lon),
    mine: MINE.test((author ?? "").trim()),
    raw,
  };
}
