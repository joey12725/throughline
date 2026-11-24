/*
 * The time grammar of design.md, as pure geometry. Every time axis draws:
 *   - a solid brass rule at "now";
 *   - the future (right of now) dimmed and dashed;
 *   - periods a source does not claim to cover, hatched;
 *   - imprecise times drawn soft-edged (handled per-mark by renderers).
 *
 * This module turns a domain, optional coverage and "now" into ordered segments
 * the renderers paint, plus the position of the now-rule.
 */

export type SegmentKind = "covered" | "uncovered" | "future";

export interface TimeSegment {
  kind: SegmentKind;
  from: number;
  to: number;
}

export interface CoverageInput {
  from: number | null;
  to: number | null;
}

/**
 * Split [domainFrom, domainTo] at coverage edges and now into ordered segments.
 * With no coverage, the whole past counts as covered (we hatch only what a source
 * reported it does not cover). Returns [] for a degenerate domain.
 */
export function timeSegments(
  domainFrom: number,
  domainTo: number,
  coverage: CoverageInput | null | undefined,
  now: number,
): TimeSegment[] {
  if (!(domainTo > domainFrom)) return [];
  // Boundaries inside the domain.
  const cuts = new Set<number>([domainFrom, domainTo]);
  const add = (x: number | null | undefined) => {
    if (x != null && x > domainFrom && x < domainTo) cuts.add(x);
  };
  add(now);
  if (coverage) {
    add(coverage.from);
    add(coverage.to);
  }
  const edges = [...cuts].sort((a, b) => a - b);
  const segs: TimeSegment[] = [];
  for (let i = 0; i < edges.length - 1; i++) {
    const from = edges[i]!;
    const to = edges[i + 1]!;
    const mid = (from + to) / 2;
    let kind: SegmentKind;
    if (mid > now) kind = "future";
    else if (!coverage || coverage.from == null) kind = "covered";
    else {
      const covFrom = coverage.from;
      const covTo = coverage.to ?? now;
      kind = mid >= covFrom && mid <= covTo ? "covered" : "uncovered";
    }
    // Merge adjacent same-kind segments.
    const last = segs[segs.length - 1];
    if (last && last.kind === kind) last.to = to;
    else segs.push({ kind, from, to });
  }
  return segs;
}

/** Where the now-rule sits in the domain, or null when now is off the axis. */
export function nowPosition(domainFrom: number, domainTo: number, now: number): number | null {
  if (now < domainFrom || now > domainTo) return null;
  return now;
}

/**
 * A grain coarser than a day means its timestamps are imprecise (a whole day,
 * month…), so marks are drawn soft-edged. Minute/hour are precise moments.
 */
export function isImpreciseGrain(grain: string | null | undefined): boolean {
  return grain !== null && grain !== undefined && grain !== "minute" && grain !== "hour";
}
