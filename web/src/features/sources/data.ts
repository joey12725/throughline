/*
 * The source board reads three aggregates, each one bounded SQL:
 *   - a freshness matrix (channel x local day, last 60 days),
 *   - an arrivals history (plugin x week, last year) for the sparklines + bands,
 *   - per-channel day counts for the matrix of one plugin's detail page.
 * All counts come back as Postgres text; callers fold them with num().
 */
import { useMemo } from "react";
import { num, useSql } from "@/api";
import { usePrefs } from "@/store/prefs";

export interface DayCountRow {
  channel: string;
  d: string; // YYYY-MM-DD, local day
  n: string;
}

export interface PluginWeekRow {
  plugin: string;
  wk: string;
  n: string;
}

/** The number of days the freshness matrix spans. */
export const MATRIX_DAYS = 60;

/** A timezone as a safe SQL string literal (POST /v1/sql does not bind :tz). */
export function tzLiteral(tz: string | null | undefined): string {
  const name = tz && /^[A-Za-z0-9_+\-/]{1,64}$/.test(tz) ? tz : "UTC";
  return `'${name}'`;
}

/**
 * Per-channel counts by local day over the last `MATRIX_DAYS` days. One grouped
 * scan over api.messages; the channel view join is avoided by grouping on the
 * channel column the view already carries.
 */
export function useFreshnessMatrix(enabled = true) {
  const tz = usePrefs((s) => s.tz);
  const tzl = tzLiteral(tz);
  const stmt = enabled
    ? `SELECT channel,
              (date_trunc('day', happened_at, ${tzl}))::date::text AS d,
              count(*)::text AS n
         FROM api.messages
        WHERE happened_at >= date_trunc('day', now(), ${tzl}) - interval '${MATRIX_DAYS - 1} days'
          AND happened_at <  date_trunc('day', now(), ${tzl}) + interval '1 day'
        GROUP BY 1, 2`
    : null;
  return useSql<DayCountRow>(stmt, { rowCap: 10000, priority: "visible", keyParts: [tz, "freshness60"] });
}

/** Per-plugin weekly arrival counts over the last year, for sparklines + bands. */
export function usePluginHistory(enabled = true) {
  const stmt = enabled
    ? `SELECT c.plugin AS plugin,
              (date_trunc('week', m.happened_at))::date::text AS wk,
              count(*)::text AS n
         FROM api.messages m
         JOIN api.channels c ON c.name = m.channel
        WHERE m.happened_at >= now() - interval '364 days'
          AND m.happened_at < now() + interval '7 days'
        GROUP BY 1, 2`
    : null;
  return useSql<PluginWeekRow>(stmt, { rowCap: 10000, priority: "background", keyParts: ["pluginHistory"] });
}

/** Fold a weekly history into an ordered count series per plugin. */
export function historyByPlugin(rows: readonly PluginWeekRow[] | undefined): Map<string, number[]> {
  const byPlugin = new Map<string, Array<{ wk: string; n: number }>>();
  for (const r of rows ?? []) {
    const list = byPlugin.get(r.plugin) ?? [];
    list.push({ wk: r.wk, n: num(r.n) });
    byPlugin.set(r.plugin, list);
  }
  const out = new Map<string, number[]>();
  for (const [plugin, list] of byPlugin) {
    list.sort((a, b) => (a.wk < b.wk ? -1 : 1));
    out.set(
      plugin,
      list.map((x) => x.n),
    );
  }
  return out;
}

export interface MatrixCell {
  n: number;
}

/** Index a flat day-count list as channel -> (YYYY-MM-DD -> count). */
export function useMatrixIndex(rows: readonly DayCountRow[] | undefined): Map<string, Map<string, number>> {
  return useMemo(() => {
    const idx = new Map<string, Map<string, number>>();
    for (const r of rows ?? []) {
      const byDay = idx.get(r.channel) ?? new Map<string, number>();
      byDay.set(r.d, num(r.n));
      idx.set(r.channel, byDay);
    }
    return idx;
  }, [rows]);
}

/** The last `days` local-day keys (YYYY-MM-DD), oldest first, in the given tz. */
export function recentDays(days: number, tz: string, now: number = Date.now()): string[] {
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz || "UTC",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const out: string[] = [];
  for (let i = days - 1; i >= 0; i--) {
    out.push(fmt.format(new Date(now - i * 86400_000)));
  }
  return out;
}

/** The local weekday (0=Sun..6=Sat) for a YYYY-MM-DD day key. */
export function weekdayOf(dayKey: string): number {
  const parts = dayKey.split("-");
  const y = Number(parts[0]) || 1970;
  const m = Number(parts[1]) || 1;
  const d = Number(parts[2]) || 1;
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay();
}
