/*
 * The completion catalogue: everything the editor can suggest, assembled from the
 * live installation and nothing hard-coded.
 *
 *   - views            every channel's api.ch_* view (from bootstrap channels[].view)
 *   - declared fields  with their type and description (bootstrap channels[].fields)
 *   - typed columns    the real columns of a view, fetched lazily via validate_only
 *                      (SELECT * FROM <view> LIMIT 0) and merged in by `columns`
 *   - functions        api.* functions with signature + description (GET /v1/describe)
 *   - builtins         the SQL allow-list the server runs (lens EXTENDED_BUILTINS)
 *
 * This module is pure data assembly; `completion.ts` turns a catalogue into
 * CodeMirror completions. Both are unit-tested without a browser.
 */
import { EXTENDED_BUILTINS } from "@/lens";
import type { Bootstrap, ChannelRow } from "@/api/types";

export interface CatField {
  name: string;
  type: string;
  description?: string | null;
}

export interface CatView {
  /** schema-qualified: "api.ch_chat_sms" */
  view: string;
  channel: string;
  plugin: string;
  rows: number;
  fields: CatField[];
  record_kind: string | null;
}

export interface CatColumn {
  name: string;
  type: string;
}

export interface CatFunction {
  /** bare name, e.g. "search" */
  name: string;
  signature: string;
  description: string;
  example?: string;
  setReturning: boolean;
}

export interface Catalogue {
  views: CatView[];
  functions: CatFunction[];
  builtins: readonly string[];
  /** Typed columns per view name, filled lazily. */
  columns: Record<string, CatColumn[]>;
}

/** Raw function entry as GET /v1/describe?subject=functions returns it. */
export interface DescribeFunction {
  name: string;
  signature?: string;
  description?: string;
  example?: string;
  kind?: string;
  [k: string]: unknown;
}

const SETOF = /\bRETURNS\s+(SETOF|TABLE)\b/i;

export function buildCatalogue(
  boot: Pick<Bootstrap, "channels"> | null | undefined,
  functions: DescribeFunction[] | null | undefined,
  columns: Record<string, CatColumn[]> = {},
): Catalogue {
  const views: CatView[] = [];
  for (const c of boot?.channels ?? []) {
    if (!c.view) continue;
    views.push({
      view: c.view,
      channel: c.name,
      plugin: c.plugin,
      rows: Number((c as ChannelRow).rows) || 0,
      record_kind: c.record_kind ?? null,
      fields: (c.fields ?? []).map((f) => ({ name: f.name, type: f.type, description: f.description })),
    });
  }
  views.sort((a, b) => b.rows - a.rows || a.channel.localeCompare(b.channel));

  const fns: CatFunction[] = (functions ?? [])
    .filter((f) => typeof f.name === "string")
    .map((f) => ({
      name: f.name,
      signature: f.signature ?? `${f.name}(…)`,
      description: f.description ?? "",
      example: f.example,
      setReturning: SETOF.test(f.signature ?? ""),
    }));
  fns.sort((a, b) => a.name.localeCompare(b.name));

  return { views, functions: fns, builtins: EXTENDED_BUILTINS, columns };
}

/** The bare view name without the "api." schema, for display. */
export function shortViewName(view: string): string {
  return view.replace(/^api\./, "");
}

/** Find a view by its channel name. */
export function viewForChannel(cat: Catalogue, channel: string): CatView | undefined {
  return cat.views.find((v) => v.channel === channel);
}

/** Known columns of a view: fetched typed columns if present, else declared fields. */
export function columnsForView(cat: Catalogue, view: string): CatColumn[] {
  const typed = cat.columns[view];
  if (typed && typed.length) return typed;
  const v = cat.views.find((x) => x.view === view);
  return v ? v.fields.map((f) => ({ name: f.name, type: f.type })) : [];
}

/**
 * The small set of standard envelope columns every api.ch_* view carries, so
 * completion offers happened_at/content/etc. even before typed columns load. These
 * are the public columns the record contract guarantees (map-http.md §api.messages),
 * minus the internal plumbing a reader rarely wants to type.
 */
export const ENVELOPE_COLUMNS: CatColumn[] = [
  { name: "id", type: "int8" },
  { name: "happened_at", type: "timestamptz" },
  { name: "ended_at", type: "timestamptz" },
  { name: "ingested_at", type: "timestamptz" },
  { name: "channel", type: "text" },
  { name: "thread_id", type: "int8" },
  { name: "thread_title", type: "text" },
  { name: "author_handle", type: "text" },
  { name: "display_name", type: "text" },
  { name: "person_id", type: "int8" },
  { name: "content", type: "text" },
  { name: "fields", type: "jsonb" },
  { name: "lat", type: "float8" },
  { name: "lon", type: "float8" },
];
