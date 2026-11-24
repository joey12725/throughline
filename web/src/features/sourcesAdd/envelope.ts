/*
 * The ingest envelope and how a file's columns map onto it. A source never has
 * to visualise its own data: it only declares happened_at, an idempotent id,
 * the text, an optional thread/author/place, and any typed fields. From the
 * mapping we build three things — the channel registration, the ingest messages,
 * and a set of local rows shaped like the generated channel view so the lens
 * engine can preview the real channel page from the sample alone.
 *
 * Pure and tested (envelope.test.ts).
 */
import { channelViewName, type ChannelMeta, type FieldType, type Row, type SqlColumn } from "@/lens";
import type { InferredColumn, ParsedTable } from "./parse";

/** The core envelope slots a column can fill. The rest become declared fields. */
export type EnvSlot =
  | "happened_at"
  | "external_id"
  | "content"
  | "thread"
  | "author"
  | "ended_at"
  | "lat"
  | "lon";

export interface EnvSlotDef {
  slot: EnvSlot;
  label: string;
  required: boolean;
  description: string;
  /** types that make sense for this slot, for the picker. */
  accepts: FieldType[];
}

export const ENV_SLOTS: EnvSlotDef[] = [
  { slot: "happened_at", label: "Happened at", required: true, accepts: ["timestamp", "text", "integer"], description: "When the record happened. Needs a timezone; a bare date is read as UTC midnight." },
  { slot: "external_id", label: "External id", required: true, accepts: ["text", "integer"], description: "A stable id from the source. Re-importing the same id updates in place (idempotent)." },
  { slot: "content", label: "Text", required: true, accepts: ["text"], description: "The record's human-readable text." },
  { slot: "thread", label: "Thread", required: false, accepts: ["text", "integer"], description: "Groups records into conversations. If unset, each record is its own thread." },
  { slot: "author", label: "Author", required: false, accepts: ["text"], description: "Who produced the record." },
  { slot: "ended_at", label: "Ended at", required: false, accepts: ["timestamp", "text"], description: "For records that span time (events, sessions)." },
  { slot: "lat", label: "Latitude", required: false, accepts: ["number", "integer"], description: "Where it happened." },
  { slot: "lon", label: "Longitude", required: false, accepts: ["number", "integer"], description: "Where it happened." },
];

export interface DeclaredField {
  column: string;
  name: string;
  type: FieldType;
  description: string;
}

export type ThreadMode = "column" | "per_row" | "single";

export interface Mapping {
  /** envelope slot -> source column name */
  slots: Partial<Record<EnvSlot, string>>;
  /** columns kept as typed, declared fields */
  fields: DeclaredField[];
  threadMode: ThreadMode;
  channelName: string;
  channelDescription: string;
}

/* ------------------------------------------------------------- mapping defaults */

const NAME_HINTS: Record<EnvSlot, RegExp> = {
  happened_at: /\b(happened|occurred|created|sent|timestamp|datetime|date|time|at|ts|when)\b/i,
  external_id: /\b(external[_-]?id|guid|uuid|msg[_-]?id|message[_-]?id|\bid\b|key|slug)\b/i,
  content: /\b(content|text|body|message|note|comment|description|summary|caption)\b/i,
  thread: /\b(thread|conversation|chat|room|channel|topic|session|group)\b/i,
  author: /\b(author|user|sender|from|handle|name|account|speaker|by)\b/i,
  ended_at: /\b(end|ended|finish|until|stop)\b/i,
  lat: /\b(lat|latitude)\b/i,
  lon: /\b(lon|lng|long|longitude)\b/i,
};

function sanitizeFieldName(raw: string, taken: Set<string>): string {
  let s = raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!s || !/^[a-z]/.test(s)) s = `f_${s}`;
  s = s.slice(0, 60);
  let name = s;
  let i = 2;
  while (taken.has(name) || RESERVED.has(name)) name = `${s}_${i++}`.slice(0, 60);
  taken.add(name);
  return name;
}

// A subset of the reserved view column names (full list in the server's models.py).
const RESERVED = new Set([
  "id",
  "channel",
  "content",
  "fields",
  "kind",
  "score",
  "lat",
  "lon",
  "person_id",
  "version",
  "thread_id",
  "happened_at",
  "ended_at",
  "external_id",
  "author",
  "author_handle",
]);

