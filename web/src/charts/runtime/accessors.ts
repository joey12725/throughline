/*
 * Reading values out of result rows by the encode channels of a view spec.
 * Postgres numeric arrives as a string; time as an ISO string; arrays as arrays.
 * These coerce centrally so every renderer treats cells the same way.
 */
import { num } from "@/api";
import type { Row, ViewSpec } from "@/lens";

/** The single column bound to a lens channel, or undefined. */
export function col1(spec: ViewSpec, channel: string): string | undefined {
  const v = spec.encode[channel];
  if (typeof v === "string") return v;
  if (Array.isArray(v)) return v[0];
  return undefined;
}

/** The list of columns bound to a lens channel (one or many). */
export function colN(spec: ViewSpec, channel: string): string[] {
  const v = spec.encode[channel];
  if (typeof v === "string") return [v];
  if (Array.isArray(v)) return [...v];
  return [];
}

export function numAt(row: Row, col: string | undefined): number {
  if (!col) return NaN;
  return num(row[col]);
}

export function strAt(row: Row, col: string | undefined): string {
  if (!col) return "";
  const v = row[col];
  if (v === null || v === undefined) return "";
  return typeof v === "string" ? v : String(v);
}

/** A cell that may be null, kept as-is for "no value" handling. */
export function rawAt(row: Row, col: string | undefined): unknown {
  if (!col) return undefined;
  return row[col];
}

export function timeMsAt(row: Row, col: string | undefined): number {
  if (!col) return NaN;
  const v = row[col];
  if (v == null) return NaN;
  if (typeof v === "number") return v;
  const t = Date.parse(String(v));
  return Number.isNaN(t) ? NaN : t;
}

export function boolAt(row: Row, col: string | undefined): boolean | null {
  if (!col) return null;
  const v = row[col];
  if (v === null || v === undefined) return null;
  if (typeof v === "boolean") return v;
  if (typeof v === "string") return v === "t" || v === "true" || v === "1";
  if (typeof v === "number") return v !== 0;
  return null;
}

/** A JSON array cell as a plain array (parsing a string if the server sent text). */
export function arrAt(row: Row, col: string | undefined): unknown[] {
  if (!col) return [];
  const v = row[col];
  if (Array.isArray(v)) return v;
  if (typeof v === "string") {
    try {
      const p = JSON.parse(v);
      return Array.isArray(p) ? p : [];
    } catch {
      return [];
    }
  }
  return [];
}

/** unit (from options.units[field]) for a bound column. */
export function unitOf(spec: ViewSpec, col: string | undefined): string | undefined {
  if (!col) return undefined;
  const units = (spec.options?.units ?? {}) as Record<string, string>;
  return units[col];
}

export function optStr(spec: ViewSpec, key: string): string | undefined {
  const v = spec.options?.[key];
  return typeof v === "string" ? v : undefined;
}

export function optNum(spec: ViewSpec, key: string): number | undefined {
  const v = spec.options?.[key];
  return typeof v === "number" ? v : undefined;
}

export function optArr(spec: ViewSpec, key: string): unknown[] | undefined {
  const v = spec.options?.[key];
  return Array.isArray(v) ? v : undefined;
}
