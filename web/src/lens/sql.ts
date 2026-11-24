// SQL rendering for the lens engine: literals, identifiers, parameter substitution,
// the builtin capability table, time grains and the view-spec compiler.
//
// POST /v1/sql takes one statement and no bind parameters, so every value is rendered
// here as a typed, escaped literal. The server's validator and the read-only reader role
// remain the safety boundary; this module only has to be correct, not trusted.

import type { ChannelMeta, Grain, ParamType, ParamValue, ViewSpec } from "./types";
import { GRAINS } from "./types";

export class LensSqlError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LensSqlError";
  }
}

// ---------------------------------------------------------------- identifiers

// Reserved and column-name keywords of PostgreSQL: a column called `to`, `end` or
// `position` has to be quoted to be read back.
const KEYWORDS = new Set(
  (
    "all analyse analyze and any array as asc asymmetric authorization between bigint binary bit boolean both " +
    "case cast char character check coalesce collate collation column concurrently constraint create cross " +
    "current_catalog current_date current_role current_schema current_time current_timestamp current_user " +
    "dec decimal default deferrable desc distinct do else end except exists extract false fetch float for " +
    "foreign freeze from full grant greatest group grouping having ilike in initially inner inout int integer " +
    "intersect interval into is isnull join json lateral leading least left like limit localtime localtimestamp " +
    "national natural nchar none normalize not notnull null nullif numeric offset on only or order out outer " +
    "overlaps overlay placing position precision primary real references returning right row select " +
    "session_user setof similar smallint some substring symmetric system_user table tablesample then time " +
    "timestamp to trailing treat trim true union unique user using values varchar variadic verbose when " +
    "where window with xmlattributes xmlconcat xmlelement xmlexists xmlforest xmlnamespaces xmlparse xmlpi " +
    "xmlroot xmlserialize xmltable"
  ).split(" "),
);

/** Render an identifier, quoted only when it has to be. */
export function quoteIdent(name: string): string {
  const clean = String(name).replace(/\u0000/g, "");
  if (clean.length === 0) throw new LensSqlError("empty identifier");
  if (/^[a-z_][a-z0-9_]*$/.test(clean) && !KEYWORDS.has(clean)) return clean;
  return `"${clean.replace(/"/g, '""')}"`;
}

/** The per-channel view of a channel name, as tl.refresh_channel names it. */
export function channelViewName(channel: string): string {
  return `api.${quoteIdent("ch_" + channel.replace(/\./g, "_"))}`;
}

/**
 * The relation to select from for a channel: the view named by bootstrap, re-quoted part
 * by part, or the conventional name when bootstrap gave none.
 */
export function channelView(meta: Pick<ChannelMeta, "name"> & { view?: string | null }): string {
  const raw = (meta.view ?? "").trim();
  if (!raw) return channelViewName(meta.name);
  const parts = splitQualified(raw);
  const rel = parts.length > 1 ? parts.slice(1).join(".") : (parts[0] ?? "");
  const schema = parts.length > 1 ? (parts[0] ?? "api") : "api";
  if (schema !== "api" || !rel) return channelViewName(meta.name);
  return `api.${quoteIdent(rel)}`;
}

function splitQualified(raw: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i] as string;
    if (c === '"') {
      if (quoted && raw[i + 1] === '"') {
        cur += '"';
        i++;
      } else quoted = !quoted;
    } else if (c === "." && !quoted) {
      out.push(cur);
      cur = "";
    } else cur += c;
  }
  out.push(cur);
  return out;
}

/** An output alias that does not collide with the other aliases of the statement. */
export function freeAlias(wanted: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  let name = wanted.length > 55 ? wanted.slice(0, 55) : wanted;
  let i = 2;
  while (used.has(name)) name = `${wanted.slice(0, 52)}_${i++}`;
  return name;
}

// ---------------------------------------------------------------- literals

function stripNul(s: string): string {
  return s.replace(/\u0000/g, "");
}

const ISO_TS = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?))?(Z|[+-]\d{2}(?::?\d{2})?)?$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const TZ_NAME = /^[A-Za-z][A-Za-z0-9_+\-/]{0,63}$/;
const INTERVAL_WORDS =
  /^\s*(?:\d+(?:\.\d+)?\s*(?:microsecond|millisecond|second|minute|hour|day|week|month|year)s?\s*)+$/i;
