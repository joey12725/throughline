/*
 * Arrivals long poll. GET /v1/web/arrivals answers as soon as something lands
 * (plain JSON request/response — no push routes, per the core's rule). Without
 * `after` it returns the newest rows and a cursor immediately. Until the backend
 * route lands the dev server 404s; we return an `unsupported` page so Live can
 * show a designed "waiting for a current server" state instead of breaking.
 */
import { ApiError } from "./errors";
import { request } from "./client";
import type { ArrivalsPage } from "./types";

export interface ArrivalsQuery {
  after?: string | null;
  /** seconds 0..25 the server may hold the request open */
  wait?: number;
  limit?: number;
  channels?: readonly string[];
  signal?: AbortSignal;
}

const EMPTY_UNSUPPORTED: ArrivalsPage = {
  cursor: "",
  rows: [],
  counts: {},
  sampled: false,
  plugins: [],
  reads: [],
  unsupported: true,
};

export async function fetchArrivals(q: ArrivalsQuery = {}): Promise<ArrivalsPage> {
  const params = new URLSearchParams();
  if (q.after) params.set("after", q.after);
  if (q.wait !== undefined) params.set("wait", String(clamp(q.wait, 0, 25)));
  if (q.limit !== undefined) params.set("limit", String(q.limit));
  if (q.channels && q.channels.length) params.set("channels", q.channels.join(","));
  const qs = params.toString();
  try {
    return await request<ArrivalsPage>(`/web/arrivals${qs ? `?${qs}` : ""}`, { signal: q.signal });
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return EMPTY_UNSUPPORTED;
    throw err;
  }
}

function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}

/**
 * Imperative long-poll loop: calls `onPage` for each page until `signal` aborts
 * or the route is unsupported. Backs off on error so a flaky server does not
 * hot-loop the audit log. Returns when polling stops.
 */
export async function longPollArrivals(
  onPage: (page: ArrivalsPage) => void,
  opts: { channels?: readonly string[]; wait?: number; signal: AbortSignal },
): Promise<void> {
  let cursor: string | null = null;
  let backoff = 1000;
  while (!opts.signal.aborted) {
    try {
      const page = await fetchArrivals({
        after: cursor,
        wait: opts.wait ?? 25,
        channels: opts.channels,
        signal: opts.signal,
      });
      if (opts.signal.aborted) return;
      onPage(page);
      if (page.unsupported) return; // nothing to poll on an older server
      cursor = page.cursor || cursor;
      backoff = 1000;
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      await sleep(backoff, opts.signal);
      backoff = Math.min(backoff * 2, 30_000);
    }
  }
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });
}
