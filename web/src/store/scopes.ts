/*
 * Global scopes — the parameters every panel reads (:from, :to, :tz, :asker).
 * A brush on any time axis sets the range; the header shows and clears it. The
 * URL is the source of truth for linkability (synced by useScopesUrlSync), so
 * every view is a link. `asker` mirrors into the auth store so the client sends
 * X-Throughline-Asker for the owner's "act as" preview.
 */
import { create } from "zustand";
import { useAuth } from "./auth";
import { usePrefs } from "./prefs";

export interface TimeScope {
  /** ISO instant, or null for "all time" */
  from: string | null;
  to: string | null;
}

interface ScopesState {
  from: string | null;
  to: string | null;
  asker: string | null;
  setRange: (from: string | null, to: string | null) => void;
  clearRange: () => void;
  setAsker: (asker: string | null) => void;
}

export const useScopes = create<ScopesState>((set) => ({
  from: null,
  to: null,
  asker: useAuth.getState().actingAsker,
  setRange: (from, to) => set({ from, to }),
  clearRange: () => set({ from: null, to: null }),
  setAsker: (asker) => {
    useAuth.getState().setActingAsker(asker);
    set({ asker });
  },
}));

/** The effective timezone (from prefs). Panels bind this as :tz. */
export function scopeTz(): string {
  return usePrefs.getState().tz;
}

/* ---- Formatting helpers (all honour the active timezone) ----------------- */

export function formatInTz(
  value: string | number | Date | null | undefined,
  tz: string,
  opts: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeStyle: "short" },
): string {
  if (value === null || value === undefined) return "—";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: tz }).format(d);
  } catch {
    return d.toISOString();
  }
}

export function formatDateInTz(value: string | number | Date | null | undefined, tz: string): string {
  return formatInTz(value, tz, { dateStyle: "medium" });
}

/** Compact relative time, e.g. "3m", "2h", "5d", "now". */
export function relative(value: string | number | Date | null | undefined, now = Date.now()): string {
  if (value === null || value === undefined) return "—";
  const d = value instanceof Date ? value : new Date(value);
  const t = d.getTime();
  if (Number.isNaN(t)) return "—";
  const diff = Math.round((now - t) / 1000); // seconds
  const ahead = diff < 0;
  const s = Math.abs(diff);
  let out: string;
  if (s < 45) out = "now";
  else if (s < 3600) out = `${Math.round(s / 60)}m`;
  else if (s < 86400) out = `${Math.round(s / 3600)}h`;
  else if (s < 2_592_000) out = `${Math.round(s / 86400)}d`;
  else if (s < 31_536_000) out = `${Math.round(s / 2_592_000)}mo`;
  else out = `${Math.round(s / 31_536_000)}y`;
  if (out === "now") return out;
  return ahead ? `in ${out}` : `${out} ago`;
}