const INTERVAL_ISO = /^P(?!$)(?:\d+Y)?(?:\d+M)?(?:\d+W)?(?:\d+D)?(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$/;

export const lit = {
  null(): string {
    return "NULL";
  },
  /** A text literal. Quotes are doubled; NUL bytes cannot exist in PostgreSQL text and are dropped. */
  text(value: unknown): string {
    if (value === null || value === undefined) return "NULL";
    return `'${stripNul(String(value)).replace(/'/g, "''")}'`;
  },
  /** A whole number. Negative values are parenthesised so `x-:n` can never open a comment. */
  int(value: unknown): string {
    if (value === null || value === undefined || value === "") return "NULL";
    const n = typeof value === "bigint" ? Number(value) : typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(n) || !Number.isInteger(n)) throw new LensSqlError(`not an integer: ${clip(value)}`);
    if (!Number.isSafeInteger(n)) throw new LensSqlError(`integer out of range: ${clip(value)}`);
    return n < 0 || Object.is(n, -0) ? `(${n === 0 ? 0 : n})` : String(n);
  },
  /** A float8 number. NaN and infinities have no place in a chart query and become NULL. */
  number(value: unknown): string {
    if (value === null || value === undefined || value === "") return "NULL";
    const n = typeof value === "number" ? value : Number(value);
    if (Number.isNaN(n) && typeof value !== "number") throw new LensSqlError(`not a number: ${clip(value)}`);
    if (!Number.isFinite(n)) return "NULL";
    const text = plainNumber(n);
    return n < 0 ? `(${text})` : text;
  },
  bool(value: unknown): string {
    if (value === null || value === undefined) return "NULL";
    if (value === true || value === "true") return "true";
    if (value === false || value === "false") return "false";
    throw new LensSqlError(`not a boolean: ${clip(value)}`);
  },
  /** An instant. ISO strings keep their microseconds; a string without a zone is read as UTC. */
  timestamp(value: unknown): string {
    if (value === null || value === undefined) return "NULL";
    if (value === "infinity" || value === "-infinity") return `timestamptz '${value}'`;
    if (value instanceof Date) {
      if (Number.isNaN(value.getTime())) throw new LensSqlError("invalid date");
      return `timestamptz '${value.toISOString()}'`;
    }
    if (typeof value === "number") {
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) throw new LensSqlError(`invalid timestamp: ${clip(value)}`);
      return `timestamptz '${d.toISOString()}'`;
    }
    const m = ISO_TS.exec(String(value).trim());
    if (!m) throw new LensSqlError(`invalid timestamp: ${clip(value)}`);
    const [, date, time, zone] = m;
    const iso = `${date}T${time ?? "00:00:00"}${zone ?? "Z"}`;
    if (Number.isNaN(Date.parse(normaliseForParse(iso)))) throw new LensSqlError(`invalid timestamp: ${clip(value)}`);
    return `timestamptz '${iso}'`;
  },
  date(value: unknown): string {
    if (value === null || value === undefined) return "NULL";
    const s = value instanceof Date ? value.toISOString().slice(0, 10) : String(value).trim();
    if (!ISO_DATE.test(s) || Number.isNaN(Date.parse(`${s}T00:00:00Z`))) {
      throw new LensSqlError(`invalid date: ${clip(value)}`);
    }
    return `date '${s}'`;
  },
  /** An interval: a number of seconds, "15 minutes", or an ISO 8601 duration. */
  interval(value: unknown): string {
    if (value === null || value === undefined) return "NULL";
    if (typeof value === "number") {
      if (!Number.isFinite(value)) throw new LensSqlError("invalid interval");
      return `interval '${String(value)} seconds'`;
    }
    const s = String(value).trim();
    if (INTERVAL_WORDS.test(s) || INTERVAL_ISO.test(s)) return `interval '${s}'`;
    throw new LensSqlError(`invalid interval: ${clip(value)}`);
  },
  /** An IANA time zone name as a text literal. */
  tz(value: unknown): string {
    const s = value === null || value === undefined || value === "" ? "UTC" : String(value);
    if (!TZ_NAME.test(s)) throw new LensSqlError(`invalid time zone: ${clip(value)}`);
    return `'${s}'`;
  },
  grain(value: unknown): string {
    if (!isGrain(value)) throw new LensSqlError(`invalid grain: ${clip(value)}`);
    return `'${value}'`;
  },
  textArray(values: unknown): string {
    const xs = asArray(values);
    return xs.length === 0 ? "ARRAY[]::text[]" : `ARRAY[${xs.map((v) => lit.text(v)).join(", ")}]::text[]`;
  },
  intArray(values: unknown): string {
    const xs = asArray(values);
    return xs.length === 0 ? "ARRAY[]::bigint[]" : `ARRAY[${xs.map((v) => lit.int(v)).join(", ")}]::bigint[]`;
  },
  numberArray(values: unknown): string {
    const xs = asArray(values);
    return xs.length === 0 ? "ARRAY[]::float8[]" : `ARRAY[${xs.map((v) => lit.number(v)).join(", ")}]::float8[]`;
  },
};

function asArray(values: unknown): unknown[] {
  if (values === null || values === undefined) return [];
  if (!Array.isArray(values)) throw new LensSqlError("not an array");
  return values;
}

function clip(value: unknown): string {
  const s = typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
  return s.length > 40 ? `${s.slice(0, 40)}...` : s;
}

function plainNumber(n: number): string {
  if (Object.is(n, -0)) return "0.0";
  const s = String(n);
  // Whole values keep a decimal point so `:hi / :bins` is never integer division.
  // 1e21 and 1e-7 are valid PostgreSQL numerics as written.
  return /^-?\d+$/.test(s) ? `${s}.0` : s;
}

