// Lens engine types. JSON in, JSON out at every stage:
// profile -> roles -> archetypes -> ranked lenses -> view spec -> SQL.
// Nothing in this directory touches React, the DOM or the network.

// ---------------------------------------------------------------- channel

export type FieldType = "text" | "integer" | "number" | "boolean" | "timestamp" | "json";

export interface FieldDecl {
  name: string;
  type: FieldType | (string & {});
  description?: string | null;
}

export interface ChannelCoverage {
  status: string;
  reasons: string[];
  covered_from: string | null;
  covered_to: string | null;
  open_gaps: number;
}

/** One entry of `channels` in GET /v1/web/bootstrap. */
export interface ChannelMeta {
  name: string;
  plugin: string;
  description: string | null;
  record_kind: string;
  is_reference: boolean;
  is_outside: boolean;
  whole_thread: boolean;
  embed?: boolean;
  fields: FieldDecl[];
  /** The per-channel view in schema api: "api.ch_" plus the channel name with dots as underscores. */
  view: string;
  rows: number;
  threads: number;
  first_at: string | null;
  last_at: string | null;
  last_ingest_at: string | null;
  with_geo: number;
  with_ended_at: number;
  with_images: number;
  coverage?: ChannelCoverage | null;
}

// ---------------------------------------------------------------- data access

export type Row = Record<string, unknown>;

export interface SqlColumn {
  name: string;
  /** PostgreSQL type name as the server reports it ("int8", "timestamptz", ...). */
  type: string;
}

export interface SqlResult {
  rows: Row[];
  columns: SqlColumn[];
  truncated: boolean;
}

export interface RunSqlOptions {
  row_cap?: number;
  deadline_ms?: number;
  signal?: unknown;
}

/** Injected data access. The engine never fetches by itself. */
export type RunSql = (statement: string, opts?: RunSqlOptions) => Promise<SqlResult>;

// ---------------------------------------------------------------- profile

export type ColumnKind =
  | "text"
  | "integer"
  | "number"
  | "boolean"
  | "timestamp"
  | "date"
  | "json"
  | "interval"
  | "unknown";

export interface NumericStats {
  min: number;
  max: number;
  avg: number;
  stddev: number | null;
  /** Every observed value is a whole number. */
  integral: boolean;
}

export interface TextStats {
  avg_len: number;
  max_len: number;
  /** Most frequent values, clipped to 80 characters, most frequent first. */
  top: { value: string; n: number }[];
  /** Share of sampled values that read as an ISO date (0..1). */
  date_like: number;
  /** Share of sampled values that start with http:// or https:// (0..1). */
  url_like: number;
}

export interface TimeStats {
  min: string | null;
  max: string | null;
  /** Share of values later than the moment the profile was taken (0..1). */
  future_share: number;
  /** Mean of (value - happened_at) in seconds; null when unknown. */
  lead_s: number | null;
}

export type JsonShape = "array_scalar" | "array_object" | "object" | "scalar" | "mixed";

export interface JsonStats {
  shape: JsonShape;
  avg_items: number | null;
  /** Keys seen in sampled objects (or in the objects of sampled arrays), most common first. */
  keys: string[];
}

export interface FieldProfile {
  name: string;
  kind: ColumnKind;
  /** Declared by the channel (as opposed to an envelope or result column). */
  declared: boolean;
  description: string;
  pg_type?: string;
  /** Rows whose values were examined for this field's statistics; 0 = shape only. */
  observed: number;
  /** Non-null count over the whole channel (exact) or the result. */
  present: number | null;
  /** present / rows, 0..1. */
  fill: number | null;
  distinct: number | null;
  numeric?: NumericStats;
  text?: TextStats;
  boolean?: { true_share: number };
  time?: TimeStats;
  json?: JsonStats;
}

