// Ranking: score = affinity x fitness x information x (1 - cost), then maximal marginal
// relevance so the top of the list is not six time series.

import type { Archetypes, LensCandidate, LensVariant } from "../types";
import type { LensDef } from "./common";

export interface Scored {
  lens: LensDef;
  variant: LensVariant;
  score: number;
}

const DEFAULT_AFFINITY = 0.3;

/** How well a lens suits the channel's archetype mix: the best archetype counts, weighted by its score. */
export function affinity(lens: LensDef, archetypes: Archetypes | null): number {
  if (!archetypes) return 1;
  if (lens.id === "spine") return 0.5;
  const top = Math.max(0.05, archetypes.scores[0]?.score ?? 0.05);
  let best = 0;
  for (const s of archetypes.scores) {
    // The primary archetype counts in full; others in proportion to how close they come.
    const weight = s.archetype === archetypes.primary ? 1 : Math.min(1, s.score / top) * 0.9;
    const a = lens.affinity[s.archetype] ?? DEFAULT_AFFINITY;
    if (weight * a > best) best = weight * a;
  }
  return best;
}

export function scoreVariant(lens: LensDef, fitness: number, variant: LensVariant, archetypes: Archetypes | null): number {
  const fit = variant.fitness ?? fitness;
  const s = affinity(lens, archetypes) * fit * variant.information * (1 - variant.cost);
  return Number.isFinite(s) ? Math.max(0, Math.min(1, s)) : 0;
}

function similarity(a: Scored, b: Scored): number {
  let s = 0;
  if (a.lens.id === b.lens.id) s += 0.7;
  else if (a.lens.group === b.lens.group) s += 0.3;
  const fa = a.variant.spec.provenance.fields ?? [];
  const fb = new Set(b.variant.spec.provenance.fields ?? []);
  if (fa.length > 0 && fb.size > 0) {
    const shared = fa.filter((f) => fb.has(f)).length;
    s += 0.4 * (shared / Math.max(fa.length, fb.size));
  }
  return Math.min(1, s);
}

/** Order by maximal marginal relevance. Scores are untouched; only the order changes. */
export function diversify(items: Scored[], lambda = 0.7): Scored[] {
  const pool = [...items].sort((a, b) => b.score - a.score);
  const top = pool[0]?.score ?? 0;
  if (top <= 0) return pool;
  const out: Scored[] = [];
  while (pool.length > 0) {
    let bestIdx = 0;
    let bestVal = -Infinity;
    for (let i = 0; i < pool.length; i++) {
      const c = pool[i] as Scored;
      let maxSim = 0;
      let sameLens = 0;
      for (const chosen of out) {
        const sim = similarity(c, chosen);
        if (sim > maxSim) maxSim = sim;
        if (chosen.lens.id === c.lens.id) sameLens++;
      }
      // Each further picture of the same kind costs a little more than the last.
      const val = lambda * (c.score / top) - (1 - lambda) * maxSim - 0.07 * sameLens;
      if (val > bestVal) {
        bestVal = val;
        bestIdx = i;
      }
    }
    out.push(pool.splice(bestIdx, 1)[0] as Scored);
  }
  return out;
}

export function toCandidates(items: Scored[]): LensCandidate[] {
  return items.map((s) => ({ spec: s.variant.spec, score: Math.round(s.score * 1000) / 1000, why: s.variant.spec.why }));
}
