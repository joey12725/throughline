/*
 * The seven source states, each with a word, a tone and a glyph. Status never
 * borrows a source hue and always carries a glyph as well as a colour, so it is
 * legible under the privacy veil and to colour-blind readers.
 *
 * This maps a shape-derived PluginRow to a state; it hard-codes no plugin name.
 */
import type { LucideIcon } from "lucide-react";
import { Check, Clock, CircleAlert, Pause, CircleSlash, CircleDot, Power } from "lucide-react";
import type { PluginRow } from "@/api/types";

export type SourceState =
  | "ok"
  | "late"
  | "stale"
  | "failing"
  | "paused"
  | "one_shot"
  | "never_ran";

export type Tone = "ok" | "warn" | "bad" | "neutral";

export interface StateDescriptor {
  word: string;
  tone: Tone;
  glyph: LucideIcon;
}

export const STATE: Record<SourceState, StateDescriptor> = {
  ok: { word: "ok", tone: "ok", glyph: Check },
  late: { word: "late", tone: "warn", glyph: Clock },
  stale: { word: "stale", tone: "warn", glyph: Clock },
  failing: { word: "failing", tone: "bad", glyph: CircleAlert },
  paused: { word: "paused", tone: "neutral", glyph: Pause },
  one_shot: { word: "one-shot", tone: "neutral", glyph: CircleDot },
  never_ran: { word: "never ran", tone: "neutral", glyph: Power },
};

/** Derive a source state from a shape-only plugin row. */
export function pluginState(p: PluginRow, now: number = Date.now()): SourceState {
  if (p.paused_at) return "paused";
  if (p.last_error) return "failing";
  if (!p.last_success_at) {
    // Never succeeded. A one-shot (import-style) source that is not polling
    // reads as "one-shot" rather than an error; otherwise it has never run.
    if (p.interval_s === null || p.interval_s === 0) return "one_shot";
    return "never_ran";
  }
  if (p.stale) return "stale";
  // Two intervals late but not yet flagged stale -> late.
  if (p.interval_s && p.last_success_at) {
    const age = (now - Date.parse(p.last_success_at)) / 1000;
    if (age > p.interval_s * 1.5) return "late";
  }
  return "ok";
}

export const UNKNOWN_ICON: LucideIcon = CircleSlash;