export interface Envelope {
  rows: number;
  first_at: string | null;
  last_at: string | null;
  last_ingest_at: string | null;
  threads: number;
  people: number;
  authors: number;
  with_ended_at: number;
  with_geo: number;
  /** Records that carry their own position (not one inferred from a track); null when unknown. */
  with_geo_source: number | null;
  with_images: number;
  avg_content_chars: number | null;
  max_content_chars: number | null;
  content_empty: number | null;
  max_version: number | null;
  future_rows: number | null;
  /** Distinct hours of day (UTC) that carry records, 0..24; null when unknown. */
  hours_used: number | null;
  min_duration_s: number | null;
  avg_duration_s: number | null;
  max_duration_s: number | null;
}

export type CadenceClass = "none" | "daily" | "regular" | "bursty" | "sparse" | "continuous";

export interface Cadence {
  class: CadenceClass;
  span_days: number;
  active_days: number;
  /** active_days / span_days. */
  coverage: number;
  per_active_day_median: number;
  per_active_day_max: number;
  /** Median gap between consecutive active days, in days. */
  median_gap_days: number;
  /** Records per day, clipped to the most recent 400 active days, oldest first. */
  days: { day: string; n: number }[];
}

export type ThreadClass = "none" | "single" | "one_per_record" | "few_large" | "many_small" | "mixed";

export interface ThreadShape {
  class: ThreadClass;
  count: number;
  rows_per_thread: number;
  median_size: number;
  max_size: number;
  /** Largest thread's share of all records. */
  top_share: number;
  /** Median distinct speakers among the largest threads. */
  median_speakers: number;
}

export interface ChannelProfile {
  v: 1;
  channel: string;
  /**
   * "observed": gathered by SQL. "envelope": only the counts bootstrap carries are known,
   * no field statistics yet. "declared": no records; predicted from declared fields and flags.
   */
  source: "observed" | "envelope" | "declared";
  rows: number;
  /** Value statistics ran on an `id % k` sample; counts stay exact. */
  sampled: boolean;
  sample_modulo: number;
  envelope: Envelope;
  fields: FieldProfile[];
  cadence: Cadence;
  threads: ThreadShape;
  /** Spread sample of rows, declared fields flattened beside the envelope columns. */
  sample: Row[];
  /** Identity of the data this was computed from, for caching. */
  cache_key: string;
}

export interface ResultProfile {
  v: 1;
  rows: number;
  truncated: boolean;
  columns: FieldProfile[];
  /** Column sets whose value combinations are unique across the result (singles and pairs). */
  unique: string[][];
  sample: Row[];
}

// ---------------------------------------------------------------- roles

export type RoleId =
  | "time.start"
  | "time.end"
  | "time.offset"
  | "time.bookkeeping"
  | "time.deadline"
  | "date.text"
  | "duration"
  | "distance"
  | "mass"
  | "percent"
  | "probability"
  | "score"
  | "count"
  | "currency"
  | "geo.lat"
  | "geo.lon"
  | "geo.label"
  | "identifier"
  | "group_key"
  | "bookkeeping"
  | "category"
  | "category.speaker"
  | "category.status"
  | "title"
  | "body"
  | "url"
  | "actor.label"
  | "flag"
  | "flag.tombstone"
  | "json.tags"
  | "json.items"
  | "json.object"
  | "measure";

export const ROLE_IDS: readonly RoleId[] = [
  "time.start", "time.end", "time.offset", "time.bookkeeping", "time.deadline", "date.text",
  "duration", "distance", "mass", "percent", "probability", "score", "count", "currency",
  "geo.lat", "geo.lon", "geo.label", "identifier", "group_key", "bookkeeping", "category",
  "category.speaker", "category.status", "title", "body", "url", "actor.label", "flag",
  "flag.tombstone", "json.tags", "json.items", "json.object", "measure",
];

export interface Role {
  field: string;
  role: RoleId;
  /** 0..1 */
  confidence: number;
  /** Short English reasons a UI can show verbatim. */
  evidence: string[];
  unit?: string;
  /** The partner of a pair (start/end, lat/lon, offset start/end). */
  pair?: string;
  /** Value domain mined from the description or observed values. */
  domain?: string[];
  /** Family id ("sleep_contrib_*") when the field belongs to one. */
  family?: string;
  /** Identifier or bookkeeping: kept out of charts by default. */
  plumbing: boolean;
  /** Hidden by the owner. */
  hidden?: boolean;
  /** Role or unit came from an owner override. */
  overridden?: boolean;
}

