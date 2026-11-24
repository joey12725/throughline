/*
 * Parse a dropped file in the browser and learn the shape of its columns.
 * CSV/TSV go through papaparse; JSON is an array of objects or NDJSON (one object
 * per line). We then infer each column's type (the six the channel contract
 * allows) and a few statistics — uniqueness, average text length, fill rate —
 * that the mapping defaults lean on. Pure and tested (parse.test.ts).
 */
import Papa from "papaparse";
import type { FieldType } from "@/lens";

export type FileKind = "csv" | "tsv" | "json" | "ndjson";

export interface ParsedTable {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  kind: FileKind;
  truncated: boolean;
}

export interface InferredColumn {
  name: string;
  type: FieldType;
  /** 0..1 distinct / non-null. */
  uniqueness: number;
  /** 0..1 non-null. */
  fill: number;
  /** mean length of string values. */
  avgLen: number;
  /** a few example values, for the mapping UI. */
  samples: string[];
  /** true when values parse as ISO/epoch timestamps. */
  timeish: boolean;
}

export interface ParseResult {
  table: ParsedTable;
  columns: InferredColumn[];
}

const MAX_PREVIEW_ROWS = 2000;

export function detectKind(filename: string, text: string): FileKind {
  const lower = filename.toLowerCase();
  if (lower.endsWith(".tsv")) return "tsv";
  if (lower.endsWith(".ndjson") || lower.endsWith(".jsonl")) return "ndjson";
  if (lower.endsWith(".json")) return "json";
  if (lower.endsWith(".csv")) return "csv";
  // Sniff: a leading [ or { suggests JSON; a tab in the first line suggests TSV.
  const head = text.slice(0, 2000).trimStart();
  if (head.startsWith("[") || head.startsWith("{")) {
    const firstLine = head.split("\n", 1)[0] ?? "";
    return firstLine.trim().endsWith("}") && head.startsWith("{") ? "ndjson" : "json";
  }
  const firstLine = text.split("\n", 1)[0] ?? "";
  if (firstLine.includes("\t")) return "tsv";
  return "csv";
}

export function parseText(filename: string, text: string): ParsedTable {
  const kind = detectKind(filename, text);
  if (kind === "json") return parseJsonArray(text);
  if (kind === "ndjson") return parseNdjson(text);
  return parseDelimited(text, kind === "tsv" ? "\t" : ",");
}

function parseDelimited(text: string, delimiter: string): ParsedTable {
  const out = Papa.parse<Record<string, unknown>>(text, {
    header: true,
    delimiter,
    skipEmptyLines: "greedy",
    preview: MAX_PREVIEW_ROWS + 1,
    dynamicTyping: false,
  });
  const rows = (out.data ?? []).filter((r) => r && typeof r === "object");
  const truncated = rows.length > MAX_PREVIEW_ROWS;
  const kept = truncated ? rows.slice(0, MAX_PREVIEW_ROWS) : rows;
  const columns = (out.meta.fields ?? collectKeys(kept)).filter((c) => c !== "");
  return { columns, rows: kept, kind: delimiter === "\t" ? "tsv" : "csv", truncated };
}

function parseJsonArray(text: string): ParsedTable {
  const data = JSON.parse(text);
  const arr = Array.isArray(data) ? data : Array.isArray(data?.data) ? data.data : [data];
  const objs = arr.filter((r: unknown): r is Record<string, unknown> => !!r && typeof r === "object" && !Array.isArray(r));
  const truncated = objs.length > MAX_PREVIEW_ROWS;
  const kept = truncated ? objs.slice(0, MAX_PREVIEW_ROWS) : objs;
  return { columns: collectKeys(kept), rows: kept, kind: "json", truncated };
}

function parseNdjson(text: string): ParsedTable {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const objs: Array<Record<string, unknown>> = [];
  for (const line of lines) {
    if (objs.length >= MAX_PREVIEW_ROWS + 1) break;
    try {
      const o = JSON.parse(line);
      if (o && typeof o === "object" && !Array.isArray(o)) objs.push(o as Record<string, unknown>);
    } catch {
      /* skip a malformed line */
    }
  }
  const truncated = objs.length > MAX_PREVIEW_ROWS;
  const kept = truncated ? objs.slice(0, MAX_PREVIEW_ROWS) : objs;
  return { columns: collectKeys(kept), rows: kept, kind: "ndjson", truncated };
}

function collectKeys(rows: Array<Record<string, unknown>>): string[] {
  const seen = new Set<string>();
  const order: string[] = [];
  for (const r of rows) {
    for (const k of Object.keys(r)) {
      if (!seen.has(k)) {
        seen.add(k);
        order.push(k);
      }
    }
  }
  return order;
}

/* ------------------------------------------------------------------ inference */

const TS_RE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$/;

function looksTimestamp(v: string): boolean {
  if (TS_RE.test(v.trim())) return true;
  // epoch seconds or ms (10 or 13 digits)
  if (/^\d{10}$/.test(v) || /^\d{13}$/.test(v)) return true;
  return false;
}

function classify(values: string[]): { type: FieldType; timeish: boolean } {
  let allInt = true;
  let allNum = true;
  let allBool = true;
  let allTime = true;
  let allJson = true;
  let any = false;
  for (const raw of values) {
    const v = raw.trim();
    if (v === "") continue;
    any = true;
    if (!/^-?\d+$/.test(v)) allInt = false;
    if (!/^-?\d*\.?\d+(e[+-]?\d+)?$/i.test(v)) allNum = false;
    if (!/^(true|false|t|f|yes|no|0|1)$/i.test(v)) allBool = false;
    if (!looksTimestamp(v)) allTime = false;
    if (!(v.startsWith("{") || v.startsWith("["))) allJson = false;
  }
  if (!any) return { type: "text", timeish: false };
  if (allTime) return { type: "timestamp", timeish: true };
  if (allBool && !allInt) return { type: "boolean", timeish: false };
  if (allInt) return { type: "integer", timeish: false };
  if (allNum) return { type: "number", timeish: false };
  if (allJson) return { type: "json", timeish: false };
  return { type: "text", timeish: values.some((v) => looksTimestamp(v.trim())) };
}

export function inferColumns(table: ParsedTable): InferredColumn[] {
  return table.columns.map((name) => {
    const raw = table.rows.map((r) => r[name]);
    const strings = raw
      .filter((v) => v !== null && v !== undefined && v !== "")
      .map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v)));
    const fill = table.rows.length ? strings.length / table.rows.length : 0;
    const distinct = new Set(strings).size;
    const uniqueness = strings.length ? distinct / strings.length : 0;
    const avgLen = strings.length ? strings.reduce((s, v) => s + v.length, 0) / strings.length : 0;
    const objCount = raw.filter((v) => v !== null && typeof v === "object").length;
    const base = classify(strings);
    const type: FieldType = objCount > strings.length / 2 && objCount > 0 ? "json" : base.type;
    return {
      name,
      type,
      uniqueness,
      fill,
      avgLen,
      samples: strings.slice(0, 3),
      timeish: base.timeish,
    };
  });
}

export function parseFile(filename: string, text: string): ParseResult {
  const table = parseText(filename, text);
  return { table, columns: inferColumns(table) };
}
