/*
 * Shared types for the Throughline web API. These mirror the SHARED CONTRACT in
 * docs/web/design.md and docs/web/backend.md. Where the backend engineer's work
 * is not yet on the dev server, `composeBootstrap` builds the same shape from
 * the older routes, so these types are the single source of truth for the UI.
 */

export interface TokenInfo {
  readonly id: number | string;
  readonly kind: "owner" | "agent" | "plugin";
  readonly name: string;
  readonly asker: string | null;
  readonly unrestricted: boolean;
  readonly can_write: boolean;
}

export interface EmbeddingsFeature {
  readonly available: boolean;
  readonly model: string | null;
}

export interface AssistantFeature {
  readonly provider: string | null;
  readonly model: string | null;
  readonly loopback: boolean;
  readonly default_tier: "schema" | "samples";
}

export interface Features {
  readonly web_state: boolean;
  readonly source_health: boolean;
  readonly embeddings: EmbeddingsFeature;
  readonly assistant: AssistantFeature;
  readonly egress: Record<string, unknown>;
}

export interface Totals {
  readonly records: number;
  readonly channels: number;
  readonly plugins: number;
  readonly people: number;
  readonly entities: number;
  readonly links: number;
  readonly threads: number;
  readonly vectors: number;
}

export interface PluginRow {
  readonly name: string;
  readonly version: string | null;
  readonly mode: string | null;
  readonly interval_s: number | null;
  readonly last_success_at: string | null;
  readonly last_error: string | null;
  readonly messages_ingested: number;
  readonly stale: boolean;
  readonly updated_at: string | null;
  // Source-health columns (present only once that migration is applied)
  readonly last_attempt_at?: string | null;
  readonly last_error_at?: string | null;
  readonly consecutive_failures?: number | null;
  readonly paused_at?: string | null;
  readonly paused_reason?: string | null;
  readonly open_outage?: unknown;
}

export interface FieldSpec {
  readonly name: string;
  readonly type: string;
  readonly description?: string | null;
}

export type CoverageStatus = "complete" | "partial" | "best_effort";

export interface ChannelCoverage {
  readonly status: CoverageStatus | string;
  readonly reasons: readonly string[];
  readonly covered_from: string | null;
  readonly covered_to: string | null;
  readonly open_gaps: number;
}

export interface ChannelRow {
  readonly name: string;
  readonly plugin: string;
  readonly description: string | null;
  readonly record_kind: string | null;
  readonly is_reference: boolean;
  readonly is_outside: boolean;
  readonly whole_thread: boolean;
  readonly embed: boolean;
  readonly fields: readonly FieldSpec[];
  readonly view: string; // the api.ch_* view name
  readonly rows: number;
  readonly threads: number;
  readonly first_at: string | null;
  readonly last_at: string | null;
  readonly last_ingest_at: string | null;
  readonly with_geo: boolean;
  readonly with_ended_at: boolean;
  readonly with_images: boolean;
  readonly coverage: ChannelCoverage;
}

export interface Bootstrap {
  readonly version: string;
  readonly now: string;
  readonly token: TokenInfo;
  readonly features: Features;
  readonly owner_person_id: number | null;
  readonly totals: Totals;
  readonly plugins: readonly PluginRow[];
  readonly channels: readonly ChannelRow[];
  /** true when this shape was composed locally from older routes. */
  readonly degraded?: boolean;
}

/* ---- SQL ----------------------------------------------------------------- */

export interface SqlColumn {
  readonly name: string;
  readonly type: string; // Postgres type name
}

export interface ExecutionEnvelope {
  readonly status: "complete" | "degraded" | "failed" | "approval_required" | string;
  readonly truncation: { reason: string; limit: number } | null;
  readonly completeness?: string;
  readonly stages?: ReadonlyArray<Record<string, unknown>>;
  readonly timings_ms?: Record<string, number>;
  readonly incomplete?: ReadonlyArray<Record<string, unknown>>;
  readonly warnings?: readonly string[];
  readonly warning_codes?: readonly string[];
  readonly trace_id?: string | null;
  readonly approval?: Record<string, unknown>;
  readonly taint?: Record<string, string>;
  readonly [k: string]: unknown;
}

export type Cell = string | number | boolean | null | Cell[] | { [k: string]: Cell };
export type Row = Record<string, Cell>;

export interface SqlResult<T = Row> {
  readonly rows: readonly T[];
  readonly columns: readonly SqlColumn[];
  readonly truncated: boolean;
  readonly rowCount: number;
  readonly execution: ExecutionEnvelope;
}

export interface SqlValidation {
  readonly valid: boolean;
  readonly error: string | null;
}

/* ---- UI state ------------------------------------------------------------ */

export type UiStateKind =
  | "prefs"
  | "colors"
  | "overrides"
  | "saved_query"
  | "board"
  | "import_mapping";

export interface UiStateEntry<V = unknown> {
  readonly key: string;
  readonly value: V;
  readonly updated_at: string | null;
}

/* ---- Arrivals (long poll) ------------------------------------------------ */

export interface ArrivalRow {
  readonly id: number | string;
  readonly channel: string;
  readonly thread_id: number | string | null;
  readonly happened_at: string;
  readonly ingested_at: string;
  readonly author: string | null;
  readonly person_id: number | null;
  readonly excerpt: string;
  readonly version: number;
  readonly settled: boolean;
}

export interface ReadRow {
  readonly at: string;
  readonly token: string | number;
  readonly asker: string | null;
  readonly statement: string;
  readonly row_count: number | null;
  readonly status: string;
}

export interface ArrivalsPage {
  readonly cursor: string;
  readonly rows: readonly ArrivalRow[];
  readonly counts: Readonly<Record<string, number>>;
  readonly sampled: boolean;
  readonly plugins: readonly PluginRow[];
  readonly reads: readonly ReadRow[];
  /** true when the dev server has no arrivals route yet. */
  readonly unsupported?: boolean;
}
