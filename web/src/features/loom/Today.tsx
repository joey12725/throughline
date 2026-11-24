/*
 * Today — the current local day (scope timezone) as swimlanes, one per source,
 * each record a tick at its time of day. One bounded query over api.day; the
 * whole panel links to the full Day screen.
 */
import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useSql } from "@/api";
import { Panel, EmptyState, Skeleton, useSourceColor } from "@/ui";
import { usePrefs } from "@/store/prefs";
import type { ChannelIndex } from "./useChannelIndex";
import styles from "./Today.module.css";

interface DayRow {
  id: string | number;
  channel: string;
  happened_at: string;
}

export function Today({ index }: { index: ChannelIndex }): ReactNode {
  const tz = usePrefs((s) => s.tz);
  const { cssFor } = useSourceColor();
  const date = useMemo(() => localDate(tz), [tz]);
  const offsetMin = useMemo(() => tzOffsetMinutes(tz, new Date()), [tz]);

  const q = useSql<DayRow>(
    `SELECT message_id AS id, channel, effective_time AS happened_at ` +
      `FROM api.day('${date}', '${sqlTz(tz)}') ORDER BY effective_time`,
    { rowCap: 4000, priority: "visible", keyParts: [date, tz] },
  );

  const lanes = useMemo(() => {
    const rows = q.data?.rows ?? [];
    const byPlugin = new Map<string, number[]>();
    for (const r of rows) {
      const info = index.byName.get(r.channel);
      const plugin = info?.plugin ?? r.channel.split(".")[0] ?? r.channel;
      const ms = Date.parse(r.happened_at);
      if (Number.isNaN(ms)) continue;
      const localHour = (((ms + offsetMin * 60000) % 86_400_000) + 86_400_000) % 86_400_000 / 3_600_000;
      const list = byPlugin.get(plugin) ?? [];
      list.push(localHour);
      byPlugin.set(plugin, list);
    }
    return [...byPlugin.entries()]
      .map(([plugin, hours]) => ({ plugin, hours, count: hours.length }))
      .sort((a, b) => b.count - a.count);
  }, [q.data, index, offsetMin]);

  const total = lanes.reduce((s, l) => s + l.count, 0);
  const nowHour = ((Date.now() + offsetMin * 60000) % 86_400_000) / 3_600_000;

  return (
    <Panel
      title="Today"
      subtitle={prettyDate(date)}
      actions={
        <Link className={styles.allLink} to={`/day/${date}`}>
          Open day →
        </Link>
      }
    >
      {q.isLoading ? (
        <Skeleton height={120} lines={5} />
      ) : lanes.length === 0 ? (
        <EmptyState title="Nothing yet today" description="Records land here through the day." />
      ) : (
        <div className={styles.wrap}>
          <div className={styles.hours} aria-hidden>
            {[0, 6, 12, 18, 24].map((h) => (
              <span key={h} className={styles.hourMark} style={{ left: `${(h / 24) * 100}%` }}>
                {h === 24 ? "24h" : `${h}`}
              </span>
            ))}
          </div>
          <div className={styles.lanes}>
            {lanes.map((l) => (
              <Link key={l.plugin} to={`/day/${date}`} className={styles.lane} title={`${l.plugin}: ${l.count}`}>
                <span className={styles.laneName}>{l.plugin}</span>
                <span className={styles.track}>
                  <span className={styles.nowLine} style={{ left: `${(nowHour / 24) * 100}%` }} aria-hidden />
                  {l.hours.map((h, i) => (
                    <span
                      key={i}
                      className={styles.tick}
                      style={{ left: `${(h / 24) * 100}%`, background: cssFor(l.plugin, 0) }}
                    />
                  ))}
                </span>
                <span className={styles.laneCount}>{l.count}</span>
              </Link>
            ))}
          </div>
          <div className={styles.foot}>{total.toLocaleString("en-US")} records today</div>
        </div>
      )}
    </Panel>
  );
}

function localDate(tz: string): string {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(new Date());
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}

function prettyDate(date: string): string {
  const [y = 1970, m = 1, d = 1] = date.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** Minutes to add to a UTC instant to get wall-clock time in tz (DST-correct for `at`). */
function tzOffsetMinutes(tz: string, at: Date): number {
  try {
    const dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    const parts = dtf.formatToParts(at);
    const get = (t: string): number => Number(parts.find((p) => p.type === t)?.value ?? "0");
    const asUTC = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
    return Math.round((asUTC - at.getTime()) / 60000);
  } catch {
    return 0;
  }
}

function sqlTz(tz: string): string {
  return tz.replace(/[^A-Za-z0-9_+\-/]/g, "") || "UTC";
}
