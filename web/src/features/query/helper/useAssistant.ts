/*
 * The helper run lifecycle. Reads provider/model/tier from the bootstrap, starts a
 * run, then long-polls it to completion, exposing each snapshot so the panel can show
 * the live step list. When the assistant routes are missing (404) it flips to
 * `unavailable` so the panel falls back to the local finder. A model is "local" when
 * the provider says so — then no run is started at all.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useBootstrap } from "@/api";
import {
  startRun,
  cancelRun,
  followRun,
  AssistantUnavailable,
  type RunState,
  type StartRequest,
  type Tier,
} from "./assistantClient";

export interface AssistantConfig {
  provider: string;
  model: string | null;
  tier: Tier;
  tiers: Tier[];
  configured: boolean;
  isLocal: boolean;
}

interface RawAssistant {
  provider?: string | null;
  model?: string | null;
  loopback?: boolean;
  default_tier?: Tier | null;
  tiers?: Tier[];
  configured?: boolean;
}

export function useAssistantConfig(): AssistantConfig {
  const { data: boot } = useBootstrap();
  const a = (boot?.features.assistant ?? {}) as RawAssistant;
  const provider = a.provider ?? "local";
  return {
    provider,
    model: a.model ?? null,
    tier: a.default_tier ?? "schema",
    tiers: a.tiers ?? ["schema"],
    configured: a.configured ?? provider !== "local",
    isLocal: provider === "local" || !a.model,
  };
}

export interface AssistantRun {
  run: RunState | null;
  running: boolean;
  unavailable: boolean;
  start: (req: StartRequest) => Promise<void>;
  cancel: () => void;
  reset: () => void;
}

export function useAssistant(): AssistantRun {
  const [run, setRun] = useState<RunState | null>(null);
  const [running, setRunning] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const runIdRef = useRef<string | null>(null);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const start = useCallback(async (req: StartRequest): Promise<void> => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setRunning(true);
    setRun(null);
    try {
      const started = await startRun(req);
      runIdRef.current = started.run;
      setRun({
        run: started.run,
        status: started.status,
        provider: started.provider,
        model: started.model,
        tier: started.tier,
        events: [],
        final: null,
        usage: null,
        error: null,
      });
      for await (const snapshot of followRun(started.run, { signal: ctrl.signal })) {
        if (ctrl.signal.aborted) break;
        setRun(snapshot);
        if (snapshot.status !== "running") break;
      }
    } catch (err) {
      if (err instanceof AssistantUnavailable) {
        setUnavailable(true);
      } else if (!ctrl.signal.aborted) {
        setRun((prev) => ({
          run: prev?.run ?? "",
          status: "failed",
          provider: prev?.provider ?? "",
          model: prev?.model ?? null,
          tier: prev?.tier ?? "schema",
          events: prev?.events ?? [],
          final: null,
          usage: null,
          error: err instanceof Error ? err.message : String(err),
        }));
      }
    } finally {
      if (!ctrl.signal.aborted) setRunning(false);
    }
  }, []);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    const id = runIdRef.current;
    if (id) void cancelRun(id);
    setRunning(false);
    setRun((prev) => (prev ? { ...prev, status: prev.status === "running" ? "cancelled" : prev.status } : prev));
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setRun(null);
    setRunning(false);
  }, []);

  return { run, running, unavailable, start, cancel, reset };
}
