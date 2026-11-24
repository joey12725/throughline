/*
 * Pure logic behind the classifier screens: the value-schema form model, the
 * editor's form state <-> definition mapping, /check diagnostics by line, and
 * small formatters. No React, no fetch — everything here is unit-tested.
 */
import type {
  ClassifierDefinition,
  ClassifierKind,
  NewAttribute,
  ValueFieldSpec,
  ValueSchema,
} from "@/api/classifiers";

/* ---- value schema -------------------------------------------------------- */

export type FieldInputKind = "boolean" | "enum" | "integer" | "number" | "text";
export type Value = Record<string, unknown>;

const BOOL_TYPES = new Set(["boolean", "bool"]);
const INT_TYPES = new Set(["integer", "int"]);
const NUM_TYPES = new Set(["number", "float", "numeric", "double"]);

/** Which control edits a field. A `values` list makes any field a choice. */
export function fieldInputKind(spec: ValueFieldSpec): FieldInputKind {
  const t = (spec.type ?? "").toLowerCase();
  if (Array.isArray(spec.values) && spec.values.length > 0) return "enum";
  if (BOOL_TYPES.has(t)) return "boolean";
  if (INT_TYPES.has(t)) return "integer";
  if (NUM_TYPES.has(t)) return "number";
  return "text";
}

/**
 * A sensible starting value for a schema: booleans start true (a classifier
 * writes the value when the answer is yes), required choices take their first
 * option, required numbers their minimum (or 0). Optional free fields are left out.
 */
export function defaultValue(schema: ValueSchema | null | undefined): Value {
  const out: Value = {};
  for (const [name, spec] of Object.entries(schema ?? {})) {
    const kind = fieldInputKind(spec);
    if (kind === "boolean") out[name] = true;
    else if (kind === "enum" && spec.required) out[name] = spec.values?.[0];
    else if ((kind === "integer" || kind === "number") && spec.required) out[name] = spec.min ?? 0;
    else if (kind === "text" && spec.required) out[name] = "";
  }
  return out;
}

/** Field name -> problem, for a value against its schema. Empty when valid. */
export function validateValue(schema: ValueSchema | null | undefined, value: Value | null | undefined): Record<string, string> {
  const errs: Record<string, string> = {};
  const v = value ?? {};
  for (const [name, spec] of Object.entries(schema ?? {})) {
    const x = v[name];
    const missing = x === undefined || x === null || x === "";
    if (missing) {
      if (spec.required) errs[name] = "Required";
      continue;
    }
    const kind = fieldInputKind(spec);
    if (kind === "boolean" && typeof x !== "boolean") errs[name] = "Must be true or false";
    else if (kind === "enum" && !(spec.values ?? []).some((o) => o === x)) errs[name] = `One of ${(spec.values ?? []).join(", ")}`;
    else if (kind === "integer" || kind === "number") {
      if (typeof x !== "number" || Number.isNaN(x)) errs[name] = "Must be a number";
      else if (kind === "integer" && !Number.isInteger(x)) errs[name] = "Must be a whole number";
      else if (spec.min !== undefined && x < spec.min) errs[name] = `At least ${spec.min}`;
      else if (spec.max !== undefined && x > spec.max) errs[name] = `At most ${spec.max}`;
    } else if (kind === "text" && typeof x !== "string") errs[name] = "Must be text";
  }
  for (const name of Object.keys(v)) {
    if (schema && !(name in schema)) errs[name] = "Not in this attribute's schema";
  }
  return errs;
}

/** Turn what a user typed into a field value (undefined clears the field). */
export function parseFieldInput(spec: ValueFieldSpec, raw: string): unknown {
  const kind = fieldInputKind(spec);
  if (raw === "") return undefined;
  if (kind === "boolean") return raw === "true";
  if (kind === "integer" || kind === "number") {
    const n = Number(raw);
    return Number.isNaN(n) ? raw : n;
  }
  if (kind === "enum") {
    // Keep a numeric option numeric.
    const hit = (spec.values ?? []).find((o) => String(o) === raw);
    return hit ?? raw;
  }
  return raw;
}

/** Set or clear one field, returning a new value object. */
export function setField(value: Value, name: string, next: unknown): Value {
  const out = { ...value };
  if (next === undefined) delete out[name];
  else out[name] = next;
  return out;
}

