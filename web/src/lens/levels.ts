// Levels that climb and reset: the evidence behind the gauge archetype and the sawtooth lens.

import { num } from "./profile";
import { isMeasure, tokens } from "./roles";
import type { ChannelProfile, Role, RoleMap } from "./types";

function knownDistinct(profile: ChannelProfile, role: Role): number | null {
  const f = profile.fields.find((x) => x.name === role.field);
  if (f && f.observed >= 5 && f.distinct !== null) return f.distinct;
  return role.domain ? role.domain.length : null;
}

const CYCLE_WORDS = new Set(["window", "cycle", "period", "series", "session", "meter", "sensor", "device", "bucket", "quota"]);
const LEVEL_WORDS = new Set(["used", "level", "balance", "remaining", "left", "usage", "consumed", "quota", "fill", "battery", "charge"]);

export interface Level {
  role: Role;
  group?: Role;
  /** 0..1: how saw-toothed the sampled values are (or how likely, from names alone). */
  strength: number;
  tested: boolean;
  drops: number;
}

/**
 * A level that climbs and then falls back: tested on the spread sample per group key
 * (steps that rise, with a few sharp drops), or predicted from the name when there is none.
 */
export function findLevels(profile: ChannelProfile, roles: RoleMap): Level[] {
  const live = Object.values(roles).filter((r) => !r.hidden);
  const group = live
    .filter((r) => r.role === "group_key")
    .map((g) => ({ g, d: knownDistinct(profile, g) }))
    .filter((x) => x.d === null || (x.d >= 2 && x.d <= 24))
    // The finest grouping that is still a handful of lines; unseen, a name that says "cycle".
    .sort(
      (a, b) =>
        (a.d === null ? 1 : 0) - (b.d === null ? 1 : 0) ||
        (b.d ?? 0) - (a.d ?? 0) ||
        Number(tokens(b.g.field).some((t) => CYCLE_WORDS.has(t))) - Number(tokens(a.g.field).some((t) => CYCLE_WORDS.has(t))) ||
        b.g.confidence - a.g.confidence,
    )[0]?.g;
  const out: Level[] = [];
  for (const r of live) {
    if (!isMeasure(r.role) || r.role === "count" || r.role === "probability" || r.role === "score") continue;
    const named = tokens(r.field).some((t) => LEVEL_WORDS.has(t));
    const rows = profile.sample
      .map((row) => ({ t: String(row.happened_at ?? ""), g: group ? String(row[group.field] ?? "") : "", v: num(row[r.field]) }))
      .filter((x): x is { t: string; g: string; v: number } => x.v !== null)
      .sort((a, b) => (a.g < b.g ? -1 : a.g > b.g ? 1 : a.t < b.t ? -1 : a.t > b.t ? 1 : 0));
    if (rows.length >= 12) {
      let min = Infinity;
      let max = -Infinity;
      for (const x of rows) {
        if (x.v < min) min = x.v;
        if (x.v > max) max = x.v;
      }
      const range = max - min;
      if (!(range > 0)) continue;
      let rises = 0;
      let dips = 0;
      let drops = 0;
      for (let i = 1; i < rows.length; i++) {
        const a = rows[i - 1] as { g: string; v: number };
        const b = rows[i] as { g: string; v: number };
        if (a.g !== b.g) continue;
        const d = b.v - a.v;
        if (d > 0) rises++;
        else if (d <= -0.3 * range) drops++;
        else if (d < 0) dips++;
      }
      const steady = rises / Math.max(1, rises + dips);
      // A sawtooth climbs several steps for every reset; noise drops as often as it rises.
      if (drops >= 1 && rises >= 4 && steady >= 0.75 && rises >= 3 * drops) {
        out.push({ role: r, group, strength: Math.min(1, 0.6 + 0.4 * steady) * (named ? 1 : r.role === "percent" ? 0.85 : 0.7), tested: true, drops });
      }
    } else if (named && (r.role === "percent" || r.role === "measure" || r.role === "currency")) {
      const deadline = live.some((x) => x.role === "time.deadline");
      const rising = tokens(r.field).some((t) => t === "used" || t === "usage" || t === "consumed");
      const before = tokens(r.field).includes("before") ? 0.5 : 1;
      out.push({ role: r, group, strength: (0.35 + (deadline ? 0.2 : 0) + (group ? 0.1 : 0) + (rising ? 0.1 : 0)) * before * (r.role === "percent" ? 1 : 0.6), tested: false, drops: 0 });
    }
  }
  return out.sort((a, b) => b.strength - a.strength);
}
