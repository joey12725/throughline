// Shared context and helpers for the lens modules.

import { type ShapeFacts, shapeFacts } from "../archetypes";
import { fieldFamilies, isCategory, isMeasure, partsOfWhole, tokens } from "../roles";
import {
  type Capabilities,
  EXTENDED,
  type SqlFns,
  channelView,
  quoteIdent,
  sqlFns,
  wrapStatement,
} from "../sql";
import type {
  ArchetypeId,
  Archetypes,
  ChannelMeta,
  ChannelProfile,
  FieldFamily,
  FieldProfile,
  LensFit,
  LensGroup,
  LensId,
  LensVariant,
  ParamValue,
  PartOfWhole,
  ResultProfile,
  Role,
  RoleId,
  RoleMap,
  ViewSpec,
} from "../types";

export const clamp = (x: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, x));

export interface ChannelCtx {
  kind: "channel";
  meta: ChannelMeta;
  profile: ChannelProfile;
  roles: RoleMap;
  archetypes: Archetypes;
  facts: ShapeFacts;
  /** The relation to select from. */
  view: string;
  caps: Capabilities;
  fns: SqlFns;
  families: FieldFamily[];
  wholes: PartOfWhole[];
  /** Channels with the same field signature, this one included; empty when it has no siblings. */
  siblings: ChannelMeta[];
  rows: number;
  /** No rows yet: everything is predicted from declared shape. */
  predicted: boolean;
  extent: [string | null, string | null];
}

export interface ResultCtx {
  kind: "result";
  profile: ResultProfile;
  roles: RoleMap;
  statement: string;
  /** "(<statement>) AS q", or null when the statement cannot be wrapped soundly. */
  wrapped: string | null;
  caps: Capabilities;
  fns: SqlFns;
  rows: number;
}

export interface LensDef {
  id: LensId;
  group: LensGroup;
  /** What the lens needs, in words the field inspector can show. */
  needs: string[];
  /** One line for the lens picker and the helper agent. */
  summary: string;
  /** The encode channels and what each binds. */
  channels: Record<string, string>;
  /** Archetype affinity, 0..1; archetypes not listed get 0.3. */
  affinity: Partial<Record<ArchetypeId, number>>;
  fit(profile: ChannelProfile, roles: RoleMap, archetypes: Archetypes): LensFit;
  build(ctx: ChannelCtx): LensVariant[];
  buildResult?(ctx: ResultCtx): LensVariant[];
}

export interface ContextOptions {
  caps?: Capabilities;
  siblings?: ChannelMeta[];
}

export function channelContext(
  meta: ChannelMeta,
  profile: ChannelProfile,
  roles: RoleMap,
  archetypes: Archetypes,
  opts: ContextOptions = {},
): ChannelCtx {
  const caps = opts.caps ?? EXTENDED;
  const siblings = (opts.siblings ?? []).some((s) => s.name === meta.name) && (opts.siblings ?? []).length > 1 ? (opts.siblings as ChannelMeta[]) : [];
  return {
    kind: "channel",
    meta,
    profile,
    roles,
    archetypes,
    facts: shapeFacts(meta, profile, roles),
    view: channelView(meta),
    caps,
    fns: sqlFns(caps),
    families: fieldFamilies(profile.fields, roles),
    wholes: partsOfWhole(profile, roles),
    siblings,
    rows: profile.envelope.rows,
    predicted: profile.source === "declared" || profile.envelope.rows === 0,
    extent: [profile.envelope.first_at, profile.envelope.last_at],
  };
}

export function resultContext(profile: ResultProfile, roles: RoleMap, statement: string, caps: Capabilities = EXTENDED): ResultCtx {
  return {
    kind: "result",
    profile,
    roles,
    statement,
    wrapped: wrapStatement(statement),
    caps,
    fns: sqlFns(caps),
    rows: profile.rows,
  };
}

// ---------------------------------------------------------------- roles and fields

export function liveRoles(roles: RoleMap): Role[] {
  return Object.values(roles).filter((r) => !r.hidden);
}

export function rolesOf(roles: RoleMap, ...ids: RoleId[]): Role[] {
  return liveRoles(roles)
    .filter((r) => ids.includes(r.role))
    .sort((a, b) => b.confidence - a.confidence);
}

export function fieldOf(profile: { fields: FieldProfile[] } | ResultProfile, name: string): FieldProfile | undefined {
  const list = "columns" in profile ? profile.columns : profile.fields;
  return list.find((f) => f.name === name);
}

/** Quoted column reference of a field. */
export const col = (name: string) => quoteIdent(name);

const UNIT_TAILS = new Set(["s", "ms", "m", "kg", "pct", "minutes", "hours", "meters", "metres", "lb"]);

