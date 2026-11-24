/*
 * Turn a catalogue into completions. The pure core (`proposeCompletions`) is
 * context-aware but has no CodeMirror dependency, so it is unit-tested directly;
 * `sqlCompletionSource` wraps it as a CodeMirror CompletionSource.
 */
import type { CompletionContext, CompletionResult, Completion } from "@codemirror/autocomplete";
import { GLOBAL_PARAMS } from "@/lens";
import {
  columnsForView,
  shortViewName,
  type Catalogue,
  type CatColumn,
  ENVELOPE_COLUMNS,
} from "./catalogue";

export type SuggestionKind = "view" | "column" | "function" | "builtin" | "keyword" | "param";

export interface Suggestion {
  label: string;
  kind: SuggestionKind;
  detail?: string;
  info?: string;
  /** Text to insert instead of `label` (e.g. a function with its call parens). */
  apply?: string;
  boost?: number;
}

const SQL_KEYWORDS = [
  "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING", "LIMIT", "OFFSET",
  "JOIN", "LEFT JOIN", "INNER JOIN", "ON", "AS", "AND", "OR", "NOT", "IN", "IS",
  "NULL", "DISTINCT", "COUNT", "WITH", "UNION", "ASC", "DESC", "BETWEEN", "LIKE",
  "ILIKE", "CASE", "WHEN", "THEN", "ELSE", "END", "DESC", "INTERVAL", "DATE",
];

const PARAM_INFO: Record<string, string> = {
  from: "Start of the global time scope (:from)",
  to: "End of the global time scope (:to)",
  tz: "Active timezone (:tz)",
  person: "Focused person id (:person)",
  channel: "Focused channel name (:channel)",
  grain: "Chosen time grain (:grain)",
};

function fieldInfo(view: string, channel: string, col: CatColumn, description?: string | null): string {
  const bits = [`${col.name} · ${col.type}`, channel];
  if (description) bits.push(description);
  return bits.join("\n");
}

/** Every column we might reference: the views mentioned in `before`, plus envelope. */
function columnsInScope(cat: Catalogue, before: string): { view: string; channel: string; col: CatColumn; description?: string | null }[] {
  const out: { view: string; channel: string; col: CatColumn; description?: string | null }[] = [];
  const seen = new Set<string>();
  const re = /api\.(ch_[a-z0-9_]+)/gi;
  let m: RegExpExecArray | null;
  const mentioned = new Set<string>();
  while ((m = re.exec(before)) !== null) mentioned.add(`api.${(m[1] as string).toLowerCase()}`);
  for (const view of mentioned) {
    const v = cat.views.find((x) => x.view.toLowerCase() === view);
    if (!v) continue;
    const cols = columnsForView(cat, v.view);
    for (const col of cols) {
      const key = `${v.view}.${col.name}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const decl = v.fields.find((f) => f.name === col.name);
      out.push({ view: v.view, channel: v.channel, col, description: decl?.description });
    }
  }
  return out;
}

export interface ProposeOptions {
  /** The identifier fragment being completed (may contain a dot or leading colon). */
  word: string;
  /** All text before the cursor, for light context (FROM clause, cast, etc.). */
  before: string;
}

/**
 * Propose completions for a fragment. Context is deliberately shallow — enough to
 * prefer the right family — because the server's validator is the real safety net.
 */
export function proposeCompletions(cat: Catalogue, { word, before }: ProposeOptions): Suggestion[] {
  // :param
  if (word.startsWith(":")) {
    return GLOBAL_PARAMS.map((p) => ({
      label: `:${p}`,
      kind: "param" as const,
      detail: "parameter",
      info: PARAM_INFO[p],
      boost: 50,
    }));
  }

  const dot = word.lastIndexOf(".");
  if (dot >= 0) {
    const prefix = word.slice(0, dot).toLowerCase();
    if (prefix === "api") {
      // api.<view or function>
      const views = cat.views.map((v) => ({
        label: shortViewName(v.view),
        kind: "view" as const,
        detail: `${v.channel} · ${v.rows.toLocaleString("en-US")} rows`,
        info: v.record_kind ? `${v.channel}\n${v.record_kind}` : v.channel,
        boost: 30,
      }));
      const fns = cat.functions.map((f) => ({
        label: f.name,
        kind: "function" as const,
        detail: f.setReturning ? "set-returning" : "function",
        info: [f.signature, f.description, f.example].filter(Boolean).join("\n\n"),
        apply: `${f.name}(`,
        boost: 20,
      }));
      return [...views, ...fns];
    }
    // alias.column / view.column — offer columns of a matching view.
    const match = cat.views.find(
      (v) => shortViewName(v.view).toLowerCase() === prefix || v.channel.toLowerCase() === prefix,
    );
    const cols = match ? columnsForView(cat, match.view) : [];
    const list = cols.length ? cols : ENVELOPE_COLUMNS;
    return list.map((col) => ({
      label: col.name,
      kind: "column" as const,
      detail: col.type,
      info: match ? fieldInfo(match.view, match.channel, col, match.fields.find((f) => f.name === col.name)?.description) : undefined,
      boost: 40,
    }));
  }

  // Bare word: columns in scope first, then views, functions, builtins, keywords.
  const out: Suggestion[] = [];
  for (const c of columnsInScope(cat, before)) {
    out.push({
      label: c.col.name,
      kind: "column",
      detail: `${c.col.type} · ${c.channel}`,
      info: fieldInfo(c.view, c.channel, c.col, c.description),
      boost: 40,
    });
  }
  for (const v of cat.views) {
    out.push({
      label: v.view,
      kind: "view",
      detail: `${v.rows.toLocaleString("en-US")} rows`,
      info: v.record_kind ? `${v.channel}\n${v.record_kind}` : v.channel,
      boost: 10,
    });
  }
  for (const f of cat.functions) {
    out.push({
      label: `api.${f.name}`,
      kind: "function",
      detail: f.setReturning ? "set-returning" : "function",
      info: [f.signature, f.description, f.example].filter(Boolean).join("\n\n"),
      apply: `api.${f.name}(`,
      boost: 5,
    });
  }
  for (const b of cat.builtins) {
    out.push({ label: b, kind: "builtin", detail: "builtin", apply: `${b}(`, boost: 0 });
  }
  for (const k of SQL_KEYWORDS) {
    out.push({ label: k, kind: "keyword", boost: -10 });
  }
  return out;
}

const CM_TYPE: Record<SuggestionKind, string> = {
  view: "class",
  column: "property",
  function: "function",
  builtin: "method",
  keyword: "keyword",
  param: "variable",
};

function toCompletion(s: Suggestion): Completion {
  const c: Completion = { label: s.label, type: CM_TYPE[s.kind] };
  if (s.detail) c.detail = s.detail;
  if (s.info) c.info = s.info;
  if (s.apply) c.apply = s.apply;
  if (s.boost !== undefined) c.boost = s.boost;
  return c;
}

/** Build a CodeMirror completion source bound to a (possibly updating) catalogue. */
export function sqlCompletionSource(getCat: () => Catalogue) {
  return (context: CompletionContext): CompletionResult | null => {
    const token = context.matchBefore(/[:A-Za-z_][\w.:]*/);
    if (!token && !context.explicit) return null;
    const word = token ? token.text : "";
    const from = token ? token.from : context.pos;
    const before = context.state.sliceDoc(0, context.pos);
    const suggestions = proposeCompletions(getCat(), { word, before });
    if (!suggestions.length) return null;
    return {
      from,
      options: suggestions.map(toCompletion),
      validFor: /^[:\w.]*$/,
    };
  };
}