function fmtScalar(x: unknown): string {
  if (x === true) return "yes";
  if (x === false) return "no";
  if (x === null || x === undefined) return "—";
  if (typeof x === "object") return JSON.stringify(x);
  return String(x);
}

/** A compact, human reading of an annotation value. */
export function summarizeValue(value: Value | null | undefined, flagField?: string | null): string {
  if (value === null || value === undefined) return "cleared";
  // "classifier" only repeats who wrote it; the flag field leads.
  const entries = Object.entries(value)
    .filter(([k]) => k !== "classifier")
    .sort((a, b) => (a[0] === flagField ? -1 : b[0] === flagField ? 1 : 0));
  if (entries.length === 0) return "{}";
  if (entries.length === 1) {
    const [k, x] = entries[0] as [string, unknown];
    return typeof x === "boolean" ? `${humanize(k)}: ${fmtScalar(x)}` : `${humanize(k)} ${fmtScalar(x)}`;
  }
  return entries.map(([k, x]) => `${humanize(k)}: ${fmtScalar(x)}`).join(" · ");
}

/** A note's detail object as one line (e.g. "level partial · kind audiobook"). */
export function summarizeDetail(detail: unknown): string {
  if (detail === null || detail === undefined || detail === "") return "";
  if (typeof detail === "string") return detail;
  if (typeof detail !== "object") return String(detail);
  const entries = Object.entries(detail as Record<string, unknown>);
  // "level" leads: it says what the conversation as a whole was judged.
  entries.sort((a, b) => (a[0] === "level" ? -1 : b[0] === "level" ? 1 : 0));
  return entries
    .map(([k, x]) => `${humanize(k).toLowerCase()} ${Array.isArray(x) ? x.map(fmtScalar).join(", ") : fmtScalar(x)}`)
    .join(" · ");
}

/**
 * Whether an annotation value means "flagged". A value whose boolean fields are
 * all false is an explicit "not" (e.g. the owner's "not media"); no booleans at
 * all means the presence of the value is the flag.
 */
export function isFlagged(value: Value | null | undefined, flagField?: string | null): boolean {
  if (!value) return false;
  if (flagField && typeof value[flagField] === "boolean") return value[flagField] as boolean;
  const bools = Object.values(value).filter((x): x is boolean => typeof x === "boolean");
  if (bools.length === 0) return true;
  return bools.some(Boolean);
}

/**
 * The single boolean field that a quick yes/no correction toggles, when the
 * schema has one and no other required field. Otherwise null (use the editor).
 */
export function toggleField(schema: ValueSchema | null | undefined, flagField?: string | null): string | null {
  const entries = Object.entries(schema ?? {});
  const flagSpec = flagField ? schema?.[flagField] : undefined;
  if (flagField && flagSpec && fieldInputKind(flagSpec) === "boolean") {
    const otherRequired = entries.some(([n, s]) => n !== flagField && s.required);
    return otherRequired ? null : flagField;
  }
  const bools = entries.filter(([, s]) => fieldInputKind(s) === "boolean");
  if (bools.length !== 1) return null;
  const [name] = bools[0] as [string, ValueFieldSpec];
  const otherRequired = entries.some(([n, s]) => n !== name && s.required);
  return otherRequired ? null : name;
}

export interface CorrectionLabels {
  yes: string;
  no: string;
  allYes: string;
  allNo: string;
}

/** Button words for the owner's quick corrections on an attribute. */
export function correctionLabels(attribute: string): CorrectionLabels {
  if (attribute === "media_noise") return { yes: "Media", no: "Not media", allYes: "All media", allNo: "No media" };
  const h = humanize(attribute);
  return { yes: h, no: `Not ${h.toLowerCase()}`, allYes: `All ${h.toLowerCase()}`, allNo: `No ${h.toLowerCase()}` };
}

