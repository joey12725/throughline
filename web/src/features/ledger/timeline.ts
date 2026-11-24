/*
 * Pure helpers for the Ledger: bucketing commitments by their due date, binning
 * audit activity by hour, and folding the spend ledger into stacked series. No
 * DOM; unit-tested.
 */

export type DueBucket = "overdue" | "today" | "week" | "later" | "undated" | "closed";

export const DUE_BUCKETS: { id: DueBucket; label: string; tone: "bad" | "warn" | "ok" | "neutral" }[] = [
  { id: "overdue", label: "Overdue", tone: "bad" },
  { id: "today", label: "Due today", tone: "warn" },
  { id: "week", label: "This week", tone: "warn" },
  { id: "later", label: "Later", tone: "ok" },
  { id: "undated", label: "No date", tone: "neutral" },
  { id: "closed", label: "Closed", tone: "neutral" },
];

const DAY = 86_400_000;

/** Which board column a commitment belongs to. */
export function dueBucket(dueMs: number | null, state: string, nowMs: number): DueBucket {
  if (state && state !== "open") return "closed";
  if (dueMs == null || !Number.isFinite(dueMs)) return "undated";
  const delta = dueMs - nowMs;
  if (delta < 0) return "overdue";
  if (delta < DAY) return "today";
  if (delta < 7 * DAY) return "week";
  return "later";
}

/** Signed whole-day distance from now to a due date (negative = overdue). */
export function daysUntil(dueMs: number, nowMs: number): number {
  return Math.round((dueMs - nowMs) / DAY);
}

/** 24 hourly counts for a set of instants, in a zone (audit activity heat). */
export function hourOfDayBins(times: readonly number[], tz: string): number[] {
  const bins = new Array(24).fill(0);
  for (const t of times) {
    if (!Number.isFinite(t)) continue;
    let h: number;
    try {
      const s = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "2-digit", hour12: false }).format(new Date(t));
      h = Number(s) % 24;
    } catch {
      h = new Date(t).getUTCHours();
    }
    if (h >= 0 && h < 24) bins[h] += 1;
  }
  return bins;
}

export interface SpendEntry {
  purpose?: string;
  provider?: string;
  model?: string;
  origin?: string;
  cost?: number;
  tokens?: number;
  calls?: number;
}
export interface SpendSlice {
  key: string;
  cost: number;
  tokens: number;
  calls: number;
}

/** Fold the spend ledger `by` list into totals per value of one dimension. */
export function aggregateSpend(by: readonly SpendEntry[], dim: "provider" | "purpose" | "model" | "origin"): SpendSlice[] {
  const map = new Map<string, SpendSlice>();
  for (const e of by) {
    const key = (e[dim] ?? "unknown") || "unknown";
    const cur = map.get(key) ?? { key, cost: 0, tokens: 0, calls: 0 };
    cur.cost += e.cost ?? 0;
    cur.tokens += e.tokens ?? 0;
    cur.calls += e.calls ?? 0;
    map.set(key, cur);
  }
  return [...map.values()].sort((a, b) => b.tokens - a.tokens || b.cost - a.cost);
}
