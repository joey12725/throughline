/*
 * The fetch client. Talks only to this app's own origin (/v1/...). Sends the
 * Authorization bearer and X-Throughline-Client: web on every request, plus
 * X-Throughline-Asker when the owner is acting as someone. Normalises the three
 * error shapes and signs out on 401. Supports AbortSignal.
 */
import { auth } from "@/store/auth";
import { ApiError, apiErrorFromBody } from "./errors";

export const CLIENT_HEADER = "X-Throughline-Client";
export const CLIENT_VALUE = "web";

/** All requests are relative to the app origin. The API lives under /v1. */
const BASE = "/v1";

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Suppress the automatic sign-out on 401 (used by the token-probe path). */
  noAuthRedirect?: boolean;
  /** Extra headers (e.g. per-request asker override). */
  headers?: Record<string, string>;
}

let onUnauthorized: (() => void) | null = null;
/** The shell registers what to do when a request comes back 401. */
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

export async function request<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const token = auth.token();
  const headers: Record<string, string> = {
    Accept: "application/json",
    [CLIENT_HEADER]: CLIENT_VALUE,
    ...opts.headers,
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  const asker = auth.asker();
  if (asker && !("X-Throughline-Asker" in headers)) headers["X-Throughline-Asker"] = asker;

  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: opts.method ?? (opts.body !== undefined ? "POST" : "GET"),
      headers,
      body,
      signal: opts.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError({ status: 0, message: "could not reach the server", detail: String(err) });
  }

  if (res.status === 401) {
    if (!opts.noAuthRedirect) {
      auth.signOut();
      onUnauthorized?.();
    }
    const parsed = await safeJson(res);
    throw apiErrorFromBody(401, parsed);
  }

  if (res.status === 204) return undefined as T;

  const parsed = await safeJson(res);
  if (!res.ok) throw apiErrorFromBody(res.status, parsed);
  return parsed as T;
}

async function safeJson(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

/** Parse a server timestamp, whether it ends in `Z` or `+00:00`. */
export function parseServerTime(ts: string | null | undefined): Date | null {
  if (!ts) return null;
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? null : d;
}