/** Pick the best column for each slot, then keep the rest as declared fields. */
export function inferMapping(columns: InferredColumn[], suggestChannel = "import.my_source"): Mapping {
  const used = new Set<string>();
  const slots: Partial<Record<EnvSlot, string>> = {};

  const pick = (slot: EnvSlot, score: (c: InferredColumn) => number): void => {
    let best: InferredColumn | null = null;
    let bestScore = 0;
    for (const c of columns) {
      if (used.has(c.name)) continue;
      const s = score(c);
      if (s > bestScore) {
        best = c;
        bestScore = s;
      }
    }
    if (best && bestScore > 0) {
      slots[slot] = best.name;
      used.add(best.name);
    }
  };

  // Time: a timestamp-typed or time-ish column, boosted by name.
  pick("happened_at", (c) => (c.type === "timestamp" || c.timeish ? 2 : 0) + (NAME_HINTS.happened_at.test(c.name) ? 1 : 0));
  // Id: high uniqueness, boosted by an id-ish name.
  pick("external_id", (c) => (NAME_HINTS.external_id.test(c.name) ? 1 : 0) + c.uniqueness * (c.fill || 0.01));
  // Text: longest average string, boosted by a text-ish name.
  pick("content", (c) => (c.type === "text" ? 1 : 0) + Math.min(2, c.avgLen / 40) + (NAME_HINTS.content.test(c.name) ? 1.5 : 0));
  pick("thread", (c) => (NAME_HINTS.thread.test(c.name) ? 1 : 0));
  pick("author", (c) => (NAME_HINTS.author.test(c.name) ? 1 : 0));
  pick("ended_at", (c) => (c.type === "timestamp" && NAME_HINTS.ended_at.test(c.name) ? 2 : 0));
  pick("lat", (c) => (NAME_HINTS.lat.test(c.name) && (c.type === "number" || c.type === "integer") ? 2 : 0));
  pick("lon", (c) => (NAME_HINTS.lon.test(c.name) && (c.type === "number" || c.type === "integer") ? 2 : 0));

  const taken = new Set<string>();
  const fields: DeclaredField[] = columns
    .filter((c) => !used.has(c.name))
    .map((c) => ({
      column: c.name,
      name: sanitizeFieldName(c.name, taken),
      type: c.type,
      description: "",
    }));

  return {
    slots,
    fields,
    threadMode: slots.thread ? "column" : "per_row",
    channelName: suggestChannel,
    channelDescription: "",
  };
}

/** Columns currently bound to an envelope slot. */
export function usedColumns(m: Mapping): Set<string> {
  return new Set(Object.values(m.slots).filter((v): v is string => !!v));
}

/** Rebuild declared fields = every column not bound to a slot, preserving any
 *  custom name/type/description the user already set for a still-present column. */
export function recomputeFields(m: Mapping, columns: InferredColumn[]): DeclaredField[] {
  const used = usedColumns(m);
  const prev = new Map(m.fields.map((f) => [f.column, f]));
  const taken = new Set<string>();
  const fields: DeclaredField[] = [];
  for (const c of columns) {
    if (used.has(c.name)) continue;
    const existing = prev.get(c.name);
    if (existing) {
      taken.add(existing.name);
      fields.push(existing);
    } else {
      fields.push({ column: c.name, name: sanitizeFieldName(c.name, taken), type: c.type, description: "" });
    }
  }
  return fields;
}

/** Set or clear a slot, clearing any other slot that held the same column, then
 *  recompute the declared fields. */
export function withSlot(m: Mapping, slot: EnvSlot, column: string | null, columns: InferredColumn[]): Mapping {
  const slots: Partial<Record<EnvSlot, string>> = { ...m.slots };
  if (!column) {
    delete slots[slot];
  } else {
    for (const [k, v] of Object.entries(slots)) {
      if (v === column) delete slots[k as EnvSlot];
    }
    slots[slot] = column;
  }
  let threadMode = m.threadMode;
  if (slot === "thread") threadMode = column ? "column" : m.threadMode === "column" ? "per_row" : m.threadMode;
  const next: Mapping = { ...m, slots, threadMode };
  next.fields = recomputeFields(next, columns);
  return next;
}

/* --------------------------------------------------------------- validation */

export const CHANNEL_NAME_RE = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$/;

export interface MappingProblem {
  level: "error" | "warn";
  message: string;
}

