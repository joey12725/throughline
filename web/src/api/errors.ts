/*
 * The three server error shapes (see brief map-http.md §3):
 *   400 { detail: "<text>" }                             — ValueError / validator
 *   400 { detail: "database operation rejected", code }  — psycopg, with SQLSTATE
 *   422 { detail: [ { type, loc, msg, input, ctx? } ] }  — pydantic body/query
 *   403/404/409 { detail: string | object }
 * `detail` may be a string, a list, or an object. ApiError normalises all of it.
 */

export interface PydanticIssue {
  readonly type: string;
  readonly loc: ReadonlyArray<string | number>;
  readonly msg: string;
  readonly input?: unknown;
  readonly ctx?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;
  /** SQLSTATE for "database operation rejected", else null. */
  readonly code: string | null;
  /** Structured pydantic issues for 422, else null. */
  readonly issues: readonly PydanticIssue[] | null;
  /** The raw `detail` as the server sent it. */
  readonly detail: unknown;

  constructor(opts: {
    status: number;
    message: string;
    code?: string | null;
    issues?: readonly PydanticIssue[] | null;
    detail?: unknown;
  }) {
    super(opts.message);
    this.name = "ApiError";
    this.status = opts.status;
    this.code = opts.code ?? null;
    this.issues = opts.issues ?? null;
    this.detail = opts.detail ?? null;
  }

  /** True when authentication failed — the UI returns to the sign-in screen. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** One-line human summary for toasts. */
  get short(): string {
    if (this.code) return `${this.message} (SQLSTATE ${this.code})`;
    return this.message;
  }
}

/** Build an ApiError from a parsed JSON body (any shape) and the HTTP status. */
export function apiErrorFromBody(status: number, body: unknown): ApiError {
  // Database rejection carries a SQLSTATE code alongside the message.
  if (isRecord(body) && typeof body.code === "string" && typeof body.detail === "string") {
    return new ApiError({ status, message: body.detail, code: body.code, detail: body });
  }
  const detail = isRecord(body) ? body.detail : body;

  if (typeof detail === "string") {
    return new ApiError({ status, message: detail, detail });
  }
  if (Array.isArray(detail)) {
    const issues = detail.filter(isPydanticIssue);
    const message = issues.length
      ? issues.map((i) => `${i.loc.filter((p) => p !== "body").join(".")}: ${i.msg}`).join("; ")
      : "request rejected";
    return new ApiError({ status, message, issues, detail });
  }
  if (isRecord(detail)) {
    // Object detail (e.g. { reason: "hash_conflict", current_hash } or egress)
    const message =
      typeof detail.reason === "string" ? String(detail.reason) : `request failed (${status})`;
    return new ApiError({ status, message, detail });
  }
  return new ApiError({ status, message: `request failed (${status})`, detail: body });
}

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

function isPydanticIssue(x: unknown): x is PydanticIssue {
  return isRecord(x) && typeof x.msg === "string" && Array.isArray(x.loc);
}
