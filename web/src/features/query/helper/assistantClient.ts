/*
 * Client for the query helper (docs/web/assistant.md). A run is started with
 * POST /v1/web/assistant and followed by long-polling GET /v1/web/assistant/{run}
 * with ?after=<seq>&wait=<s>. The server answers as soon as a new event lands, the
 * run ends, or `wait` seconds pass; we re-request from the last seq we saw.
 *
 * The transport is injected so the long-poll loop is unit-tested with a fake fetch
 * (no network, no timers). When the assistant routes are missing (404) the caller
 * falls back to the local finder; `AssistantUnavailable` marks that case.
 */
import { request } from "@/api";
import { ApiError } from "@/api/errors";

export type AssistantVerb = "ask" | "fix" | "explain" | "refine";
export type RunStatus = "running" | "done" | "failed" | "cancelled";
export type Tier = "schema" | "samples";

export interface ResultShape {
  columns: { name: string; type: string }[];
  row_count: number;
  truncated: boolean;
}

export interface StartRequest {
  verb: AssistantVerb;
  question?: string;
  statement?: string;
  selection?: string;
  error?: { code?: string | null; message?: string } | null;
  result?: ResultShape | null;
  tier?: Tier;
  scope?: { from?: string | null; to?: string | null; tz?: string };
  channel?: string | null;
  history?: { role: "user" | "assistant"; text: string }[];
}

export interface StartResponse {
  run: string;
  status: RunStatus;
  provider: string;
  model: string | null;
  tier: Tier;
}

export interface StepEvent {
  seq: number;
  at?: string;
  kind: "step";
  tool: string;
  input?: Record<string, unknown>;
  sql?: string;
  row_count?: number;
  columns?: { name: string; type: string }[];
  error?: string | null;
  summary?: string;
  sent?: string;
  ms?: number;
}

export interface TextEvent {
  seq: number;
  kind: "text";
  text: string;
}

export interface MarkerEvent {
  seq: number;
  kind: "final" | string;
  [k: string]: unknown;
}

export type RunEvent = StepEvent | TextEvent | MarkerEvent;

export interface RunFinal {
  answer?: string;
  statement?: string;
  spec?: unknown;
  notes?: string[];
}

export interface RunState {
  run: string;
  status: RunStatus;
  provider: string;
  model: string | null;
  tier: Tier;
  events: RunEvent[];
  final: RunFinal | null;
  usage: { tokens_in?: number; tokens_out?: number; cost_usd?: number; steps?: number } | null;
  error: string | null;
}

export class AssistantUnavailable extends Error {
  constructor() {
    super("assistant routes not available");
    this.name = "AssistantUnavailable";
  }
}

export type Transport = typeof request;

/** POST a run. Throws `AssistantUnavailable` when the route is missing (404). */
export async function startRun(body: StartRequest, transport: Transport = request): Promise<StartResponse> {
  try {
    return await transport<StartResponse>("/web/assistant", { method: "POST", body });
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) throw new AssistantUnavailable();
    throw err;
  }
}

export async function cancelRun(run: string, transport: Transport = request): Promise<void> {
  try {
    await transport(`/web/assistant/${encodeURIComponent(run)}/cancel`, { method: "POST" });
  } catch {
    /* a cancel on an already-finished run is harmless */
  }
}

function isTerminal(status: RunStatus): boolean {
  return status === "done" || status === "failed" || status === "cancelled";
}

function mergeEvents(into: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  if (incoming.length === 0) return into;
  const seen = new Set(into.map((e) => e.seq));
  const merged = into.slice();
  for (const e of incoming) {
    if (!seen.has(e.seq)) {
      merged.push(e);
      seen.add(e.seq);
    }
  }
  merged.sort((a, b) => a.seq - b.seq);
  return merged;
}

export interface FollowOptions {
  wait?: number;
  signal?: AbortSignal;
  transport?: Transport;
}

/**
 * Long-poll a run to completion, yielding a fresh snapshot after every response.
 * Consumers (the React hook) render each snapshot. Stops on a terminal status, a
 * 404 (server restarted → run gone), or an abort.
 */
export async function* followRun(run: string, opts: FollowOptions = {}): AsyncGenerator<RunState> {
  const wait = opts.wait ?? 20;
  const transport = opts.transport ?? request;
  let after = 0;
  let events: RunEvent[] = [];

  for (;;) {
    if (opts.signal?.aborted) return;
    let res: RunState;
    try {
      res = await transport<RunState>(
        `/web/assistant/${encodeURIComponent(run)}?after=${after}&wait=${wait}`,
        { signal: opts.signal },
      );
    } catch (err) {
      if (opts.signal?.aborted) return;
      if (err instanceof ApiError && err.status === 404) {
        yield {
          run,
          status: "failed",
          provider: "",
          model: null,
          tier: "schema",
          events,
          final: null,
          usage: null,
          error: "This run is no longer available (the server may have restarted).",
        };
        return;
      }
      throw err;
    }

    events = mergeEvents(events, res.events ?? []);
    for (const e of events) if (e.seq > after) after = e.seq;

    const snapshot: RunState = { ...res, events };
    yield snapshot;

    if (isTerminal(res.status)) return;
  }
}
