/*
 * Direct access to POST /v1/sql {validate_only:true}, which answers
 * {valid, error, code, columns} (foundation-reports §2). The shared client's
 * `validateSql` drops `code` and `columns`; the editor needs both — `code` for a
 * friendly diagnostic, `columns` to learn a view's real typed columns cheaply.
 */
import { request } from "@/api";
import { ApiError } from "@/api/errors";
import type { CatColumn } from "./catalogue";

export interface ValidateResult {
  valid: boolean;
  error: string | null;
  code: string | null;
  columns: CatColumn[];
}

interface ValidateResponse {
  valid?: boolean;
  error?: string | null;
  code?: string | null;
  columns?: CatColumn[];
  detail?: string;
}

export async function validateStatement(statement: string, signal?: AbortSignal): Promise<ValidateResult> {
  try {
    const res = await request<ValidateResponse>("/sql", {
      method: "POST",
      body: { statement, validate_only: true },
      signal,
    });
    if (typeof res.valid === "boolean") {
      return {
        valid: res.valid,
        error: res.error ?? null,
        code: res.code ?? null,
        columns: Array.isArray(res.columns) ? res.columns : [],
      };
    }
    // Older server with no validate_only: a 200 means it ran — treat as valid.
    return { valid: true, error: null, code: null, columns: [] };
  } catch (err) {
    if (err instanceof ApiError) {
      const text = typeof err.detail === "string" ? err.detail : err.message;
      return { valid: false, error: text, code: err.code ?? null, columns: [] };
    }
    return { valid: false, error: err instanceof Error ? err.message : String(err), code: null, columns: [] };
  }
}

/** The real typed columns of a view, via a zero-row validate. */
export async function fetchViewColumns(view: string, signal?: AbortSignal): Promise<CatColumn[]> {
  const res = await validateStatement(`SELECT * FROM ${view} LIMIT 0`, signal);
  return res.columns;
}