function normaliseForParse(iso: string): string {
  // Date.parse wants +hh:mm and at most milliseconds.
  return iso
    .replace(/(\.\d{3})\d+/, "$1")
    .replace(/([+-]\d{2})(\d{2})$/, "$1:$2")
    .replace(/([+-]\d{2})$/, "$1:00")
    .replace(/T(\d{2}:\d{2})(Z|[+-])/, "T$1:00$2");
}

export function isGrain(value: unknown): value is Grain {
  return typeof value === "string" && (GRAINS as readonly string[]).includes(value);
}

/** Render one typed parameter value as a literal. */
export function renderParam(param: ParamValue): string {
  const v = param.value;
  switch (param.type) {
    case "text": return lit.text(v);
    case "int": return lit.int(v);
    case "number": return lit.number(v);
    case "bool": return lit.bool(v);
    case "timestamp": return lit.timestamp(v);
    case "date": return lit.date(v);
    case "interval": return lit.interval(v);
    case "text[]": return lit.textArray(v);
    case "int[]": return lit.intArray(v);
    case "number[]": return lit.numberArray(v);
    case "grain": return lit.grain(v);
    case "tz": return lit.tz(v);
    default: throw new LensSqlError(`unknown parameter type: ${String((param as { type: unknown }).type)}`);
  }
}

/** Guess a parameter type for a bare JavaScript value. */
export function paramOf(value: unknown, type?: ParamType): ParamValue {
  if (type) return { type, value };
  if (typeof value === "boolean") return { type: "bool", value };
  if (typeof value === "number") return { type: Number.isInteger(value) ? "int" : "number", value };
  if (value instanceof Date) return { type: "timestamp", value };
  if (Array.isArray(value)) {
    return { type: value.every((x) => typeof x === "number") && value.length > 0 ? "number[]" : "text[]", value };
  }
  return { type: "text", value };
}

// ---------------------------------------------------------------- parameter scanner

export interface ParamRef {
  name: string;
  /** Offset of the colon. */
  start: number;
  /** Offset just past the name. */
  end: number;
}

const IDENT_START = /[A-Za-z_\u0080-\uffff]/;
const IDENT_CHAR = /[A-Za-z0-9_$\u0080-\uffff]/;

/**
 * Find `:name` parameter references in a statement, following PostgreSQL's lexical rules:
 * string literals ('..', E'..' with backslash escapes), quoted identifiers, dollar-quoted
 * strings, line and nested block comments and `::` casts are all skipped.
 */
export function scanParams(sql: string): ParamRef[] {
  const refs: ParamRef[] = [];
  let skipUntil = 0;
  lexSkip(sql, (i, c) => {
    if (c !== ":" || i < skipUntil) return;
    const next = sql[i + 1];
    if (sql[i - 1] === ":" || next === ":" || next === "=") return;
    if (next === undefined || !IDENT_START.test(next) || slicePosition(sql, i)) return;
    let j = i + 1;
    while (j < sql.length && /[A-Za-z0-9_]/.test(sql[j] as string)) j++;
    refs.push({ name: sql.slice(i + 1, j), start: i, end: j });
    skipUntil = j;
  });
  return refs;
}

function skipString(sql: string, open: number, escapes: boolean): number {
  let i = open + 1;
  const n = sql.length;
  while (i < n) {
    const c = sql[i];
    if (escapes && c === "\\") i += 2;
    else if (c === "'") {
      if (sql[i + 1] === "'") i += 2;
      else return i + 1;
    } else i++;
  }
  return n;
}

// `arr[1:n]` is an array slice, not a parameter: a colon glued to a value on its left.
function slicePosition(sql: string, colon: number): boolean {
  const prev = sql[colon - 1];
  return prev !== undefined && /[A-Za-z0-9_\]]/.test(prev);
}

export interface Substitution {
  sql: string;
  /** Parameter names that were replaced. */
  used: string[];
  /** References with no value; left in the text untouched. */
  unresolved: string[];
}

/**
 * Replace `:name` references by rendered literals in a single pass, so a value that itself
 * contains ":from" or a quote is never looked at again.
 */
export function substituteParams(sql: string, values: Record<string, ParamValue | undefined>): Substitution {
  const refs = scanParams(sql);
  const used = new Set<string>();
  const unresolved = new Set<string>();
  let out = "";
  let at = 0;
  for (const ref of refs) {
    const value = Object.prototype.hasOwnProperty.call(values, ref.name) ? values[ref.name] : undefined;
    if (value === undefined) {
      unresolved.add(ref.name);
      continue;
    }
    out += sql.slice(at, ref.start) + renderParam(value);
    at = ref.end;
    used.add(ref.name);
  }
  out += sql.slice(at);
  return { sql: out, used: [...used], unresolved: [...unresolved] };
}

// ---------------------------------------------------------------- grains