export function humanize(name: string): string {
  const s = name.replace(/[_\-.]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : name;
}

/* ---- new-attribute field drafts ----------------------------------------- */

export type FieldType = "boolean" | "string" | "integer" | "number";

export interface FieldDraft {
  name: string;
  type: FieldType;
  required: boolean;
  /** comma-separated choices; empty = free */
  values: string;
  min: string;
  max: string;
}

export const emptyField = (): FieldDraft => ({ name: "", type: "boolean", required: true, values: "", min: "", max: "" });

export function fieldsToSchema(fields: readonly FieldDraft[]): ValueSchema {
  const out: ValueSchema = {};
  for (const f of fields) {
    const name = f.name.trim();
    if (!name) continue;
    const spec: ValueFieldSpec = { type: f.type };
    if (f.required) spec.required = true;
    const vals = f.values
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean);
    if (vals.length && f.type !== "boolean") {
      // A text field with choices is an enum on the server.
      if (f.type === "string") {
        spec.type = "enum";
        spec.values = vals;
      } else spec.values = vals.map((v) => Number(v));
    }
    if (f.type === "integer" || f.type === "number") {
      if (f.min.trim() !== "" && !Number.isNaN(Number(f.min))) spec.min = Number(f.min);
      if (f.max.trim() !== "" && !Number.isNaN(Number(f.max))) spec.max = Number(f.max);
    }
    out[name] = spec;
  }
  return out;
}

export function schemaToFields(schema: ValueSchema | null | undefined): FieldDraft[] {
  return Object.entries(schema ?? {}).map(([name, s]) => {
    const t = (s.type ?? "").toLowerCase();
    // An enum's choices are text unless they are all numbers.
    const type: FieldType = BOOL_TYPES.has(t) ? "boolean" : INT_TYPES.has(t) ? "integer" : NUM_TYPES.has(t) ? "number" : "string";
    return {
      name,
      type,
      required: !!s.required,
      values: (s.values ?? []).join(", "),
      min: s.min === undefined ? "" : String(s.min),
      max: s.max === undefined ? "" : String(s.max),
    };
  });
}

const NAME_RE = /^[a-z][a-z0-9_]{1,62}$/;

/** Problems with a new attribute draft, keyed "name", "fields", "field.<i>". */
export function attributeProblems(a: { name: string; fields: readonly FieldDraft[] }): Record<string, string> {
  const errs: Record<string, string> = {};
  if (!NAME_RE.test(a.name.trim())) errs.name = "lowercase letters, digits and _; starts with a letter";
  if (a.fields.length === 0) errs.fields = "Add at least one field";
  const seen = new Set<string>();
  a.fields.forEach((f, i) => {
    const n = f.name.trim();
    if (!/^[a-z][a-z0-9_]*$/.test(n)) errs[`field.${i}`] = "lowercase name";
    else if (seen.has(n)) errs[`field.${i}`] = "duplicate name";
    seen.add(n);
    if ((f.type === "integer" || f.type === "number") && f.values.trim()) {
      const bad = f.values.split(",").some((v) => v.trim() !== "" && Number.isNaN(Number(v.trim())));
      if (bad) errs[`field.${i}`] = "choices must be numbers";
    }
  });
  return errs;
}

/* ---- the editor's form state --------------------------------------------- */

export type BackfillMode = "none" | "all" | "days" | "since";

export interface ClassifierForm {
  kind: ClassifierKind;
  name: string;
  description: string;
  /** empty = all channels */
  channels: string[];
  monthly_cap_usd: string;
  attrMode: "existing" | "new";
  attribute: string;
  /** flag: the boolean field meaning "flagged", "" for none */
  newAttr: { name: string; description: string; recall_effect: "none" | "exclude"; flag: string; fields: FieldDraft[] };

  // message kind
  worker: "jev" | "llm";
  question: string;
  answer: "yes_no" | "choice";
  threshold: number;
  value: Value;
  options: string[];
  values: Record<string, Value>;
  context_messages: string;
  min_chars: string;
  max_per_run: string;
  backfill: BackfillMode;
  backfillDays: string;
  backfillSince: string;

  // python kind
  code: string;
  settle_minutes: string;
  min_messages: string;
  since: string;
  max_threads_per_run: string;

  // shared
  max_cost_per_run: string;
  /** spec keys this form does not edit, carried through untouched */
  extraSpec: Record<string, unknown>;
}