export function validateMapping(m: Mapping): MappingProblem[] {
  const out: MappingProblem[] = [];
  if (!m.channelName || !CHANNEL_NAME_RE.test(m.channelName)) {
    out.push({ level: "error", message: "Channel name must be dot.separated lower_snake_case, e.g. import.my_source." });
  }
  if (m.channelName.length > 60) out.push({ level: "error", message: "Channel name must be 60 characters or fewer." });
  for (const slot of ENV_SLOTS) {
    if (slot.required && !m.slots[slot.slot]) {
      out.push({ level: "error", message: `Map a column to "${slot.label}".` });
    }
  }
  const names = new Set<string>();
  for (const f of m.fields) {
    if (names.has(f.name)) out.push({ level: "error", message: `Two fields map to "${f.name}".` });
    names.add(f.name);
    if (!/^[a-z][a-z0-9_]*$/.test(f.name)) {
      out.push({ level: "error", message: `Field name "${f.name}" must be lower_snake_case.` });
    }
  }
  if (m.threadMode === "single" && !m.slots.thread) {
    // fine — everything in one thread
  }
  return out;
}

/* ------------------------------------------------------------ channel + rows */

export function buildChannelFields(m: Mapping): Array<{ name: string; type: FieldType; description?: string }> {
  return m.fields.map((f) => ({ name: f.name, type: f.type, description: f.description || undefined }));
}

/** The channel registration body for POST /v1/channels. */
export function buildChannelBody(m: Mapping, plugin: string): Record<string, unknown> {
  return {
    name: m.channelName,
    plugin,
    description: m.channelDescription || `Imported from a file via the web interface`,
    fields: buildChannelFields(m),
    is_reference: false,
    is_outside: false,
    record_kind: "life_event",
  };
}

/** A ChannelMeta the lens engine can analyse, built from the mapping alone. */
export function buildChannelMeta(m: Mapping, rowCount: number): ChannelMeta {
  return {
    name: m.channelName,
    plugin: "webimport",
    description: m.channelDescription || null,
    record_kind: "life_event",
    is_reference: false,
    is_outside: false,
    whole_thread: false,
    embed: false,
    fields: buildChannelFields(m),
    view: channelViewName(m.channelName),
    rows: rowCount,
    threads: m.threadMode === "single" ? 1 : rowCount,
    first_at: null,
    last_at: null,
    last_ingest_at: null,
    with_geo: m.slots.lat && m.slots.lon ? rowCount : 0,
    with_ended_at: m.slots.ended_at ? rowCount : 0,
    with_images: 0,
    coverage: null,
  };
}

function toIso(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    const ms = value > 1e12 ? value : value * 1000;
    return new Date(ms).toISOString();
  }
  const s = String(value).trim();
  if (/^\d{10}$/.test(s)) return new Date(Number(s) * 1000).toISOString();
  if (/^\d{13}$/.test(s)) return new Date(Number(s)).toISOString();
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(s) ? `${s}T00:00:00Z` : s);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

function coerceField(value: unknown, type: FieldType): unknown {
  if (value === null || value === undefined || value === "") return null;
  switch (type) {
    case "integer": {
      const n = parseInt(String(value), 10);
      return Number.isNaN(n) ? null : n;
    }
    case "number": {
      const n = Number(value);
      return Number.isNaN(n) ? null : n;
    }
    case "boolean":
      return /^(true|t|yes|1)$/i.test(String(value).trim());
    case "json":
      if (typeof value === "object") return value;
      try {
        return JSON.parse(String(value));
      } catch {
        return String(value);
      }
    case "timestamp":
      return toIso(value);
    default:
      return String(value);
  }
}

export interface EnvelopeMessage {
  channel: string;
  external_id: string;
  thread: { external_id: string };
  happened_at: string;
  content: string;
  payload: Record<string, unknown>;
  ended_at?: string;
  author?: { system: string; kind: string; value: string };
  fields?: Record<string, unknown>;
  location?: { lat: number; lon: number };
}

export interface RowBuildResult {
  message: EnvelopeMessage | null;
  error: string | null;
}

