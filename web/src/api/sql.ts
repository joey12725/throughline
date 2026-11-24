/*
 * Typed SQL access. POST /v1/sql returns {rows, row_count, truncated, execution}
 * and, on a current server, {columns:[{name,type}]}. `validate_only` lints a
 * statement without running it. Everything goes through the shared scheduler
 * (<=4 concurrent) so a panel-heavy screen does not swamp the audit log.
 *
 * Postgres `numeric` arrives as a string (map-http.md §1.5). `num()` coerces one
 * centrally when a caller wants a JS number; precision-sensitive callers keep
 * the string. Columns' Postgres types let callers decide.
 */
import { request } from "./client";
import { sqlScheduler, type Priority } from "./scheduler";
import type { ExecutionEnvelope, Row, SqlColumn, SqlResult, SqlValidation } from "./types";

export interface SqlOptions {
  rowCap?: number;
  deadlineMs?: number;
  tokenBudget?: number;
  trace?: boolean;
  strictExact?: boolean;
  priority?: Priority;
  signal?: AbortSignal;
}

interface SqlResponse {
  rows: Row[];
  columns?: SqlColumn[];
  truncated: boolean;
  row_count: number;
  execution: ExecutionEnvelope;
}

export function sql<T = Row>(
  statement: string,
  opts: SqlOptions = {},
): Promise<SqlResult<T>> {
  return sqlScheduler.schedule<SqlResult<T>>(
    async (signal) => {
      const body: Record<string, unknown> = { statement };
      if (opts.rowCap !== undefined) body.row_cap = opts.rowCap;
      if (opts.deadlineMs !== undefined) body.deadline_ms = opts.deadlineMs;
      if (opts.tokenBudget !== undefined) body.token_budget = opts.tokenBudget;
      if (opts.trace) body.trace = true;
      if (opts.strictExact) body.strict_exact = true;

      const res = await request<SqlResponse>("/sql", { method: "POST", body, signal });
      return {
        rows: res.rows as unknown as readonly T[],
        columns: res.columns ?? inferColumns(res.rows),
        truncated: res.truncated,
        rowCount: res.row_count,
        execution: res.execution,
      };
    },
    { priority: opts.priority, signal: opts.signal },
  );
}

/** Lint a statement with `validate_only`; no rows run, no audit of a result. */
export async function validateSql(statement: string, signal?: AbortSignal): Promise<SqlValidation> {
  try {
    const res = await request<Partial<SqlValidation> & { detail?: string }>("/sql", {
      method: "POST",
      body: { statement, validate_only: true },
      signal,
    });
    // A current server answers {valid, error}. An older one that lacks
    // validate_only ignores it and actually runs; treat a 200 as valid.
    if (typeof res.valid === "boolean") return { valid: res.valid, error: res.error ?? null };
    return { valid: true, error: null };
  } catch (err) {
    return { valid: false, error: err instanceof Error ? err.message : String(err) };
  }
}

/** Coerce a numeric-string (or number) cell to a JS number. */
export function num(cell: unknown): number {
  if (typeof cell === "number") return cell;
  if (typeof cell === "string") {
    const n = Number(cell);
    return Number.isFinite(n) ? n : NaN;
  }
  return NaN;
}

/** Fallback column inference for older servers that omit `columns`. */
function inferColumns(rows: readonly Row[]): SqlColumn[] {
  const first = rows[0];
  if (!first) return [];
  return Object.keys(first).map((name) => ({ name, type: pgType(first[name]) }));
}

function pgType(v: unknown): string {
  if (v === null) return "unknown";
  if (typeof v === "number") return Number.isInteger(v) ? "bigint" : "double precision";
  if (typeof v === "boolean") return "boolean";
  if (Array.isArray(v)) return "array";
  if (typeof v === "object") return "jsonb";
  if (typeof v === "string") {
    if (/^-?\d+\.\d+$/.test(v)) return "numeric";
    if (/^\d{4}-\d{2}-\d{2}T/.test(v)) return "timestamptz";
    if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return "date";
  }
  return "text";
}