export type RoleMap = Record<string, Role>;

export interface FieldFamily {
  /** "sleep_contrib_*" or "*_sleep_s". */
  id: string;
  kind: "prefix" | "suffix";
  members: string[];
  role: RoleId;
  unit?: string;
}

export interface PartOfWhole {
  parts: string[];
  /** A field name, or a constant such as 100 when the parts sum to a fixed total. */
  whole: string | number;
  unit?: string;
  confidence: number;
  /** True when the sum was checked on sampled rows; false when predicted from names. */
  tested: boolean;
  evidence: string[];
}

export interface ChannelFamily {
  id: string;
  channels: string[];
  /** Field names every member declares. */
  shared_fields: string[];
}

// ---------------------------------------------------------------- archetypes

export type ArchetypeId =
  | "conversation"
  | "transcript"
  | "measurement"
  | "interval"
  | "geo_track"
  | "document"
  | "feed_item"
  | "gauge"
  | "snapshot"
  | "registry";

export const ARCHETYPE_IDS: readonly ArchetypeId[] = [
  "conversation", "transcript", "measurement", "interval", "geo_track",
  "document", "feed_item", "gauge", "snapshot", "registry",
];

export interface ArchetypeScore {
  archetype: ArchetypeId;
  /** 0..1 */
  score: number;
  evidence: string[];
}

export interface Archetypes {
  primary: ArchetypeId;
  /** Every other archetype scoring above 0.5, best first. */
  secondary: ArchetypeId[];
  /** All ten, best first. */
  scores: ArchetypeScore[];
  /** True when nothing was observed and the scores come from declared shape alone. */
  predicted: boolean;
}

// ---------------------------------------------------------------- lenses

export type LensGroup = "time" | "distribution" | "structure" | "space" | "relation" | "reading";

export type LensId =
  | "spine" | "timeseries" | "small_multiples" | "calendar_heat" | "weekday_hour"
  | "interval_clock" | "gantt" | "sawtooth"
  | "histogram" | "top_values" | "composition" | "scatter" | "strip"
  | "thread_tapestry" | "speaker_share" | "reply_latency" | "status_matrix" | "tags" | "json_tree"
  | "track_map" | "pin_map" | "dwell_rank"
  | "ego_graph" | "link_matrix"
  | "reader" | "table" | "stat";

export const LENS_IDS: readonly LensId[] = [
  "spine", "timeseries", "small_multiples", "calendar_heat", "weekday_hour",
  "interval_clock", "gantt", "sawtooth",
  "histogram", "top_values", "composition", "scatter", "strip",
  "thread_tapestry", "speaker_share", "reply_latency", "status_matrix", "tags", "json_tree",
  "track_map", "pin_map", "dwell_rank",
  "ego_graph", "link_matrix",
  "reader", "table", "stat",
];

export type ParamType =
  | "text" | "int" | "number" | "bool" | "timestamp" | "date" | "interval"
  | "text[]" | "int[]" | "number[]" | "grain" | "tz";

export interface ParamValue {
  type: ParamType;
  value: unknown;
}

export type Grain = "minute" | "hour" | "day" | "week" | "month" | "quarter" | "year";

export const GRAINS: readonly Grain[] = ["minute", "hour", "day", "week", "month", "quarter", "year"];

