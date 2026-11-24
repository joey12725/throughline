/*
 * The Live stream: a long-poll loop over GET /v1/web/arrivals?after=…&wait=25
 * (plain request/response — no push routes), with the error handling the brief
 * calls for: 429 Retry-After, 400 bad-cursor reset, network backoff, and a
 * pause when the tab is hidden. It also keeps a rolling per-plugin arrival rate
 * and can replay the last 15 minutes of real ingestion at 10×.
 *
 * Built on fetchArrivals from @/api (read: web/src/api/arrivals.ts); this feature
 * owns the controller so it can pause, filter and replay.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchArrivals, sql, ApiError } from "@/api";
import type { ArrivalRow, ArrivalsPage, ReadRow } from "@/api/types";

interface ArrivalsPageExt extends ArrivalsPage {
  horizon?: string;
  settle_s?: number;
}

const MAX_ROWS = 600;
const RATE_WINDOW_MS = 10 * 60_000; // keep 10 minutes of arrivals for the strip
const BUCKET_MS = 10_000; // 10-second buckets
const BUCKETS = RATE_WINDOW_MS / BUCKET_MS;

export interface PluginRate {
  plugin: string;
  buckets: number[]; // oldest -> newest, length BUCKETS
  perMin: number; // arrivals in the last 60 s
  last: number; // ms of most recent arrival (for the pulse)
}

export interface LiveState {
  rows: ArrivalRow[];
  reads: ReadRow[];
  ratePlugins: PluginRate[];
  totalPerMin: number;
  settleHorizon: number | null;
  settleS: number | null;
  connected: boolean;
  paused: boolean;
  hidden: boolean;
  error: ApiError | null;
  setPaused: (b: boolean) => void;
  clear: () => void;
  replay: () => void;
  replaying: boolean;
  replayProgress: number;
}

export function useLiveStream(pluginOf: (channel: string) => string): LiveState {
  const [rows, setRows] = useState<ArrivalRow[]>([]);
  const [reads, setReads] = useState<ReadRow[]>([]);
  const [connected, setConnected] = useState(false);
  const [paused, setPaused] = useState(false);
  const [hidden, setHidden] = useState(() => typeof document !== "undefined" && document.hidden);
  const [error, setError] = useState<ApiError | null>(null);
  const [settleHorizon, setSettleHorizon] = useState<number | null>(null);
  const [settleS, setSettleS] = useState<number | null>(null);
  const [tick, setTick] = useState(0); // 1 Hz, so the rate strip animates
  const [replaying, setReplaying] = useState(false);
  const [replayProgress, setReplayProgress] = useState(0);

  // Rolling arrival log for the rate strip (kept in a ref; rendered on `tick`).
  const logRef = useRef<{ plugin: string; ms: number }[]>([]);
  const pluginOfRef = useRef(pluginOf);
  pluginOfRef.current = pluginOf;

  const active = !paused && !hidden && !replaying;
  const seededRef = useRef(false);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  // Track tab visibility → pause while hidden.
  useEffect(() => {
    const onVis = (): void => setHidden(document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  // 1 Hz tick to keep the rate strip live and prune the log.
  useEffect(() => {
    const t = setInterval(() => {
      const cutoff = Date.now() - RATE_WINDOW_MS;
      const log = logRef.current;
      let i = 0;
      while (i < log.length && log[i]!.ms < cutoff) i++;
      if (i > 0) logRef.current = log.slice(i);
      setTick((x) => x + 1);
    }, 1000);
    return () => clearInterval(t);
  }, []);

  // Reads, settle horizon and settle window from a page — no row ingestion.
  const ingestMeta = useCallback((page: ArrivalsPageExt) => {
    if (page.reads && page.reads.length) {
      setReads((prev) => [...[...page.reads].reverse(), ...prev].slice(0, 200));
    }
    if (page.horizon) {
      const h = Date.parse(page.horizon);
      if (!Number.isNaN(h)) setSettleHorizon(h);
    }
    if (typeof page.settle_s === "number") setSettleS(page.settle_s);
  }, []);

  const ingest = useCallback((page: ArrivalsPageExt) => {
    const newRows = page.rows;
    if (newRows.length) {
      // Record arrivals for the rate strip (by arrival/ingested time).
      const now = Date.now();
      for (const r of newRows) {
        const ms = Date.parse(r.ingested_at);
        logRef.current.push({ plugin: pluginOfRef.current(r.channel), ms: Number.isNaN(ms) ? now : ms });
      }
      // Newest first, bounded.
      setRows((prev) => {
        const merged = [...[...newRows].reverse(), ...prev];
        return merged.slice(0, MAX_ROWS);
      });
    }
    if (page.reads && page.reads.length) {
      setReads((prev) => [...[...page.reads].reverse(), ...prev].slice(0, 200));
    }
    if (page.horizon) {
      const h = Date.parse(page.horizon);
      if (!Number.isNaN(h)) setSettleHorizon(h);
    }
    if (typeof page.settle_s === "number") setSettleS(page.settle_s);
  }, []);

  // Seed the list once with the most recent real activity, so Live reads as a
  // page of true archetype-native arrivals from the first paint rather than
  // waiting for the next thing to land. These are historical records (not live
  // arrivals), so they do not feed the rate strip; the long poll owns that.
  useEffect(() => {
    if (seededRef.current || replaying) return;
    let cancelled = false;
    void (async () => {
      try {
        // The latest record in every channel (one per channel) gives a rich,
        // archetype-diverse snapshot instead of a run from one busy channel.
        const res = await sql<ReplayRow>(
          "SELECT DISTINCT ON (channel) id, channel, thread_id, happened_at, ingested_at, " +
            "author_handle AS author, person_id, version, left(content, 240) AS excerpt " +
            "FROM api.messages WHERE happened_at <= now() ORDER BY channel, happened_at DESC",
          { rowCap: 120, priority: "visible" },
        );
        // Do not drop a late seed onto a paused feed (it would look like a new
        // arrival landed while paused); the long poll will fill it on resume.
        if (cancelled || seededRef.current || pausedRef.current) return;
        const seeded = res.rows
          .map(toArrival)
          .sort((a, b) => Date.parse(b.happened_at) - Date.parse(a.happened_at));
        if (seeded.length) {
          seededRef.current = true;
          setRows((prev) => (prev.length ? prev : seeded));
        }
      } catch {
        /* no seed — the long poll still populates the list as things land */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [replaying]);

  // The long-poll loop.
  useEffect(() => {
    if (!active) {
      setConnected(false);
      return;
    }
    const controller = new AbortController();
    let cursor: string | null = null;
    let backoff = 1000;
    let stopped = false;

    const loop = async (): Promise<void> => {
      while (!stopped && !controller.signal.aborted) {
        try {
          const firstPage = cursor === null;
          const page = (await fetchArrivals({
            after: cursor,
            wait: cursor ? 25 : 0,
            signal: controller.signal,
          })) as ArrivalsPageExt;
          if (controller.signal.aborted) return;
          if (page.unsupported) {
            setError(new ApiError({ status: 404, message: "Arrivals route not available on this server" }));
            return;
          }
          setConnected(true);
          setError(null);
          // The very first page is a catch-up snapshot (newest records since the
          // consumer horizon); the seed query above already shows recent activity,
          // so take only its cursor and settle metadata and let genuinely new
          // arrivals stream in on top. Later pages ingest their rows normally.
          if (firstPage) ingestMeta(page);
          else ingest(page);
          cursor = page.cursor || cursor;
          backoff = 1000;
        } catch (err) {
          if (controller.signal.aborted || (err instanceof DOMException && err.name === "AbortError")) return;
          if (err instanceof ApiError) {
            if (err.status === 429) {
              setConnected(false);
              await sleep(2000, controller.signal); // server sets Retry-After: 2
              continue;
            }
            if (err.status === 400) {
              cursor = null; // stale/invalid cursor → reset and re-seed
              await sleep(500, controller.signal);
              continue;
            }
            if (err.status === 403) {
              // Restricted asker / plugin token: arrivals are not for this token.
              setError(err);
              setConnected(false);
              return; // stop — never hammer a forbidden route
            }
            setError(err);
          }
          setConnected(false);
          await sleep(backoff, controller.signal);
          backoff = Math.min(backoff * 2, 30_000);
        }
      }
    };
    void loop();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [active, ingest, ingestMeta]);

  const clear = useCallback(() => {
    setRows([]);
    setReads([]);
  }, []);

  // Replay the last 15 minutes of real ingestion at 10×.
  const replay = useCallback(() => {
    setReplaying(true);
    setReplayProgress(0);
    let cancelled = false;
    void (async () => {
      try {
        const res = await sql<ReplayRow>(
          "SELECT id, channel, thread_id, happened_at, ingested_at, author_handle AS author, " +
            "person_id, version, left(content, 240) AS excerpt FROM api.messages " +
            "WHERE ingested_at >= (SELECT max(ingested_at) FROM api.messages) - interval '15 minutes' " +
            "ORDER BY ingested_at",
          { rowCap: 4000, priority: "visible" },
        );
        const src = res.rows;
        if (!src.length) {
          setReplaying(false);
          return;
        }
        const t0 = Date.parse(src[0]!.ingested_at);
        const span = Date.parse(src[src.length - 1]!.ingested_at) - t0 || 1;
        const start = performance.now();
        const DURATION = Math.min(90_000, span / 10); // 10× speed, capped at 90 s
        let idx = 0;
        setRows([]);
        logRef.current = [];
        const step = (): void => {
          if (cancelled) return;
          const elapsed = performance.now() - start;
          const frac = Math.min(1, elapsed / DURATION);
          setReplayProgress(frac);
          const until = t0 + frac * span;
          const batch: ArrivalRow[] = [];
          while (idx < src.length && Date.parse(src[idx]!.ingested_at) <= until) {
            batch.push(toArrival(src[idx]!));
            idx++;
          }
          if (batch.length) {
            const now = Date.now();
            for (const r of batch) logRef.current.push({ plugin: pluginOfRef.current(r.channel), ms: now });
            setRows((prev) => [...batch.reverse(), ...prev].slice(0, MAX_ROWS));
          }
          if (idx < src.length && frac < 1) requestAnimationFrame(step);
          else setReplaying(false);
        };
        requestAnimationFrame(step);
      } catch {
        setReplaying(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Derive the per-plugin rate from the rolling log (recomputed each tick).
  const ratePlugins = useMemo<PluginRate[]>(() => {
    void tick;
    const now = Date.now();
    const byPlugin = new Map<string, PluginRate>();
    for (const e of logRef.current) {
      let pr = byPlugin.get(e.plugin);
      if (!pr) {
        pr = { plugin: e.plugin, buckets: new Array(BUCKETS).fill(0), perMin: 0, last: 0 };
        byPlugin.set(e.plugin, pr);
      }
      const bi = BUCKETS - 1 - Math.floor((now - e.ms) / BUCKET_MS);
      if (bi >= 0 && bi < BUCKETS) pr.buckets[bi] = (pr.buckets[bi] ?? 0) + 1;
      if (now - e.ms <= 60_000) pr.perMin += 1;
      if (e.ms > pr.last) pr.last = e.ms;
    }
    return [...byPlugin.values()].sort((a, b) => b.perMin - a.perMin || b.last - a.last);
  }, [tick]);

  const totalPerMin = useMemo(() => ratePlugins.reduce((s, p) => s + p.perMin, 0), [ratePlugins]);

  return {
    rows,
    reads,
    ratePlugins,
    totalPerMin,
    settleHorizon,
    settleS,
    connected,
    paused,
    hidden,
    error,
    setPaused,
    clear,
    replay,
    replaying,
    replayProgress,
  };
}

interface ReplayRow {
  id: string | number;
  channel: string;
  thread_id: string | number | null;
  happened_at: string;
  ingested_at: string;
  author: string | null;
  person_id: number | null;
  version: number;
  excerpt: string;
}

function toArrival(r: ReplayRow): ArrivalRow {
  return {
    id: r.id,
    channel: r.channel,
    thread_id: r.thread_id,
    happened_at: r.happened_at,
    ingested_at: r.ingested_at,
    author: r.author,
    person_id: r.person_id,
    excerpt: r.excerpt,
    version: Number(r.version) || 1,
    settled: true,
  };
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });
}

export { BUCKETS };