const GRAIN_SECONDS: Record<Grain, number> = {
  minute: 60,
  hour: 3600,
  day: 86400,
  week: 7 * 86400,
  month: 30.44 * 86400,
  quarter: 91.31 * 86400,
  year: 365.25 * 86400,
};

export function grainSeconds(grain: Grain): number {
  return GRAIN_SECONDS[grain];
}

function toMs(value: unknown): number | null {
  if (value === null || value === undefined || value === "infinity" || value === "-infinity") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.getTime();
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const s = String(value).trim();
  const m = ISO_TS.exec(s);
  const ms = Date.parse(m ? normaliseForParse(`${m[1]}T${m[2] ?? "00:00:00"}${m[3] ?? "Z"}`) : s);
  return Number.isNaN(ms) ? null : ms;
}

/**
 * The finest grain that keeps a range under `budget` buckets (about 1,500 by default).
 * An unknown or empty range gets `month`.
 */
export function chooseGrain(
  from: unknown,
  to: unknown,
  opts: { budget?: number; min?: Grain; max?: Grain } = {},
): Grain {
  const budget = Math.max(1, opts.budget ?? 1500);
  const lo = toMs(from);
  const hi = toMs(to);
  const minIdx = opts.min ? GRAINS.indexOf(opts.min) : 0;
  const maxIdx = opts.max ? GRAINS.indexOf(opts.max) : GRAINS.length - 1;
  let pick: Grain = "month";
  if (lo !== null && hi !== null && hi >= lo) {
    const span = Math.max(1, (hi - lo) / 1000);
    pick = "year";
    for (const g of GRAINS) {
      if (span / GRAIN_SECONDS[g] <= budget) {
        pick = g;
        break;
      }
    }
  }
  const idx = Math.min(Math.max(GRAINS.indexOf(pick), minIdx), Math.max(minIdx, maxIdx));
  return GRAINS[idx] ?? "month";
}

// ---------------------------------------------------------------- capabilities

/** The builtins the validator allowed before the web release. */
export const NARROW_BUILTINS: readonly string[] = [
  "count", "sum", "avg", "min", "max", "array_agg", "jsonb_agg", "json_agg",
  "row_number", "rank", "dense_rank",
  "coalesce", "nullif", "greatest", "least", "abs", "round",
  "lower", "upper", "length", "left", "right", "substring", "substr",
  "btrim", "ltrim", "rtrim", "position", "strpos", "to_char",
  "date_trunc", "extract", "date_part", "now", "timezone", "generate_series",
  "jsonb_build_object", "to_jsonb", "websearch_to_tsquery", "ts_rank_cd",
];

/** The allow-list the web release extends it to. This is what the compiler assumes by default. */
export const EXTENDED_BUILTINS: readonly string[] = [
  ...NARROW_BUILTINS,
  "floor", "ceil", "width_bucket", "date_bin",
  "percentile_cont", "percentile_disc", "mode", "stddev_samp", "corr",
  "string_agg", "bool_or", "bool_and",
  "jsonb_typeof", "jsonb_array_length",
  "lag", "lead", "first_value", "last_value",
  "ln", "sqrt", "power", "split_part", "concat", "cardinality", "age",
  "jsonb_array_elements", "jsonb_array_elements_text", "jsonb_object_keys", "unnest",
];

export interface Capabilities {
  has(name: string): boolean;
  readonly names: readonly string[];
}

export function capabilities(names: Iterable<string> = EXTENDED_BUILTINS): Capabilities {
  const set = new Set(names);
  return { has: (name) => set.has(name), names: [...set] };
}

export const EXTENDED: Capabilities = capabilities(EXTENDED_BUILTINS);
export const NARROW: Capabilities = capabilities(NARROW_BUILTINS);

export interface WindowSpec {
  partitionBy?: string;
  orderBy: string;
}

export interface LateralElements {
  /** The FROM item to CROSS JOIN. */
  join: string;
  /** Expression of one element as text. */
  value: string;
  /** Extra WHERE condition the fallback needs, if any. */
  where: string | null;
}

/**
 * Every use of a builtin outside the original allow-list goes through this table: the
 * extended function when the server has it, the tested replacement when it does not, and
 * null where no replacement exists inside the dialect.
 */