export function emptyForm(kind: ClassifierKind): ClassifierForm {
  return {
    kind,
    name: "",
    description: "",
    channels: [],
    monthly_cap_usd: "",
    attrMode: "existing",
    attribute: "",
    newAttr: { name: "", description: "", recall_effect: "none", flag: "", fields: [emptyField()] },
    worker: "jev",
    question: "",
    answer: "yes_no",
    threshold: 0.7,
    value: {},
    options: ["yes", "no"],
    values: {},
    context_messages: "0",
    min_chars: "",
    max_per_run: "",
    backfill: "none",
    backfillDays: "30",
    backfillSince: "",
    code: "",
    settle_minutes: "",
    min_messages: "",
    since: "",
    max_threads_per_run: "",
    max_cost_per_run: "",
    extraSpec: {},
  };
}

const MESSAGE_KEYS = new Set([
  "worker", "question", "answer", "threshold", "value", "options", "values",
  "context_messages", "min_chars", "max_per_run", "max_cost_per_run",
]);
const PYTHON_KEYS = new Set(["settle_minutes", "min_messages", "since", "max_threads_per_run", "max_cost_per_run"]);

function numOrUndef(s: string): number | undefined {
  if (s.trim() === "") return undefined;
  const n = Number(s);
  return Number.isNaN(n) ? undefined : n;
}

function put(o: Record<string, unknown>, k: string, v: unknown): void {
  if (v !== undefined && v !== "") o[k] = v;
}

export function formAttribute(form: ClassifierForm): string | NewAttribute {
  if (form.attrMode === "existing") return form.attribute;
  const a: NewAttribute = {
    name: form.newAttr.name.trim(),
    description: form.newAttr.description.trim(),
    value_schema: fieldsToSchema(form.newAttr.fields),
    recall_effect: form.newAttr.recall_effect,
  };
  if (form.newAttr.flag && form.newAttr.fields.some((f) => f.name.trim() === form.newAttr.flag && f.type === "boolean")) {
    a.flag = form.newAttr.flag;
  }
  return a;
}

/** The schema the value editors follow: the picked attribute's, or the draft's. */
export function formSchema(form: ClassifierForm, existing: ValueSchema | null | undefined): ValueSchema {
  return form.attrMode === "new" ? fieldsToSchema(form.newAttr.fields) : (existing ?? {});
}

export function formToDefinition(form: ClassifierForm): ClassifierDefinition {
  const spec: Record<string, unknown> = { ...form.extraSpec };
  if (form.kind === "message") {
    spec.worker = form.worker;
    spec.question = form.question.trim();
    if (form.worker === "jev") spec.answer = form.answer;
    const choice = form.worker === "jev" && form.answer === "choice";
    if (choice) {
      const opts = form.options.map((o) => o.trim()).filter(Boolean);
      spec.options = opts;
      const values: Record<string, Value> = {};
      for (const o of opts) {
        const v = form.values[o];
        if (v && Object.keys(v).length) values[o] = v;
      }
      spec.values = values;
    } else {
      spec.threshold = Math.round(form.threshold * 100) / 100;
      spec.value = form.value;
    }
    put(spec, "context_messages", numOrUndef(form.context_messages));
    put(spec, "min_chars", numOrUndef(form.min_chars));
    put(spec, "max_per_run", numOrUndef(form.max_per_run));
  } else {
    put(spec, "settle_minutes", numOrUndef(form.settle_minutes));
    put(spec, "min_messages", numOrUndef(form.min_messages));
    put(spec, "since", form.since.trim() || undefined);
    put(spec, "max_threads_per_run", numOrUndef(form.max_threads_per_run));
  }
  put(spec, "max_cost_per_run", numOrUndef(form.max_cost_per_run));

  const def: ClassifierDefinition = {
    name: form.name.trim(),
    kind: form.kind,
    attribute: formAttribute(form),
    channels: form.channels.length ? [...form.channels] : ["*"],
    spec,
  };
  if (form.description.trim()) def.description = form.description.trim();
  if (form.kind === "python") def.code = form.code;
  const cap = numOrUndef(form.monthly_cap_usd);
  if (cap !== undefined) def.monthly_cap_usd = cap;
  if (form.kind === "message" && form.backfill !== "none") {
    if (form.backfill === "since") {
      if (form.backfillSince) def.backfill = form.backfillSince;
    } else if (form.backfill === "days") {
      const n = Number(form.backfillDays);
      if (Number.isInteger(n) && n > 0) def.backfill = `${n}d`;
    } else def.backfill = form.backfill;
  }
  return def;
}

const str = (x: unknown): string => (x === undefined || x === null ? "" : String(x));
const isObj = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);

