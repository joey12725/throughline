/*
 * The Loom's single aggregate query: week x channel record counts across the
 * whole installation, no time scope (the Loom always shows the whole life, and
 * brushing it *sets* the scope rather than reading it). One statement, ~3,400
 * rows, measured ~205 ms on the 186k-record corpus. Everything the canvas draws
 * — lanes, the river, year ticks — is derived from this grid in the browser.
 */
import { useMemo } from "react";
import { num, useSql } from "@/api";
import type { ChannelIndex } from "./useChannelIndex";

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

export interface LoomGrid {
  /** week-start instants (ms, Monday 00:00 UTC), ascending and contiguous */
  weeks: number[];
  domainFrom: number;
  domainTo: number;
  /** channel name -> per-week counts (length === weeks.length) */
  counts: Map<string, Int32Array>;
  /** channel name -> largest weekly count (per-lane normalisation) */
  laneMax: Map<string, number>;
  /** plugin -> per-week summed counts (for the river) */
  pluginCounts: Map<string, Float64Array>;
  /** per-week total across every visible channel (for the density pivot) */
  weekTotals: Float64Array;
  globalMax: number;
  weekIndex: (ms: number) => number;
  loading: boolean;
  error: unknown;
}

const LOOM_SQL =
  "SELECT channel, date_trunc('week', happened_at, 'UTC') AS bucket, count(*)::text AS n " +
  "FROM api.messages GROUP BY 1, 2";

export function useLoomData(index: ChannelIndex | null, now: number): LoomGrid {
  const q = useSql<{ channel: string; bucket: string; n: string }>(LOOM_SQL, {
    rowCap: 10000, // the server's maximum; ~3,400 rows on this corpus
    priority: "visible",
  });

  return useMemo<LoomGrid>(() => {
    const rows = q.data?.rows ?? [];
    const empty: LoomGrid = {
      weeks: [],
      domainFrom: now - 52 * WEEK_MS,
      domainTo: now,
      counts: new Map(),
      laneMax: new Map(),
      pluginCounts: new Map(),
      weekTotals: new Float64Array(0),
      globalMax: 1,
      weekIndex: () => -1,
      loading: q.isLoading,
      error: q.error,
    };
    if (!rows.length || !index) return empty;

    // Domain: first observed week to the later of now or the last observed week.
    let minMs = Infinity;
    let maxMs = -Infinity;
    const parsed: { channel: string; ms: number; n: number }[] = [];
    for (const r of rows) {
      const ms = Date.parse(r.bucket);
      if (Number.isNaN(ms)) continue;
      const n = num(r.n);
      parsed.push({ channel: r.channel, ms, n });
      if (ms < minMs) minMs = ms;
      if (ms > maxMs) maxMs = ms;
    }
    if (!parsed.length) return empty;

    const nowWeek = Math.floor((now - minMs) / WEEK_MS) * WEEK_MS + minMs;
    const domainFrom = minMs;
    const domainTo = Math.max(maxMs, nowWeek) + WEEK_MS;
    const nWeeks = Math.max(1, Math.round((domainTo - domainFrom) / WEEK_MS));
    const weeks: number[] = new Array(nWeeks);
    for (let i = 0; i < nWeeks; i++) weeks[i] = domainFrom + i * WEEK_MS;

    const weekIndex = (ms: number): number => {
      const i = Math.round((ms - domainFrom) / WEEK_MS);
      return i >= 0 && i < nWeeks ? i : -1;
    };

    const counts = new Map<string, Int32Array>();
    const laneMax = new Map<string, number>();
    const pluginCounts = new Map<string, Float64Array>();
    const weekTotals = new Float64Array(nWeeks);
    let globalMax = 1;

    for (const info of index.byName.values()) {
      counts.set(info.name, new Int32Array(nWeeks));
      laneMax.set(info.name, 0);
    }
    for (const g of index.groups) pluginCounts.set(g.plugin, new Float64Array(nWeeks));

    for (const p of parsed) {
      const arr = counts.get(p.channel);
      if (!arr) continue; // restricted token: channel not visible
      const i = weekIndex(p.ms);
      if (i < 0) continue;
      const cur = (arr[i] ?? 0) + p.n;
      arr[i] = cur;
      weekTotals[i] = (weekTotals[i] ?? 0) + p.n;
      if (cur > (laneMax.get(p.channel) ?? 0)) laneMax.set(p.channel, cur);
      if (cur > globalMax) globalMax = cur;
      const info = index.byName.get(p.channel);
      if (info) {
        const pc = pluginCounts.get(info.plugin);
        if (pc) pc[i] = (pc[i] ?? 0) + p.n;
      }
    }

    return {
      weeks,
      domainFrom,
      domainTo,
      counts,
      laneMax,
      pluginCounts,
      weekTotals,
      globalMax,
      weekIndex,
      loading: q.isLoading,
      error: q.error,
    };
  }, [q.data, q.isLoading, q.error, index, now]);
}

export { WEEK_MS };