export interface SqlFns {
  floor(x: string): string;
  ceil(x: string): string;
  /** 1-based bucket of x in [lo, hi) cut into `bins` equal parts; out-of-range values clamp. */
  widthBucket(x: string, lo: string, hi: string, bins: string): string;
  /** Start of the `seconds`-wide bucket holding timestamptz `ts`. */
  dateBin(seconds: number, ts: string): string;
  percentile(p: number, x: string): string | null;
  mode(x: string): string | null;
  stddev(x: string): string;
  corr(x: string, y: string): string;
  /** Text aggregate; without string_agg this is an array for the client to join. */
  stringAgg(x: string, separator: string): string;
  boolOr(b: string): string;
  boolAnd(b: string): string;
  jsonTypeof(v: string): string;
  jsonArrayLength(v: string): string;
  lag(x: string, w: WindowSpec): string;
  lead(x: string, w: WindowSpec): string;
  firstValue(x: string, w: WindowSpec): string;
  lastValue(x: string, w: WindowSpec): string;
  ln(x: string): string | null;
  sqrt(x: string): string;
  power(x: string, y: string): string;
  /** The n-th (1 or 2) part of x split on a single-character separator. */
  splitPart(x: string, separator: string, n: 1 | 2): string;
  concat(parts: string[]): string;
  cardinality(arr: string): string;
  /** Interval between two timestamps. */
  age(a: string, b: string): string;
  /** The elements of a jsonb array as text, one row each. Non-arrays yield no rows. */
  arrayElementsText(jsonExpr: string, alias: string, max?: number): LateralElements;
  /** The keys of a jsonb object, one row each; null when the server cannot enumerate keys. */
  objectKeys(jsonExpr: string, alias: string): LateralElements | null;
}

function over(w: WindowSpec, frame = ""): string {
  const part = w.partitionBy ? `PARTITION BY ${w.partitionBy} ` : "";
  return `OVER (${part}ORDER BY ${w.orderBy}${frame ? ` ${frame}` : ""})`;
}

const TYPEOF_CASE = (v: string) =>
  `(CASE WHEN ${v} IS NULL THEN NULL WHEN ${v} = 'null'::jsonb THEN 'null' WHEN left(${v}::text, 1) = '"' THEN 'string' ` +
  `WHEN left(${v}::text, 1) = '{' THEN 'object' WHEN left(${v}::text, 1) = '[' THEN 'array' ` +
  `WHEN ${v}::text IN ('true', 'false') THEN 'boolean' ELSE 'number' END)`;