/** Turn one source row into an ingest message, or report why it cannot be. */
export function rowToMessage(m: Mapping, row: Record<string, unknown>, index: number): RowBuildResult {
  const get = (slot: EnvSlot): unknown => {
    const col = m.slots[slot];
    return col ? row[col] : undefined;
  };
  const happened = toIso(get("happened_at"));
  if (!happened) return { message: null, error: "no valid timestamp for Happened at" };
  const content = get("content");
  const text = content === null || content === undefined ? "" : String(content).trim();
  if (!text) return { message: null, error: "empty text" };

  const extIdRaw = get("external_id");
  const external_id = extIdRaw === null || extIdRaw === undefined || String(extIdRaw).trim() === "" ? `row-${index}` : String(extIdRaw);

  let threadExt: string;
  if (m.threadMode === "single") threadExt = "all";
  else if (m.threadMode === "column" && m.slots.thread) {
    const t = get("thread");
    threadExt = t === null || t === undefined || String(t).trim() === "" ? `row-${index}` : String(t);
  } else {
    threadExt = external_id;
  }

  const fields: Record<string, unknown> = {};
  for (const f of m.fields) {
    const v = coerceField(row[f.column], f.type);
    if (v !== null) fields[f.name] = v;
  }

  const msg: EnvelopeMessage = {
    channel: m.channelName,
    external_id,
    thread: { external_id: threadExt },
    happened_at: happened,
    content: text,
    payload: { source_row: index },
  };
  const ended = toIso(get("ended_at"));
  if (ended) msg.ended_at = ended;
  const author = get("author");
  if (author !== null && author !== undefined && String(author).trim() !== "") {
    msg.author = { system: "webimport", kind: "account", value: String(author) };
  }
  if (Object.keys(fields).length) msg.fields = fields;
  const lat = m.slots.lat ? Number(get("lat")) : NaN;
  const lon = m.slots.lon ? Number(get("lon")) : NaN;
  if (!Number.isNaN(lat) && !Number.isNaN(lon)) msg.location = { lat, lon };

  return { message: msg, error: null };
}

export interface BuiltMessages {
  messages: EnvelopeMessage[];
  rejected: Array<{ index: number; error: string }>;
}

export function buildMessages(m: Mapping, rows: Array<Record<string, unknown>>): BuiltMessages {
  const messages: EnvelopeMessage[] = [];
  const rejected: Array<{ index: number; error: string }> = [];
  rows.forEach((row, i) => {
    const { message, error } = rowToMessage(m, row, i);
    if (message) messages.push(message);
    else rejected.push({ index: i, error: error ?? "rejected" });
  });
  return { messages, rejected };
}

/* -------------------------------------------- local preview rows for the lens */

/**
 * Rows shaped like the generated channel view (id, happened_at, thread_id,
 * content, author, the declared field columns), built from the sample with no
 * server round-trip. The lens engine profiles these to pick the real channel
 * page's lenses.
 */
export function previewRows(m: Mapping, built: BuiltMessages): { rows: Row[]; columns: SqlColumn[] } {
  const columns: SqlColumn[] = [
    { name: "id", type: "int8" },
    { name: "happened_at", type: "timestamptz" },
    { name: "thread_id", type: "int8" },
    { name: "content", type: "text" },
  ];
  if (m.slots.author) columns.push({ name: "author", type: "text" });
  if (m.slots.ended_at) columns.push({ name: "ended_at", type: "timestamptz" });
  if (m.slots.lat && m.slots.lon) columns.push({ name: "lat", type: "float8" }, { name: "lon", type: "float8" });
  for (const f of m.fields) columns.push({ name: f.name, type: pgType(f.type) });

  const threadIds = new Map<string, number>();
  const rows: Row[] = built.messages.map((msg, i) => {
    let tid = threadIds.get(msg.thread.external_id);
    if (tid === undefined) {
      tid = threadIds.size + 1;
      threadIds.set(msg.thread.external_id, tid);
    }
    const r: Row = {
      id: i + 1,
      happened_at: msg.happened_at,
      thread_id: tid,
      content: msg.content,
    };
    if (m.slots.author) r.author = msg.author?.value ?? null;
    if (m.slots.ended_at) r.ended_at = msg.ended_at ?? null;
    if (m.slots.lat && m.slots.lon) {
      r.lat = msg.location?.lat ?? null;
      r.lon = msg.location?.lon ?? null;
    }
    for (const f of m.fields) r[f.name] = (msg.fields?.[f.name] ?? null) as Row[string];
    return r;
  });
  return { rows, columns };
}

function pgType(t: FieldType): string {
  switch (t) {
    case "integer":
      return "int8";
    case "number":
      return "float8";
    case "boolean":
      return "bool";
    case "timestamp":
      return "timestamptz";
    case "json":
      return "jsonb";
    default:
      return "text";
  }
}

/** A stable signature of a file's column set, for saving/recalling the mapping. */
export function columnSignature(table: Pick<ParsedTable, "columns">): string {
  const sorted = [...table.columns].map((c) => c.toLowerCase().trim()).sort();
  let h = 2166136261;
  const s = sorted.join("\u0001");
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return `cols_${(h >>> 0).toString(36)}_${sorted.length}`;
}