/** throughline.view/1: a lens, one read-only SELECT and the column bindings. */
export interface ViewSpec {
  v: 1;
  title: string;
  /** One short sentence: why this picture for this data. */
  why: string;
  lens: LensId;
  source: {
    /**
     * One SELECT over schema api. May reference :from, :to, :tz, :person, :channel, :grain
     * and the panel-local parameters listed in `params`.
     */
    sql: string;
    row_cap: number;
    /** Panel-local parameters with their defaults. */
    params?: Record<string, ParamValue>;
  };
  /** Lens channel -> result column (or columns). The channels of each lens are in LENS_CATALOG. */
  encode: Record<string, string | string[]>;
  /** Renderer hints that are not columns: units, grain bounds, the reader variant, a value domain. */
  options?: Record<string, unknown>;
  /** Where a click on a mark leads: the reader filtered by `key`. */
  drill?: { to: "reader" | "record"; key: string };
  provenance: {
    by: "engine" | "agent" | "user";
    channel?: string;
    sampled: boolean;
    /** Built from declared fields only; the channel has no records yet. */
    predicted?: boolean;
    /** For a view of a SQL result: the statement as written, or wrapped in an aggregate. */
    statement?: "as_is" | "wrapped";
    /** Declared fields the view reads. */
    fields?: string[];
  };
}

export interface LensCandidate {
  spec: ViewSpec;
  /** 0..1, comparable within one ranking only. */
  score: number;
  why: string;
}

/** One built picture before ranking. */
export interface LensVariant {
  spec: ViewSpec;
  /** 0..1: how much the picture says (0 = degenerate). */
  information: number;
  /** 0..1: rows scanned and returned. */
  cost: number;
  /** Optional per-variant fitness that replaces the lens-level one. */
  fitness?: number;
}

export interface LensFit {
  /** 0..1; 0 means the lens does not apply. */
  fitness: number;
  why: string;
}

// ---------------------------------------------------------------- overrides

export interface FieldOverride {
  role?: RoleId;
  unit?: string;
  hidden?: boolean;
}

/** UI state of kind "overrides", keyed by channel name. */
export interface ChannelOverrides {
  fields?: Record<string, FieldOverride>;
  /** Lens ids, or "lens:field" keys, shown first in this order. */
  pinned?: string[];
  hidden?: string[];
}

// ---------------------------------------------------------------- schema

const str = { type: "string" } as const;

/** JSON Schema (draft-07) of throughline.view/1, for ajv and for the helper agent. */
export const VIEW_SPEC_SCHEMA = {
  $schema: "http://json-schema.org/draft-07/schema#",
  $id: "throughline.view/1",
  title: "Throughline view spec",
  type: "object",
  additionalProperties: false,
  required: ["v", "title", "why", "lens", "source", "encode", "provenance"],
  properties: {
    v: { const: 1 },
    title: { type: "string", minLength: 1, maxLength: 160 },
    why: { type: "string", maxLength: 400 },
    lens: { enum: [...LENS_IDS] },
    source: {
      type: "object",
      additionalProperties: false,
      required: ["sql", "row_cap"],
      properties: {
        sql: { type: "string", minLength: 1 },
        row_cap: { type: "integer", minimum: 1, maximum: 10000 },
        params: {
          type: "object",
          propertyNames: { pattern: "^[A-Za-z_][A-Za-z0-9_]*$" },
          additionalProperties: {
            type: "object",
            additionalProperties: false,
            required: ["type", "value"],
            properties: {
              type: {
                enum: [
                  "text", "int", "number", "bool", "timestamp", "date", "interval",
                  "text[]", "int[]", "number[]", "grain", "tz",
                ],
              },
              value: {},
            },
          },
        },
      },
    },
    encode: {
      type: "object",
      propertyNames: { pattern: "^[a-z_][a-z0-9_]*$" },
      additionalProperties: {
        anyOf: [
          { type: "string", minLength: 1 },
          { type: "array", items: { type: "string", minLength: 1 }, minItems: 1, maxItems: 24 },
        ],
      },
    },
    options: { type: "object" },
    drill: {
      type: "object",
      additionalProperties: false,
      required: ["to", "key"],
      properties: { to: { enum: ["reader", "record"] }, key: str },
    },
    provenance: {
      type: "object",
      additionalProperties: false,
      required: ["by", "sampled"],
      properties: {
        by: { enum: ["engine", "agent", "user"] },
        channel: str,
        sampled: { type: "boolean" },
        predicted: { type: "boolean" },
        statement: { enum: ["as_is", "wrapped"] },
        fields: { type: "array", items: str },
      },
    },
  },
} as const;
