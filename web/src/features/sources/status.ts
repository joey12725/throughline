/*
 * Source health, derived deterministically from a shape-only plugin row.
 *
 * Nothing here names a plugin. A source's state comes only from its cadence,
 * its last success, its last error, its failure streak, whether it is paused,
 * and its run mode. The derivation is pure and tested (status.test.ts); the
 * board and the plugin detail page both render from it.
 */
import type { PluginRow } from "@/api/types";
import type { SourceState } from "@/ui/status";

export type { SourceState };

/** Most-severe first. The board sorts plugins by this, then by name. */
export const SEVERITY: Record<SourceState, number> = {
  failing: 0,
  never_ran: 1,
  stale: 2,
  late: 3,
  paused: 4,
  one_shot: 5,
  ok: 6,
};

/** A run mode that produces data in one pass rather than on an interval. */
function isOneShotMode(mode: string | null | undefined): boolean {
  if (!mode) return false;
  const m = mode.toLowerCase();
  return m === "manual" || m === "oneshot" || m === "one_shot" || m === "once" || m === "import";
}

export interface SourceHealth {
  state: SourceState;
  severity: number;
  /** Seconds since the last successful run, or null when it never succeeded. */
  ageS: number | null;
  /** The declared cadence in seconds, or null for an unscheduled source. */
  intervalS: number | null;
  /** How many consecutive failures the host reports (0 when unknown/healthy). */
  failures: number;
  /** A short machine reason for the state, for tooltips and tests. */
  reason: string;
}

/**
 * Derive a source's state from its row. Deterministic in `now` so it is testable
 * and so two renders in the same frame agree.
 *
 * Order of evidence (strongest first): paused, failing (error or a failure
 * streak), never-succeeded (one-shot vs never-ran by mode/cadence), host-flagged
 * stale, late against its own cadence, otherwise ok.
 */
export function deriveSourceState(p: PluginRow, now: number = Date.now()): SourceHealth {
  const intervalS = p.interval_s ?? null;
  const failures = p.consecutive_failures ?? (p.last_error ? 1 : 0);
  const lastSuccess = p.last_success_at ? Date.parse(p.last_success_at) : NaN;
  const ageS = Number.isNaN(lastSuccess) ? null : Math.max(0, Math.round((now - lastSuccess) / 1000));

  const finish = (state: SourceState, reason: string): SourceHealth => ({
    state,
    severity: SEVERITY[state],
    ageS,
    intervalS,
    failures,
    reason,
  });

  if (p.paused_at) return finish("paused", "paused_at set");
  if (p.last_error || (p.consecutive_failures ?? 0) > 0) {
    return finish("failing", failures > 1 ? `${failures} consecutive failures` : "last run errored");
  }
  if (ageS === null) {
    // Never succeeded. An unscheduled / one-shot source reads as one-shot rather
    // than an error; a scheduled source that has not landed reads as never-ran.
    if (isOneShotMode(p.mode) || intervalS === null || intervalS === 0) {
      return finish("one_shot", "no cadence, never ran");
    }
    return finish("never_ran", "scheduled but no success yet");
  }
  if (p.stale) return finish("stale", "host flagged stale");
  if (intervalS && ageS > intervalS * 1.5) return finish("late", "overdue against cadence");
  return finish("ok", "fresh");
}

/** Count sources by state for the summary sentence. */
export function tallyStates(plugins: readonly PluginRow[], now: number = Date.now()): Record<SourceState, number> {
  const out: Record<SourceState, number> = {
    ok: 0,
    late: 0,
    stale: 0,
    failing: 0,
    paused: 0,
    one_shot: 0,
    never_ran: 0,
  };
  for (const p of plugins) out[deriveSourceState(p, now).state] += 1;
  return out;
}

/**
 * The health-board summary sentence: "12 sources healthy, 1 late, 1 failing".
 * "healthy" folds ok + one-shot (both are fine); every other non-zero state is
 * named with its own word so nothing hides.
 */
export function summarySentence(plugins: readonly PluginRow[], now: number = Date.now()): string {
  if (plugins.length === 0) return "No sources";
  const t = tallyStates(plugins, now);
  const healthy = t.ok + t.one_shot;
  const parts: string[] = [];
  if (healthy > 0) parts.push(`${healthy} source${healthy === 1 ? "" : "s"} healthy`);
  const order: SourceState[] = ["failing", "never_ran", "stale", "late", "paused"];
  const WORD: Partial<Record<SourceState, string>> = {
    failing: "failing",
    never_ran: "never ran",
    stale: "stale",
    late: "late",
    paused: "paused",
  };
  for (const s of order) if (t[s] > 0) parts.push(`${t[s]} ${WORD[s]}`);
  if (parts.length === 0) return `${plugins.length} sources`;
  return parts.join(", ");
}

const UNITS: Array<[number, string]> = [
  [86400, "day"],
  [3600, "h"],
  [60, "min"],
  [1, "s"],
];

/** A cadence as prose: 900 -> "every 15 min", 86400 -> "every day". */
export function formatCadence(intervalS: number | null | undefined): string {
  if (intervalS === null || intervalS === undefined) return "on demand";
  if (intervalS === 0) return "one-shot";
  for (const [secs, unit] of UNITS) {
    if (intervalS % secs === 0 && intervalS >= secs) {
      const n = intervalS / secs;
      if (unit === "day" && n === 1) return "every day";
      if (unit === "h" && n === 1) return "hourly";
      if (unit === "min" && n === 1) return "every minute";
      return `every ${n} ${unit}${unit === "day" && n !== 1 ? "s" : ""}`;
    }
  }
  // Non-round cadence: nearest coarse unit.
  for (const [secs, unit] of UNITS) {
    if (intervalS >= secs) return `every ~${Math.round(intervalS / secs)} ${unit}`;
  }
  return `every ${intervalS}s`;
}

export interface CadenceBand {
  lo: number;
  hi: number;
  typical: number;
}

/**
 * The source's own typical arrival rate, as a p10–p90 band over its history of
 * per-bucket counts. Used to shade a sparkline so a quiet week reads against the
 * source's own rhythm, not an absolute scale. Pure and tested.
 */
export function cadenceBand(counts: readonly number[]): CadenceBand | null {
  const nonzero = counts.filter((n) => n > 0).sort((a, b) => a - b);
  if (nonzero.length === 0) return null;
  const q = (p: number): number => {
    const i = (nonzero.length - 1) * p;
    const lo = Math.floor(i);
    const hi = Math.ceil(i);
    const vlo = nonzero[lo] ?? 0;
    const vhi = nonzero[hi] ?? vlo;
    if (lo === hi) return vlo;
    return vlo + (vhi - vlo) * (i - lo);
  };
  return { lo: q(0.1), hi: q(0.9), typical: q(0.5) };
}
