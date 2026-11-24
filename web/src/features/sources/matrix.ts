/*
 * Freshness classification: for one channel and one window of days, decide what
 * each empty day *means*. A day with records is "active". An empty day inside the
 * channel's own active span is a "hole" when the channel's rhythm predicts
 * records there, or a "quiet" day when it does not. Days a source explicitly
 * reported as a gap are "reported" and outrank inference. Days outside the span
 * (before the first or after the last record) are "outside" — nothing is owed.
 *
 * Pure and tested (matrix.test.ts); the canvas grid is only a renderer of this.
 */
export type DayKind = "active" | "hole" | "quiet" | "reported" | "outside";

export interface DayClass {
  day: string;
  kind: DayKind;
  count: number;
}

export interface ChannelFreshness {
  days: DayClass[];
  /** The channel's own active-day density within its span (0..1). */
  density: number;
  /** The weekdays (0..6) on which the channel is ever active. */
  activeWeekdays: Set<number>;
  maxCount: number;
}

export interface ReportedGap {
  from: string; // YYYY-MM-DD inclusive
  to: string; // YYYY-MM-DD inclusive
}

/**
 * Classify a channel's days.
 * @param dayKeys    the window, oldest first (YYYY-MM-DD)
 * @param counts     count per day key (missing => 0)
 * @param weekdayOf  day key -> local weekday 0..6
 * @param reported   source-reported gap ranges (solid), optional
 */
export function classifyChannel(
  dayKeys: readonly string[],
  counts: Map<string, number>,
  weekdayOf: (day: string) => number,
  reported: readonly ReportedGap[] = [],
): ChannelFreshness {
  let first = -1;
  let last = -1;
  const activeWeekdays = new Set<number>();
  let activeCount = 0;
  let maxCount = 0;
  dayKeys.forEach((day, i) => {
    const c = counts.get(day) ?? 0;
    if (c > 0) {
      if (first < 0) first = i;
      last = i;
      activeWeekdays.add(weekdayOf(day));
      activeCount += 1;
      if (c > maxCount) maxCount = c;
    }
  });
  const spanLen = first < 0 ? 0 : last - first + 1;
  const density = spanLen > 0 ? activeCount / spanLen : 0;

  const inReported = (day: string): boolean =>
    reported.some((g) => day >= g.from && day <= g.to);

  const days: DayClass[] = dayKeys.map((day, i) => {
    const count = counts.get(day) ?? 0;
    if (count > 0) return { day, kind: "active", count };
    if (inReported(day)) return { day, kind: "reported", count: 0 };
    const insideSpan = first >= 0 && i >= first && i <= last;
    if (!insideSpan) return { day, kind: "outside", count: 0 };
    // Empty day within the channel's active span. Is a record *expected* here?
    const wd = weekdayOf(day);
    const rhythmExpects = density >= 0.5 || activeWeekdays.has(wd);
    return { day, kind: rhythmExpects ? "hole" : "quiet", count: 0 };
  });

  return { days, density, activeWeekdays, maxCount };
}

/** Log-scaled intensity 0..1 for a count against a channel's own peak. */
export function intensity(count: number, maxCount: number): number {
  if (count <= 0 || maxCount <= 0) return 0;
  return Math.log1p(count) / Math.log1p(maxCount);
}
