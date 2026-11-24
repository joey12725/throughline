/*
 * Data access for the Day view. Day boundaries honour the scope timezone: the
 * window is computed in SQL from the local date, so a record at 00:30 lands in
 * the right day. Everything is a bounded SELECT over schema api; no channel or
 * plugin name is hard-coded.
 */
import { useMemo } from "react";
import { useSql, num } from "@/api";
import type { DayRecord } from "./episodes";
import { addDays } from "./episodes";

function tzLit(tz: string): string {
  return `'${tz.replace(/'/g, "''")}'`;
}
function dateInstant(date: string, tz: string, plusDays = 0): string {
  const d = plusDays ? addDays(date, plusDays) : date;
  return `('${d}'::timestamp AT TIME ZONE ${tzLit(tz)})`;
}

export interface DayData {
  records: DayRecord[];
  loading: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => void;
  truncated: boolean;
}

/** All records that intersect the local day [start, end), with their local time. */
export function useDayRecords(date: string | null, tz: string): DayData {
  const statement =
    date == null
      ? null
      : `SELECT id::int AS id, channel, happened_at, ended_at, ` +
        `(happened_at AT TIME ZONE ${tzLit(tz)}) AS local_time, ` +
        `(ended_at AT TIME ZONE ${tzLit(tz)}) AS local_end, ` +
        `lat, lon, content, thread_id::int AS thread_id, person_id::int AS person_id, display_name ` +
        `FROM api.messages ` +
        `WHERE happened_at < ${dateInstant(date, tz, 1)} ` +
        `AND coalesce(ended_at, happened_at) >= ${dateInstant(date, tz, 0)} ` +
        `ORDER BY happened_at`;
  const q = useSql<RawRow>(statement, { rowCap: 4000, priority: "visible", keyParts: ["day", date ?? "", tz] });
  const records = useMemo<DayRecord[]>(
    () =>
      (q.data?.rows ?? []).map((r) => ({
        id: Number(r.id),
        channel: r.channel,
        happenedAt: r.happened_at,
        endedAt: r.ended_at,
        localTime: r.local_time,
        localEnd: r.local_end,
        content: r.content ?? "",
        lat: r.lat == null ? null : num(r.lat),
        lon: r.lon == null ? null : num(r.lon),
        personId: r.person_id == null ? null : Number(r.person_id),
        displayName: r.display_name,
        threadId: r.thread_id == null ? null : Number(r.thread_id),
      })),
    [q.data],
  );
  return {
    records,
    loading: q.isLoading,
    isError: q.isError,
    error: q.error,
    refetch: () => void q.refetch(),
    truncated: q.data?.truncated ?? false,
  };
}

export interface HeatCell {
  date: string;
  n: number;
}

/** Records per local day across the months around `date`, for the jump heat. */
export function useCalendarHeat(date: string | null, tz: string, span = 100): Map<string, number> {
  const statement =
    date == null
      ? null
      : `SELECT (happened_at AT TIME ZONE ${tzLit(tz)})::date::text AS d, count(*)::int AS n ` +
        `FROM api.messages ` +
        `WHERE happened_at >= ${dateInstant(addDays(date, -span), tz, 0)} ` +
        `AND happened_at < ${dateInstant(addDays(date, span), tz, 1)} ` +
        `GROUP BY 1`;
  const q = useSql<{ d: string; n: string }>(statement, {
    rowCap: 1000,
    priority: "background",
    keyParts: ["heat", date ?? "", tz, span],
  });
  return useMemo(() => {
    const m = new Map<string, number>();
    for (const r of q.data?.rows ?? []) m.set(r.d, num(r.n));
    return m;
  }, [q.data]);
}

interface RawRow {
  id: string;
  channel: string;
  happened_at: string;
  ended_at: string | null;
  local_time: string;
  local_end: string | null;
  lat: string | null;
  lon: string | null;
  content: string | null;
  thread_id: string | null;
  person_id: string | null;
  display_name: string | null;
}
