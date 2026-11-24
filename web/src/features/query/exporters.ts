/*
 * Result exporters: CSV/TSV/JSON of the rows in the browser, and "copy as CLI" —
 * the exact `throughline sql '<statement>'` the box would run (the CLI command is
 * `sql` with a positional statement; see src/throughline/cli.py). Pure string
 * builders, unit-tested.
 */
import type { Row, SqlColumn } from "@/api/types";

function cellToText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function escapeDelimited(value: string, delimiter: string): string {
  if (value.includes(delimiter) || value.includes("\n") || value.includes('"')) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

export function toDelimited(
  rows: readonly Row[],
  columns: readonly SqlColumn[],
  delimiter: "," | "\t",
): string {
  const names = columns.length ? columns.map((c) => c.name) : Object.keys(rows[0] ?? {});
  const header = names.map((n) => escapeDelimited(n, delimiter)).join(delimiter);
  const body = rows.map((r) =>
    names.map((n) => escapeDelimited(cellToText((r as Record<string, unknown>)[n]), delimiter)).join(delimiter),
  );
  return [header, ...body].join("\n");
}

export function toCsv(rows: readonly Row[], columns: readonly SqlColumn[]): string {
  return toDelimited(rows, columns, ",");
}

export function toTsv(rows: readonly Row[], columns: readonly SqlColumn[]): string {
  return toDelimited(rows, columns, "\t");
}

export function toJson(rows: readonly Row[]): string {
  return JSON.stringify(rows, null, 2);
}

/** The CLI invocation that reproduces this run on the box. */
export function toCliCommand(statement: string): string {
  // Single-quote the statement for a POSIX shell, escaping embedded single quotes.
  const quoted = `'${statement.replace(/'/g, `'\\''`)}'`;
  return `throughline sql ${quoted}`;
}