/** "total_sleep_s" -> "Total sleep". */
export function label(name: string): string {
  const t = tokens(name);
  const body = t.length > 1 && UNIT_TAILS.has(t[t.length - 1] as string) ? t.slice(0, -1) : t;
  const s = body.join(" ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function lower(name: string): string {
  const s = label(name);
  return s.charAt(0).toLowerCase() + s.slice(1);
}

/** Known number of distinct values of a field: observed, else the mined domain, else null. */
export function distinctOf(profile: { fields: FieldProfile[] } | ResultProfile, role: Role): number | null {
  const f = fieldOf(profile, role.field);
  if (f && f.observed >= 5 && f.distinct !== null) return f.distinct;
  if (f?.kind === "boolean") return 2;
  return role.domain ? role.domain.length : null;
}

export function fillOf(profile: { fields: FieldProfile[] } | ResultProfile, name: string): number {
  const f = fieldOf(profile, name);
  return f?.fill ?? 0.8;
}

const MEASURE_WEIGHT: Partial<Record<RoleId, number>> = {
  score: 1, percent: 0.95, measure: 0.9, mass: 0.9, probability: 0.9, currency: 0.9, duration: 0.85, distance: 0.8, count: 0.75,
};

/** How much a quantity deserves a chart of its own: filled, varying, a headline rather than a part. */
export function measureInterest(profile: { fields: FieldProfile[] } | ResultProfile, role: Role): number {
  const f = fieldOf(profile, role.field);
  const fill = f?.fill ?? 0.8;
  let vary = 0.7;
  if (f?.numeric && f.observed >= 5) vary = f.numeric.max > f.numeric.min ? 1 : 0;
  // Two or three distinct values is a setting, not a series.
  if (f && f.observed >= 20 && f.distinct !== null && f.distinct <= 3) vary *= 0.3;
  const t = tokens(role.field);
  const headline = 1 / (1 + 0.07 * Math.max(0, t.length - 2));
  const part = t.includes("contrib") || t.includes("contributor") ? 0.55 : 1;
  return clamp(fill, 0.05, 1) * vary * (MEASURE_WEIGHT[role.role] ?? 0.6) * headline * part * (0.5 + 0.5 * role.confidence);
}

/** Quantities worth charting, most interesting first. */
export function rankedMeasures(profile: { fields: FieldProfile[] } | ResultProfile, roles: RoleMap): Role[] {
  return liveRoles(roles)
    .filter((r) => isMeasure(r.role))
    .map((r) => ({ r, s: measureInterest(profile, r) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s)
    .map((x) => x.r);
}

const LINEAGE = new Set(["provider", "source", "origin", "transport", "scorer", "instance", "environment", "account", "platform"]);

/** How good a field is as a small set of groups (colour, facet, bar chart). 0 = useless. */
export function categoryInterest(profile: { fields: FieldProfile[] } | ResultProfile, role: Role, max = 30): number {
  const d = distinctOf(profile, role);
  const fill = fillOf(profile, role.field);
  if (d !== null && d <= 1) return 0;
  const f = fieldOf(profile, role.field);
  // A different value on nearly every record is a name, not a grouping.
  const names = f && f.observed >= 5 && d !== null && d >= 0.8 * f.observed ? 0.1 : 1;
  let shape = 0.6;
  if (d !== null) shape = d <= max ? 1 : d <= max * 10 ? 0.5 : 0.25;
  const kind =
    role.role === "category.status" || role.role === "category.speaker" ? 1
    : role.role === "category" ? 0.9
    : role.role === "group_key" ? 0.7
    : role.role === "flag.tombstone" ? 0.3
    : 0.6;
  // Where a record came from (provider, source, transport) says less than what it is.
  const lineage = role.role === "category" && tokens(role.field).some((t) => LINEAGE.has(t)) ? 0.6 : 1;
  return clamp(fill, 0.05, 1) * shape * kind * lineage * names * (0.5 + 0.5 * role.confidence);
}

export function rankedCategories(profile: { fields: FieldProfile[] } | ResultProfile, roles: RoleMap, include: RoleId[] = []): Role[] {
  return liveRoles(roles)
    .filter((r) => isCategory(r.role) || include.includes(r.role))
    .map((r) => ({ r, s: categoryInterest(profile, r) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s)
    .map((x) => x.r);
}

/** A category with few enough values to colour by (at most `max`), or undefined. */
export function colourField(profile: { fields: FieldProfile[] } | ResultProfile, roles: RoleMap, max = 8, exclude: string[] = []): Role | undefined {
  return rankedCategories(profile, roles).find((r) => {
    if (exclude.includes(r.field) || r.role === "flag.tombstone") return false;
    const d = distinctOf(profile, r);
    return d !== null && d >= 2 && d <= max;
  });
}

// ---------------------------------------------------------------- cost

/** Cost of scanning `rows` rows `passes` times and returning `returned` rows, 0..0.6. */
export function costOf(rows: number, returned: number, passes = 1): number {
  const scanned = Math.max(1, rows) * passes;
  const scan = scanned <= 20000 ? 0 : Math.min(1, Math.log(scanned / 20000) / Math.log(500)) * 0.25;
  const ret = Math.min(1, Math.max(0, returned) / 10000) * 0.2;
  return clamp(scan + ret, 0, 0.6);
}

// ---------------------------------------------------------------- spec builders

export interface SpecInit {
  title: string;
  why: string;
  sql: string;
  row_cap: number;
  encode: Record<string, string | string[]>;
  params?: Record<string, ParamValue>;
  options?: Record<string, unknown>;
  fields?: string[];
  sampled?: boolean;
  drill?: ViewSpec["drill"];
}

export function channelSpec(ctx: ChannelCtx, lens: LensId, init: SpecInit): ViewSpec {
  const options: Record<string, unknown> = { ...(init.options ?? {}) };
  if (/:grain\b/.test(init.sql) && options.extent === undefined && (ctx.extent[0] || ctx.extent[1])) options.extent = ctx.extent;
  const spec: ViewSpec = {
    v: 1,
    title: init.title,
    why: ctx.predicted ? `${init.why} (predicted from the declared fields; awaiting the first record)` : init.why,
    lens,
    source: { sql: init.sql, row_cap: init.row_cap },
    encode: init.encode,
    provenance: { by: "engine", channel: ctx.meta.name, sampled: init.sampled ?? false },
  };
  if (init.params && Object.keys(init.params).length > 0) spec.source.params = init.params;
  if (Object.keys(options).length > 0) spec.options = options;
  if (init.drill) spec.drill = init.drill;
  if (ctx.predicted) spec.provenance.predicted = true;
  if (init.fields && init.fields.length > 0) spec.provenance.fields = init.fields;
  return spec;
}

export function resultSpec(lens: LensId, mode: "as_is" | "wrapped", init: SpecInit): ViewSpec {
  const spec: ViewSpec = {
    v: 1,
    title: init.title,
    why: init.why,
    lens,
    source: { sql: init.sql, row_cap: init.row_cap },
    encode: init.encode,
    provenance: { by: "engine", sampled: false, statement: mode },
  };
  if (init.params && Object.keys(init.params).length > 0) spec.source.params = init.params;
  if (init.options && Object.keys(init.options).length > 0) spec.options = init.options;
  if (init.fields && init.fields.length > 0) spec.provenance.fields = init.fields;
  return spec;
}

/** WHERE clause from the conditions that are present. */
export function where(...conditions: (string | null | undefined | false)[]): string {
  const xs = conditions.filter((c): c is string => typeof c === "string" && c.length > 0);
  return xs.length ? ` WHERE ${xs.join(" AND ")}` : "";
}

export const SCOPE = "happened_at >= :from AND happened_at < :to";

/** Row count to expect inside the scope when nothing better is known. */
export function estRows(ctx: ChannelCtx): number {
  return Math.max(0, ctx.rows);
}

export function int(value: number): ParamValue {
  return { type: "int", value: Math.round(value) };
}

export function number(value: number): ParamValue {
  return { type: "number", value };
}

export function unitOptions(roles: RoleMap, fields: string[]): Record<string, unknown> {
  const units: Record<string, string> = {};
  for (const f of fields) {
    const u = roles[f]?.unit;
    if (u) units[f] = u;
  }
  return Object.keys(units).length ? { units } : {};
}

/** Result columns of one of the given kinds, in result order. */
export function columnsOf(ctx: ResultCtx, ...kinds: FieldProfile["kind"][]): FieldProfile[] {
  return ctx.profile.columns.filter((c) => kinds.includes(c.kind));
}

export function isUnique(ctx: ResultCtx, ...names: string[]): boolean {
  const want = [...names].sort().join("\u0001");
  return ctx.profile.unique.some((u) => [...u].sort().join("\u0001") === want);
}

/** The time column of a result: a time.start role, else the first timestamp or date. */
export function resultTime(ctx: ResultCtx): FieldProfile | undefined {
  const cols = columnsOf(ctx, "timestamp", "date");
  return cols.find((c) => ctx.roles[c.name]?.role === "time.start") ?? cols.find((c) => !["time.end", "time.bookkeeping", "time.deadline"].includes(ctx.roles[c.name]?.role ?? ""));
}

/** Numeric result columns that are quantities (not ids, not coordinates). */
export function resultMeasures(ctx: ResultCtx): FieldProfile[] {
  return columnsOf(ctx, "integer", "number").filter((c) => {
    const r = ctx.roles[c.name];
    return r !== undefined && isMeasure(r.role) && !r.hidden;
  });
}

/** Text-like result columns that group rows. */
export function resultCategories(ctx: ResultCtx): FieldProfile[] {
  return ctx.profile.columns.filter((c) => {
    const r = ctx.roles[c.name];
    if (!r || r.hidden) return false;
    if (c.kind === "boolean") return true;
    if (c.kind !== "text" && c.kind !== "integer") return false;
    return isCategory(r.role) || r.role === "group_key" || r.role === "actor.label" || r.role === "geo.label" || r.role === "title";
  });
}