export function sqlFns(caps: Capabilities = EXTENDED): SqlFns {
  const has = (name: string) => caps.has(name);
  const floor = (x: string) =>
    has("floor") ? `floor(${x})` : `((${x})::numeric - ((((${x})::numeric % 1) + 1) % 1))`;
  const jsonTypeof = (v: string) => (has("jsonb_typeof") ? `jsonb_typeof(${v})` : TYPEOF_CASE(v));
  return {
    floor,
    ceil: (x) => (has("ceil") ? `ceil(${x})` : `((${x})::numeric + ((((-(${x})::numeric) % 1) + 1) % 1))`),
    widthBucket: (x, lo, hi, bins) =>
      has("width_bucket")
        ? `greatest(1, least(${bins}, width_bucket((${x})::float8, (${lo})::float8, (${hi})::float8, ${bins})))`
        : `greatest(1, least(${bins}, (${floor(`((${x})::numeric - (${lo})::numeric) * ${bins} / ((${hi})::numeric - (${lo})::numeric)`)})::int + 1))`,
    dateBin: (seconds, ts) => {
      const s = Math.max(1, Math.round(seconds));
      return has("date_bin")
        ? `date_bin(interval '${s} seconds', ${ts}, timestamptz '2000-01-01T00:00:00Z')`
        : `(timestamptz 'epoch' + (${floor(`extract(epoch FROM ${ts}) / ${s}`)} * ${s}) * interval '1 second')`;
    },
    percentile: (p, x) =>
      has("percentile_cont") ? `(percentile_cont(${lit.number(p)}) WITHIN GROUP (ORDER BY ${x}))::float8` : null,
    mode: (x) => (has("mode") ? `mode() WITHIN GROUP (ORDER BY ${x})` : null),
    stddev: (x) =>
      has("stddev_samp")
        ? `stddev_samp(${x})::float8`
        : `(|/ greatest(avg((${x})::float8 * (${x})::float8) - avg((${x})::float8) ^ 2, 0))::float8`,
    corr: (x, y) =>
      has("corr")
        ? `corr((${x})::float8, (${y})::float8)::float8`
        : `((avg((${x})::float8 * (${y})::float8) - avg((${x})::float8) * avg((${y})::float8)) / nullif(` +
          `(|/ greatest(avg((${x})::float8 * (${x})::float8) - avg((${x})::float8) ^ 2, 0)) * ` +
          `(|/ greatest(avg((${y})::float8 * (${y})::float8) - avg((${y})::float8) ^ 2, 0)), 0))::float8`,
    stringAgg: (x, separator) =>
      has("string_agg") ? `string_agg(${x}, ${lit.text(separator)} ORDER BY ${x})` : `array_agg(${x} ORDER BY ${x})`,
    boolOr: (b) => (has("bool_or") ? `bool_or(${b})` : `(count(*) FILTER (WHERE ${b}) > 0)`),
    boolAnd: (b) => (has("bool_and") ? `bool_and(${b})` : `(count(*) FILTER (WHERE NOT (${b})) = 0)`),
    jsonTypeof,
    jsonArrayLength: (v) =>
      has("jsonb_array_length") && has("jsonb_typeof")
        ? `(CASE WHEN jsonb_typeof(${v}) = 'array' THEN jsonb_array_length(${v}) END)`
        : `(CASE WHEN left(${v}::text, 1) = '[' THEN (SELECT count(*) FROM generate_series(0, 199) AS ix(i) WHERE ${v} -> ix.i IS NOT NULL) END)`,
    lag: (x, w) =>
      has("lag") ? `lag(${x}) ${over(w)}` : `max(${x}) ${over(w, "ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING")}`,
    lead: (x, w) =>
      has("lead") ? `lead(${x}) ${over(w)}` : `min(${x}) ${over(w, "ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING")}`,
    firstValue: (x, w) =>
      has("first_value")
        ? `first_value(${x}) ${over(w, "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING")}`
        : `(array_agg(${x}) ${over(w, "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING")})[1]`,
    lastValue: (x, w) =>
      has("last_value")
        ? `last_value(${x}) ${over(w, "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING")}`
        : `(array_agg(${x}) ${over({ ...w, orderBy: flipOrder(w.orderBy) }, "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING")})[1]`,
    ln: (x) => (has("ln") ? `ln(${x})` : null),
    sqrt: (x) => (has("sqrt") ? `sqrt(${x})` : `(|/ (${x}))`),
    power: (x, y) => (has("power") ? `power(${x}, ${y})` : `((${x}) ^ (${y}))`),
    splitPart: (x, separator, n) => {
      if (separator.length !== 1) throw new LensSqlError("splitPart takes a single-character separator");
      if (has("split_part")) return `split_part(${x}, ${lit.text(separator)}, ${n})`;
      const cls = `[^${/[\]\\^-]/.test(separator) ? "\\" + separator : separator}]*`;
      const sep = /[.[\](){}*+?^$|\\]/.test(separator) ? "\\" + separator : separator;
      return n === 1
        ? `coalesce(substring(${x} from ${lit.text("^" + cls)}), '')`
        : `coalesce(substring(${x} from ${lit.text("^" + cls + sep + "(" + cls + ")")}), '')`;
    },
    concat: (parts) =>
      has("concat") ? `concat(${parts.join(", ")})` : `(${parts.map((p) => `coalesce((${p})::text, '')`).join(" || ")})`,
    cardinality: (arr) =>
      has("cardinality")
        ? `cardinality(${arr})`
        : `(SELECT count(*) FROM generate_series(1, 1000) AS ix(i) WHERE (${arr})[ix.i] IS NOT NULL)`,
    age: (a, b) => (has("age") ? `age(${a}, ${b})` : `(${a} - ${b})`),
    arrayElementsText: (jsonExpr, alias, max = 50) => {
      const a = quoteIdent(alias);
      if (has("jsonb_array_elements_text") && has("jsonb_typeof")) {
        return {
          join: `LATERAL jsonb_array_elements_text(CASE WHEN jsonb_typeof(${jsonExpr}) = 'array' THEN ${jsonExpr} ELSE '[]'::jsonb END) AS ${a}(value)`,
          value: `${a}.value`,
          where: null,
        };
      }
      const top = Math.max(1, Math.min(200, Math.round(max))) - 1;
      return {
        join: `generate_series(0, ${top}) AS ${a}(i)`,
        value: `(${jsonExpr} ->> ${a}.i)`,
        where: `left(${jsonExpr}::text, 1) = '[' AND ${jsonExpr} -> ${a}.i IS NOT NULL`,
      };
    },
    objectKeys: (jsonExpr, alias) => {
      if (!(has("jsonb_object_keys") && has("jsonb_typeof"))) return null;
      const a = quoteIdent(alias);
      return {
        join: `LATERAL jsonb_object_keys(CASE WHEN jsonb_typeof(${jsonExpr}) = 'object' THEN ${jsonExpr} ELSE '{}'::jsonb END) AS ${a}(value)`,
        value: `${a}.value`,
        where: null,
      };
    },
  };
}

function flipOrder(orderBy: string): string {
  return orderBy
    .split(",")
    .map((part) => {
      const p = part.trim();
      if (/\sDESC$/i.test(p)) return p.replace(/\sDESC$/i, " ASC");
      if (/\sASC$/i.test(p)) return p.replace(/\sASC$/i, " DESC");
      return `${p} DESC`;
    })
    .join(", ");
}

/**
 * One small statement per capability-table entry, for the conformance run: each must be
 * accepted and executed by the server in the formulation the given capabilities select.
 */