/**
 * Load a definition (a saved classifier, a guide example, an AI draft) into the
 * form. Missing parts keep `base`'s values, so a partial draft fills what it can.
 */
export function definitionToForm(def: Partial<ClassifierDefinition> & { code?: string | null }, base?: ClassifierForm): ClassifierForm {
  const kind: ClassifierKind = def.kind === "python" || def.kind === "message" ? def.kind : (base?.kind ?? "message");
  const f: ClassifierForm = { ...(base ?? emptyForm(kind)), kind };
  if (def.name !== undefined) f.name = def.name;
  if (def.description !== undefined && def.description !== null) f.description = def.description;
  if (Array.isArray(def.channels)) f.channels = def.channels.filter((c) => c !== "*");
  if (def.monthly_cap_usd !== undefined && def.monthly_cap_usd !== null) f.monthly_cap_usd = String(def.monthly_cap_usd);
  if (typeof def.attribute === "string") {
    f.attrMode = "existing";
    f.attribute = def.attribute;
  } else if (isObj(def.attribute)) {
    const a = def.attribute as Partial<NewAttribute>;
    f.attrMode = "new";
    f.newAttr = {
      name: a.name ?? "",
      description: a.description ?? "",
      recall_effect: a.recall_effect === "exclude" ? "exclude" : "none",
      flag: typeof a.flag === "string" ? a.flag : "",
      fields: schemaToFields(a.value_schema).length ? schemaToFields(a.value_schema) : [emptyField()],
    };
  }
  if (typeof def.code === "string") f.code = def.code;
  if (typeof def.backfill === "string") {
    const days = /^(\d+)d$/.exec(def.backfill);
    if (def.backfill === "all") f.backfill = "all";
    else if (days) {
      f.backfill = "days";
      f.backfillDays = days[1] as string;
    } else {
      f.backfill = "since";
      f.backfillSince = def.backfill.slice(0, 10);
    }
  }

  const spec = isObj(def.spec) ? def.spec : {};
  const known = kind === "message" ? MESSAGE_KEYS : PYTHON_KEYS;
  const extra: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(spec)) if (!known.has(k)) extra[k] = v;
  f.extraSpec = extra;

  if (kind === "message") {
    if (spec.worker === "jev" || spec.worker === "llm") f.worker = spec.worker;
    if (typeof spec.question === "string") f.question = spec.question;
    if (spec.answer === "yes_no" || spec.answer === "choice") f.answer = spec.answer;
    if (typeof spec.threshold === "number") f.threshold = spec.threshold;
    if (isObj(spec.value)) f.value = spec.value;
    if (Array.isArray(spec.options)) f.options = spec.options.map(String);
    if (isObj(spec.values)) {
      const vals: Record<string, Value> = {};
      for (const [k, v] of Object.entries(spec.values)) if (isObj(v)) vals[k] = v;
      f.values = vals;
    }
    if (spec.context_messages !== undefined) f.context_messages = str(spec.context_messages);
    if (spec.min_chars !== undefined) f.min_chars = str(spec.min_chars);
    if (spec.max_per_run !== undefined) f.max_per_run = str(spec.max_per_run);
  } else {
    if (spec.settle_minutes !== undefined) f.settle_minutes = str(spec.settle_minutes);
    if (spec.min_messages !== undefined) f.min_messages = str(spec.min_messages);
    if (spec.since !== undefined) f.since = str(spec.since);
    if (spec.max_threads_per_run !== undefined) f.max_threads_per_run = str(spec.max_threads_per_run);
  }
  if (spec.max_cost_per_run !== undefined) f.max_cost_per_run = str(spec.max_cost_per_run);
  return f;
}

