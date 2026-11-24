/*
 * Pure summaries for a place's visit history — weekday×hour occupancy and a
 * humane duration. No DOM; unit-tested.
 */

/** ISO weekday (1=Mon..7=Sun) and hour (0..23) of an instant, in a zone. */
export function weekdayHour(tMs: number, tz: string): { weekday: number; hour: number } {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      weekday: "short",
      hour: "2-digit",
      hour12: false,
    }).formatToParts(new Date(tMs));
    const wd = parts.find((p) => p.type === "weekday")?.value ?? "Mon";
    let hour = Number(parts.find((p) => p.type === "hour")?.value ?? "0");
    if (hour === 24) hour = 0;
    const map: Record<string, number> = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 };
    return { weekday: map[wd] ?? 1, hour };
  } catch {
    const d = new Date(tMs);
    const wd = d.getUTCDay(); // 0=Sun
    return { weekday: wd === 0 ? 7 : wd, hour: d.getUTCHours() };
  }
}

/** A 7×24 grid of visit counts, indexed [weekday-1][hour]. */
export function weekdayHourGrid(times: readonly number[], tz: string): number[][] {
  const grid: number[][] = Array.from({ length: 7 }, () => new Array(24).fill(0));
  for (const t of times) {
    if (!Number.isFinite(t)) continue;
    const { weekday, hour } = weekdayHour(t, tz);
    const row = grid[weekday - 1];
    if (row) row[hour] = (row[hour] ?? 0) + 1;
  }
  return grid;
}

/** The largest cell value in a grid (for scaling), at least 1. */
export function gridMax(grid: number[][]): number {
  let m = 1;
  for (const row of grid) for (const v of row) if (v > m) m = v;
  return m;
}

/** A compact human duration from seconds: "3d 4h", "2h 10m", "45m", "30s". */
export function humanDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return h > 0 ? `${d}d ${h}h` : `${d}d`;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return `${m}m`;
  return `${Math.round(seconds)}s`;
}