export function builtinProbes(caps: Capabilities = EXTENDED): { name: string; sql: string }[] {
  const f = sqlFns(caps);
  const w: WindowSpec = { partitionBy: "channel_id", orderBy: "id" };
  const base = "FROM api.messages";
  const one = (name: string, expr: string | null, tail = `${base} LIMIT 5`) =>
    expr === null ? null : { name, sql: `SELECT ${expr} AS v ${tail}` };
  const tags = f.arrayElementsText("c.fields", "e");
  const keys = f.objectKeys("m.fields", "k");
  const probes = [
    one("floor", `${f.floor("id / 7.0")}::float8`),
    one("ceil", `${f.ceil("id / 7.0")}::float8`),
    one("width_bucket", f.widthBucket("id", "0", "1000000", "20")),
    one("date_bin", f.dateBin(900, "happened_at")),
    one("percentile_cont", f.percentile(0.5, "id"), base),
    one("mode", f.mode("channel_id"), base),
    one("stddev_samp", f.stddev("id"), base),
    one("corr", f.corr("id", "channel_id"), base),
    one("string_agg", f.stringAgg("name", ","), "FROM api.channels"),
    one("bool_or", f.boolOr("is_reference"), "FROM api.channels"),
    one("bool_and", f.boolAnd("is_reference"), "FROM api.channels"),
    one("jsonb_typeof", f.jsonTypeof("fields"), "FROM api.channels LIMIT 5"),
    one("jsonb_array_length", f.jsonArrayLength("fields"), "FROM api.channels LIMIT 5"),
    one("lag", f.lag("id", w)),
    one("lead", f.lead("id", w)),
    one("first_value", f.firstValue("id", w)),
    one("last_value", f.lastValue("id", w)),
    one("ln", f.ln("(id + 1)::float8")),
    one("sqrt", `${f.sqrt("id::float8")}::float8`),
    one("power", `${f.power("2::float8", "3")}::float8`, "FROM api.channels LIMIT 1"),
    one("split_part", f.splitPart("name", ".", 2), "FROM api.channels LIMIT 5"),
    one("concat", f.concat(["name", "'/'", "plugin"]), "FROM api.channels LIMIT 5"),
    one("cardinality", f.cardinality("ARRAY[1, 2, 3]"), "FROM api.channels LIMIT 1"),
    one("age", `${f.age("now()", "happened_at")}::text`),
    {
      name: "jsonb_array_elements_text",
      sql: `SELECT ${tags.value} AS v FROM api.channels c CROSS JOIN ${tags.join}${tags.where ? ` WHERE ${tags.where}` : ""} LIMIT 5`,
    },
    keys === null
      ? null
      : { name: "jsonb_object_keys", sql: `SELECT ${keys.value} AS v FROM api.messages m CROSS JOIN ${keys.join} LIMIT 5` },
  ];
  return probes.filter((p): p is { name: string; sql: string } => p !== null);
}

// ---------------------------------------------------------------- expressions

/** `date_trunc(:grain, col, :tz)`: the bucket start as an instant, cut in the viewer's zone. */
export function bucketExpr(col = "happened_at"): string {
  return `date_trunc(:grain, ${col}, :tz)`;
}

/** The local calendar day of an instant. */
export function localDayExpr(col = "happened_at"): string {
  return `(${col} AT TIME ZONE :tz)::date`;
}

/** Half-open scope filter on a time column. */
export function scopeFilter(col = "happened_at"): string {
  return `${col} >= :from AND ${col} < :to`;
}

/** Aggregate of a column cast so the value arrives as a JSON number. */
export function numericAgg(agg: "avg" | "sum" | "min" | "max", col: string): string {
  return `${agg}(${col})::float8`;
}

/** Sampling modulus that keeps about `target` of `rows` rows (1 = everything). */
export function sampleModulo(rows: number, target: number): number {
  if (!Number.isFinite(rows) || rows <= target || target <= 0) return 1;
  return Math.max(1, Math.ceil(rows / target));
}

/**
 * Wrap a user's statement as a subquery so it can be aggregated. Returns null when the
 * text cannot be wrapped soundly (empty, or more than one statement).
 */
export function wrapStatement(statement: string, alias = "q"): string | null {
  let body = statement.trim();
  if (!body) return null;
  // Remove trailing semicolons that are real tokens, and refuse any other semicolon.
  const semis = topLevelSemicolons(body);
  while (semis.length > 0) {
    const last = semis[semis.length - 1] as number;
    const tail = body.slice(last + 1);
    if (stripComments(tail).trim() !== "") break;
    body = body.slice(0, last).trimEnd() + (tail.trim() ? `\n${tail.trim()}` : "");
    semis.pop();
  }
  if (topLevelSemicolons(body).length > 0) return null;
  if (stripComments(body).trim() === "") return null;
  // The closing parenthesis goes on its own line: the statement may end in a line comment.
  return `(\n${body}\n) AS ${quoteIdent(alias)}`;
}