/** Client-side problems worth showing before the server's /check answers. */
export function formProblems(form: ClassifierForm, schema: ValueSchema): Record<string, string> {
  const errs: Record<string, string> = {};
  if (!NAME_RE.test(form.name.trim())) errs.name = "lowercase letters, digits and _; starts with a letter";
  if (form.attrMode === "existing" && !form.attribute) errs.attribute = "Pick an attribute or define a new one";
  if (form.attrMode === "new") {
    const a = attributeProblems(form.newAttr);
    if (a.name) errs.attribute = `Attribute name: ${a.name}`;
    else if (Object.keys(a).length) errs.attribute = Object.values(a)[0] as string;
  }
  if (form.kind === "message") {
    if (!form.question.trim()) errs.question = "Required";
    const ctx = numOrUndef(form.context_messages);
    if (ctx !== undefined && (ctx < 0 || ctx > 20 || !Number.isInteger(ctx))) errs.context_messages = "0 to 20";
    const choice = form.worker === "jev" && form.answer === "choice";
    if (choice) {
      const opts = form.options.map((o) => o.trim()).filter(Boolean);
      if (opts.length < 2) errs.options = "At least two options";
      else if (new Set(opts).size !== opts.length) errs.options = "Options must differ";
      else {
        for (const o of opts) {
          const v = form.values[o];
          if (v && Object.keys(v).length && Object.keys(validateValue(schema, v)).length) {
            errs.options = `Value for “${o}” does not fit the attribute`;
            break;
          }
        }
      }
    } else if (Object.keys(validateValue(schema, form.value)).length) {
      errs.value = "Value does not fit the attribute";
    }
    if (form.backfill === "since" && !/^\d{4}-\d{2}-\d{2}/.test(form.backfillSince)) errs.backfill = "Pick a date";
    if (form.backfill === "days" && !(Number.isInteger(Number(form.backfillDays)) && Number(form.backfillDays) > 0)) errs.backfill = "A whole number of days";
  } else if (!form.code.trim()) errs.code = "Write the classifier function";
  return errs;
}

/* ---- /check diagnostics --------------------------------------------------- */

export interface LineMessage {
  line: number | null;
  message: string;
  severity: "error" | "warning";
}

const LINE_PATTERNS = [/\bline (\d+)\b/i, /^L(\d+)\b/, /^(\d+):\d*:?\s/, /:(\d+):(\d+)?/];

/** Pull a line number out of a check message, when it names one. */
export function lineOf(message: string): number | null {
  for (const re of LINE_PATTERNS) {
    const m = re.exec(message);
    if (m && m[1]) {
      const n = Number(m[1]);
      if (Number.isInteger(n) && n > 0) return n;
    }
  }
  return null;
}

export function checkMessages(check: { errors?: string[]; warnings?: string[] } | null | undefined): LineMessage[] {
  if (!check) return [];
  return [
    ...(check.errors ?? []).map((m) => ({ line: lineOf(m), message: m, severity: "error" as const })),
    ...(check.warnings ?? []).map((m) => ({ line: lineOf(m), message: m, severity: "warning" as const })),
  ];
}

/* ---- formatting ------------------------------------------------------------ */

export function formatUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  if (n === 0) return "$0";
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
  if (Math.abs(n) < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

/** Seconds into a recording as m:ss or h:mm:ss. */
export function formatOffset(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return "";
  const t = Math.max(0, Math.floor(s));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

export function formatDuration(s: number | null | undefined): string {
  if (!s) return "";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

export type Tone = "ok" | "warn" | "bad" | "neutral";

export function statusTone(status: string): Tone {
  switch (status) {
    case "active":
    case "done":
      return "ok";
    case "proposed":
    case "partial":
    case "running":
    case "held":
      return "warn";
    case "failed":
    case "error":
    case "rejected":
      return "bad";
    default:
      return "neutral";
  }
}

/** Verdicts read as degrees: none is quiet, partial a caution, full strong. */
export function verdictTone(verdict: string | null | undefined): Tone {
  if (!verdict || verdict === "none") return "neutral";
  if (verdict === "full" || verdict === "yes" || verdict === "all") return "bad";
  return "warn";
}

/** Order verdict keys none, partial, full first, then the rest alphabetically. */
export function orderVerdicts(counts: Record<string, number>): Array<[string, number]> {
  const rank = (k: string) => (k === "none" ? 0 : k === "partial" ? 1 : k === "full" ? 2 : 3);
  return Object.entries(counts).sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]));
}

/** Parse ids typed one per line or comma-separated; numeric ids stay numbers. */
export function parseIds(raw: string): Array<string | number> {
  return raw
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => (/^\d+$/.test(s) && s.length < 16 ? Number(s) : s));
}

export function logsText(logs: string | string[] | null | undefined): string {
  if (!logs) return "";
  return Array.isArray(logs) ? logs.join("\n") : logs;
}