function lexSkip(sql: string, visit: (i: number, c: string) => void): void {
  // Walk the characters that are plain SQL tokens (outside strings, identifiers, comments).
  const n = sql.length;
  let i = 0;
  while (i < n) {
    const c = sql[i] as string;
    const next = sql[i + 1];
    if (c === "'") {
      const prev = sql[i - 1];
      i = skipString(sql, i, (prev === "E" || prev === "e") && !(i >= 2 && IDENT_CHAR.test(sql[i - 2] as string)));
    } else if (c === '"') {
      i++;
      while (i < n && !(sql[i] === '"' && sql[i + 1] !== '"')) i += sql[i] === '"' ? 2 : 1;
      i++;
    } else if (c === "-" && next === "-") {
      while (i < n && sql[i] !== "\n") i++;
    } else if (c === "/" && next === "*") {
      let depth = 1;
      i += 2;
      while (i < n && depth > 0) {
        if (sql[i] === "/" && sql[i + 1] === "*") {
          depth++;
          i += 2;
        } else if (sql[i] === "*" && sql[i + 1] === "/") {
          depth--;
          i += 2;
        } else i++;
      }
    } else if (c === "$" && !(i > 0 && IDENT_CHAR.test(sql[i - 1] as string))) {
      const tag = /^\$(?:[A-Za-z_\u0080-\uffff][A-Za-z0-9_\u0080-\uffff]*)?\$/.exec(sql.slice(i, i + 80));
      if (tag) {
        const close = sql.indexOf(tag[0], i + tag[0].length);
        i = close < 0 ? n : close + tag[0].length;
      } else {
        visit(i, c);
        i++;
      }
    } else {
      visit(i, c);
      i++;
    }
  }
}

function topLevelSemicolons(sql: string): number[] {
  const out: number[] = [];
  lexSkip(sql, (i, c) => {
    if (c === ";") out.push(i);
  });
  return out;
}

function stripComments(sql: string): string {
  let out = "";
  lexSkip(sql, (_i, c) => {
    out += c;
  });
  return out;
}

// ---------------------------------------------------------------- compiler

export interface Scope {
  /** Start of the time scope (inclusive). Missing means unbounded. */
  from?: string | number | Date | null;
  /** End of the time scope (exclusive). Missing means unbounded. */
  to?: string | number | Date | null;
  tz?: string | null;
  person?: number | null;
  channel?: string | null;
  /** Force a grain instead of letting the compiler choose. */
  grain?: Grain | null;
  /** Values for panel-local parameters; they replace the spec's defaults. */
  params?: Record<string, unknown>;
}

export interface CompiledView {
  sql: string;
  row_cap: number;
  /** The grain substituted for :grain, when the statement uses one. */
  grain: Grain | null;
  /** Parameters referenced by the statement that nothing supplied. */
  unresolved: string[];
}

export const GLOBAL_PARAMS = ["from", "to", "tz", "person", "channel", "grain"] as const;

/**
 * Turn a view spec and the current scope into the statement to send. Global parameters
 * are typed here; panel-local ones take the type the spec declares. The grain is chosen
 * from the scope (cut down to the data's own extent when the spec records it) so the
 * result stays under the spec's bucket budget.
 */
export function compileView(spec: ViewSpec, scope: Scope = {}): CompiledView {
  const options = (spec.options ?? {}) as Record<string, unknown>;
  const extent = Array.isArray(options.extent) ? (options.extent as unknown[]) : [];
  const lo = maxMs(toMs(scope.from), toMs(extent[0]));
  const hi = minMs(toMs(scope.to), toMs(extent[1]));
  const grain =
    scope.grain ??
    (isGrain(options.grain) ? options.grain : null) ??
    chooseGrain(lo, hi, {
      budget: typeof options.bucket_budget === "number" ? options.bucket_budget : 1500,
      min: isGrain(options.min_grain) ? options.min_grain : undefined,
      max: isGrain(options.max_grain) ? options.max_grain : undefined,
    });

  const values: Record<string, ParamValue | undefined> = {
    from: { type: "timestamp", value: scope.from ?? "-infinity" },
    to: { type: "timestamp", value: scope.to ?? "infinity" },
    tz: { type: "tz", value: scope.tz ?? "UTC" },
    person: { type: "int", value: scope.person ?? null },
    channel: { type: "text", value: scope.channel ?? spec.provenance.channel ?? null },
    grain: { type: "grain", value: grain },
  };
  const locals = spec.source.params ?? {};
  for (const [name, def] of Object.entries(locals)) {
    const given = scope.params && Object.prototype.hasOwnProperty.call(scope.params, name)
      ? scope.params[name]
      : undefined;
    values[name] = given === undefined ? def : { type: def.type, value: given };
  }
  for (const [name, given] of Object.entries(scope.params ?? {})) {
    if (!(name in values) && given !== undefined) values[name] = paramOf(given);
  }
  const done = substituteParams(spec.source.sql, values);
  return {
    sql: done.sql,
    row_cap: Math.min(10000, Math.max(1, Math.round(spec.source.row_cap))),
    grain: done.used.includes("grain") ? grain : null,
    unresolved: done.unresolved,
  };
}

function maxMs(a: number | null, b: number | null): number | null {
  if (a === null) return b;
  if (b === null) return a;
  return Math.max(a, b);
}

function minMs(a: number | null, b: number | null): number | null {
  if (a === null) return b;
  if (b === null) return a;
  return Math.min(a, b);
}
