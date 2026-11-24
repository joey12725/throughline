/*
 * The day's swimlanes: one lane per channel across 00:00–24:00 local, grouped by
 * plugin into collapsible groups (like the Loom) so a plugin with many small
 * channels stays tidy. The treatment of each record comes from its shape, never
 * its name:
 *   · a record whose interval covers almost the whole day is a "day fact"
 *     (a daily summary) — lifted out of the lanes into an all-day strip;
 *   · a long interval that overlaps the night reads as sleep/rest: one calm,
 *     monochrome shaded band behind the lanes;
 *   · other long intervals are drawn as quiet outlined bars, not solid washes;
 *   · short intervals are solid bars and instants are dots.
 * A brass rule marks "now" when the day is today. Canvas for the marks, an HTML
 * gutter for crisp labels and the group carets, an HTML tooltip for the hover.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { channelLabel, useSourceColor, Sensitive } from "@/ui";
import { usePrefs } from "@/store/prefs";
import { hourOffset } from "./episodes";
import type { DayRecord } from "./episodes";
import styles from "./Day.module.css";

const LANE_H = 22;
const GROUP_H = 19;
const TOP = 30;
const BOTTOM = 10;
const LABEL_W = 150;
const LABEL_W_NARROW = 104;

const DAY_FACT_H = 20; // interval covering ~the whole day → a day fact
const SLEEP_MIN_H = 2.5; // a long interval…
const LONG_MIN_S = 45 * 60; // …or a merely long one (quiet outline)
const AUTO_COLLAPSE_MIN = 6; // plugins with this many channels start collapsed

type RecordKind = "point" | "bar" | "longbar" | "sleep";

interface Classified {
  dayFacts: DayRecord[];
  laneRecords: DayRecord[];
  kind: Map<number, RecordKind>;
  sleep: DayRecord[];
}

interface LaneRow {
  kind: "group" | "lane" | "summary";
  y: number;
  h: number;
  plugin: string;
  channel?: string; // lane: the channel; summary: undefined
  channels: string[]; // summary: all collapsed channels
  collapsed: boolean;
  /** first row of a plugin group — gets a separator (and, if a solo lane, a dot) */
  groupStart?: boolean;
  solo?: boolean;
}

interface Hit {
  rec: DayRecord;
  x: number;
  y: number;
  w: number;
  h: number;
}

export function Swimlanes({
  records,
  date,
  nowHour,
  isToday,
  pluginOf,
  chIndex,
  onOpen,
}: {
  records: DayRecord[];
  date: string;
  nowHour: number;
  isToday: boolean;
  pluginOf: Map<string, string>;
  chIndex: Map<string, number>;
  onOpen: (id: number, channel: string) => void;
}): ReactNode {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const hitsRef = useRef<Hit[]>([]);
  const [hover, setHover] = useState<{ rec: DayRecord; x: number; y: number } | null>(null);
  const [plotW, setPlotW] = useState(600);
  const [labelW, setLabelW] = useState(LABEL_W);
  const theme = usePrefs((s) => s.theme);
  const { rgbFor } = useSourceColor();

  const cls = useMemo(() => classify(records, date), [records, date]);

  // Merge overlapping rest/sleep intervals so the night reads as one calm band.
  const sleepBands = useMemo(() => {
    const spans = cls.sleep
      .map((r) => {
        const s = hourOffset(r.localTime, date);
        const e = r.localEnd ? hourOffset(r.localEnd, date) : s + 0.3;
        return [s, e] as [number, number];
      })
      .sort((a, b) => a[0] - b[0]);
    const merged: [number, number][] = [];
    for (const [s, e] of spans) {
      const last = merged[merged.length - 1];
      if (last && s <= last[1] + 0.25) last[1] = Math.max(last[1], e);
      else merged.push([s, e]);
    }
    return merged;
  }, [cls.sleep, date]);

  // Group channels by plugin (stable order), then build the collapsible layout.
  const groups = useMemo(() => {
    const byPlugin = new Map<string, Map<string, number>>();
    for (const r of cls.laneRecords) {
      const plugin = pluginOf.get(r.channel) ?? r.channel.split(".")[0] ?? r.channel;
      let m = byPlugin.get(plugin);
      if (!m) {
        m = new Map();
        byPlugin.set(plugin, m);
      }
      m.set(r.channel, (m.get(r.channel) ?? 0) + 1);
    }
    return [...byPlugin.entries()]
      .map(([plugin, chans]) => ({ plugin, channels: [...chans.keys()].sort() }))
      .sort((a, b) => a.plugin.localeCompare(b.plugin));
  }, [cls.laneRecords, pluginOf]);

  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  // Seed the auto-collapse once the groups are known.
  const seeded = useRef(false);
  useEffect(() => {
    if (seeded.current || groups.length === 0) return;
    seeded.current = true;
    const auto = new Set<string>();
    for (const g of groups) if (g.channels.length >= AUTO_COLLAPSE_MIN) auto.add(g.plugin);
    if (auto.size) setCollapsed(auto);
  }, [groups]);

  const { layout, contentH } = useMemo(() => computeLayout(groups, collapsed), [groups, collapsed]);
  const height = TOP + contentH + BOTTOM;

  // Measure width → responsive gutter + plot width.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const cw = el.clientWidth;
      const lw = cw < 560 ? LABEL_W_NARROW : LABEL_W;
      setLabelW(lw);
      setPlotW(Math.max(200, cw - lw));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const laneByChannel = useMemo(() => {
    const m = new Map<string, LaneRow>();
    for (const r of layout) {
      if (r.kind === "lane" && r.channel) m.set(r.channel, r);
      else if (r.kind === "summary") for (const c of r.channels) m.set(c, r);
    }
    return m;
  }, [layout]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.floor(plotW * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, plotW, height);

    const tok = readTokens(canvas);
    const hourX = (h: number): number => Math.max(0, Math.min(plotW, (clampHour(h) / 24) * plotW));

    // Hour grid.
    ctx.font = "10px var(--font-mono), monospace";
    ctx.textBaseline = "alphabetic";
    for (let h = 0; h <= 24; h += 3) {
      const x = hourX(h);
      ctx.strokeStyle = tok.line;
      ctx.globalAlpha = h % 6 === 0 ? 0.7 : 0.3;
      ctx.beginPath();
      ctx.moveTo(x, TOP - 6);
      ctx.lineTo(x, height - BOTTOM);
      ctx.stroke();
      ctx.globalAlpha = 1;
      if (h % 6 === 0 || h === 24) {
        ctx.fillStyle = tok.ink3;
        ctx.textAlign = h === 0 ? "left" : h === 24 ? "right" : "center";
        ctx.fillText(`${String(h).padStart(2, "0")}:00`, x, TOP - 12);
      }
    }

    // Sleep / rest: calm, monochrome band(s) behind the lanes.
    for (const [s, e] of sleepBands) {
      const bx0 = hourX(s);
      const bx1 = hourX(e);
      ctx.fillStyle = withAlpha(tok.ink3, 0.08);
      ctx.fillRect(bx0, TOP - 2, Math.max(2, bx1 - bx0), height - BOTTOM - (TOP - 2));
      ctx.strokeStyle = withAlpha(tok.ink3, 0.22);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(bx0 + 0.5, TOP - 2);
      ctx.lineTo(bx0 + 0.5, height - BOTTOM);
      ctx.moveTo(bx1 - 0.5, TOP - 2);
      ctx.lineTo(bx1 - 0.5, height - BOTTOM);
      ctx.stroke();
      ctx.fillStyle = tok.ink3;
      ctx.font = "9px var(--font-mono), monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      if (bx1 - bx0 > 44) ctx.fillText("rest", (bx0 + bx1) / 2, TOP + 1);
      ctx.textBaseline = "alphabetic";
    }

    const hits: Hit[] = [];

    // Lane baselines.
    for (const row of layout) {
      if (row.kind === "group") continue;
      const yMid = TOP + row.y + row.h / 2;
      ctx.strokeStyle = tok.line;
      ctx.globalAlpha = 0.22;
      ctx.beginPath();
      ctx.moveTo(0, yMid + row.h / 2 - 0.5);
      ctx.lineTo(plotW, yMid + row.h / 2 - 0.5);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Marks.
    for (const r of cls.laneRecords) {
      const row = laneByChannel.get(r.channel);
      if (!row) continue;
      const y = TOP + row.y + row.h / 2;
      const plugin = pluginOf.get(r.channel) ?? r.channel;
      const ci = row.kind === "summary" ? 0 : chIndex.get(r.channel) ?? 0;
      const sH = hourOffset(r.localTime, date);
      const kind = cls.kind.get(r.id) ?? "point";

      if (kind === "bar" || kind === "longbar") {
        const eH = r.localEnd ? hourOffset(r.localEnd, date) : sH;
        const bx0 = hourX(sH);
        const bx1 = Math.max(bx0 + 3, hourX(eH));
        const barY = y - 5;
        if (kind === "longbar") {
          // Quiet outline, not a wash.
          ctx.fillStyle = rgbFor(plugin, ci, 0.1);
          roundRect(ctx, bx0, barY, bx1 - bx0, 10, 3);
          ctx.fill();
          ctx.strokeStyle = rgbFor(plugin, ci, 0.7);
          ctx.lineWidth = 1;
          roundRect(ctx, bx0 + 0.5, barY + 0.5, bx1 - bx0 - 1, 9, 3);
          ctx.stroke();
        } else {
          ctx.fillStyle = rgbFor(plugin, ci, 0.9);
          roundRect(ctx, bx0, barY, bx1 - bx0, 10, 3);
          ctx.fill();
        }
        hits.push({ rec: r, x: bx0, y: barY, w: bx1 - bx0, h: 10 });
      } else {
        if (sH < 0 || sH > 24) continue;
        const x = hourX(sH);
        ctx.fillStyle = rgbFor(plugin, ci, 0.22);
        ctx.beginPath();
        ctx.arc(x, y, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = rgbFor(plugin, ci, 1);
        ctx.beginPath();
        ctx.arc(x, y, 2.5, 0, Math.PI * 2);
        ctx.fill();
        hits.push({ rec: r, x: x - 6, y: y - 6, w: 12, h: 12 });
      }
    }

    // Now-rule.
    if (isToday) {
      const nx = hourX(nowHour);
      ctx.strokeStyle = tok.brass;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(nx, TOP - 8);
      ctx.lineTo(nx, height - BOTTOM);
      ctx.stroke();
      ctx.lineWidth = 1;
      ctx.fillStyle = tok.brass;
      ctx.beginPath();
      ctx.arc(nx, TOP - 8, 3, 0, Math.PI * 2);
      ctx.fill();
    }

    hitsRef.current = hits;
  }, [cls, sleepBands, layout, laneByChannel, plotW, height, date, isToday, nowHour, pluginOf, chIndex, rgbFor, theme]);

  useEffect(() => {
    draw();
  }, [draw]);

  const toggleGroup = useCallback((plugin: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(plugin)) next.delete(plugin);
      else next.add(plugin);
      return next;
    });
  }, []);

  const onMove = (e: React.PointerEvent): void => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    let found: Hit | null = null;
    for (const h of hitsRef.current) {
      if (x >= h.x - 2 && x <= h.x + h.w + 2 && y >= h.y - 2 && y <= h.y + h.h + 2) {
        found = h;
        break;
      }
    }
    setHover(found ? { rec: found.rec, x: found.x + found.w / 2 + labelW, y: found.y } : null);
    canvas.style.cursor = found ? "pointer" : "default";
  };

  const onClick = (): void => {
    if (hover) onOpen(hover.rec.id, hover.rec.channel);
  };

  return (
    <div className={styles.swimWrap} ref={wrapRef}>
      {cls.dayFacts.length > 0 && (
        <div className={styles.dayFacts}>
          <span className={styles.dayFactsLabel}>All day</span>
          {cls.dayFacts.map((r) => {
            const plugin = pluginOf.get(r.channel) ?? r.channel;
            return (
              <button
                key={r.id}
                type="button"
                className={styles.dayFact}
                style={{ ["--c" as string]: rgbFor(plugin, chIndex.get(r.channel) ?? 0, 1) }}
                onClick={() => onOpen(r.id, r.channel)}
                title={`${r.channel} — open`}
              >
                <span className={styles.dayFactDot} aria-hidden />
                <span className={styles.dayFactName}>{channelShortIn(pluginOf, r.channel)}</span>
              </button>
            );
          })}
        </div>
      )}

      <div className={styles.swimBody}>
        {/* gutter: plugin groups + lane labels */}
        <div className={styles.swimGutter} style={{ width: labelW }}>
          {layout.map((row, i) =>
            row.kind === "group" ? (
              <button
                key={`g${i}`}
                type="button"
                className={styles.swimGroup}
                style={{ top: TOP + row.y, height: row.h }}
                onClick={() => toggleGroup(row.plugin)}
                aria-expanded={!row.collapsed}
                title={`${row.plugin} · click to ${row.collapsed ? "expand" : "collapse"}`}
              >
                <span className={styles.swimCaret} data-collapsed={row.collapsed}>▸</span>
                <span className={styles.swimGroupDot} style={{ background: rgbFor(row.plugin, 0) }} />
                <span className={styles.swimGroupName}>{row.plugin}</span>
                {row.collapsed && row.channels.length > 1 && (
                  <span className={styles.swimGroupCount}>{row.channels.length}</span>
                )}
              </button>
            ) : (
              <div
                key={`l${i}`}
                className={styles.swimLabel}
                data-group-start={row.groupStart ? "true" : undefined}
                style={{ top: TOP + row.y, height: row.h }}
                title={row.kind === "summary" ? row.channels.join(", ") : row.channel}
              >
                {row.solo && (
                  <span className={styles.swimSoloDot} style={{ background: rgbFor(row.plugin, chIndex.get(row.channel ?? "") ?? 0) }} />
                )}
                <span className={styles.swimLabelText}>
                  {row.kind === "summary" ? `${row.channels.length} channels` : channelShortIn(pluginOf, row.channel ?? "")}
                </span>
              </div>
            ),
          )}
        </div>

        {/* plot */}
        <div className={styles.swimPlot} style={{ width: plotW }}>
          <canvas
            ref={canvasRef}
            className={styles.swimCanvas}
            style={{ height, width: plotW }}
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
            onClick={onClick}
            role="img"
            aria-label={`Timeline of ${cls.laneRecords.length} records across ${groups.length} sources on ${date}`}
          />
        </div>
      </div>

      {hover && (
        <div className={styles.swimTip} style={{ left: hover.x, top: hover.y + TOP + 4 }} role="status">
          <div className={styles.swimTipHead}>
            {hover.rec.displayName || channelShortIn(pluginOf, hover.rec.channel)}
            <span className={styles.swimTipTime}>{hover.rec.localTime.slice(11, 16)}</span>
          </div>
          <div className={styles.swimTipChan}>{hover.rec.channel}</div>
          <div className={styles.swimTipBody}>
            <Sensitive>{hover.rec.content}</Sensitive>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---- classification & layout (pure) -------------------------------------- */

function classify(records: DayRecord[], date: string): Classified {
  const dayFacts: DayRecord[] = [];
  const laneRecords: DayRecord[] = [];
  const sleep: DayRecord[] = [];
  const kind = new Map<number, RecordKind>();

  for (const r of records) {
    const hasEnd = !!r.endedAt;
    const durS = hasEnd ? (Date.parse(r.endedAt as string) - Date.parse(r.happenedAt)) / 1000 : 0;
    const durH = durS / 3600;

    // A record covering ~the whole day is a day fact, not a bar.
    if (hasEnd && durH >= DAY_FACT_H) {
      dayFacts.push(r);
      continue;
    }

    laneRecords.push(r);

    if (hasEnd && durH >= SLEEP_MIN_H && overlapsNight(r, date)) {
      kind.set(r.id, "sleep");
      sleep.push(r);
    } else if (hasEnd && durS >= LONG_MIN_S) {
      kind.set(r.id, "longbar");
    } else if (hasEnd && durS > 90) {
      kind.set(r.id, "bar");
    } else {
      kind.set(r.id, "point");
    }
  }
  return { dayFacts, laneRecords, kind, sleep };
}

/** Does the interval cover a chunk of the deep night [00:00–06:00] of this day? */
function overlapsNight(r: DayRecord, date: string): boolean {
  const s = hourOffset(r.localTime, date);
  const e = r.localEnd ? hourOffset(r.localEnd, date) : s;
  const lo = Math.max(s, 0);
  const hi = Math.min(e, 6);
  return hi - lo >= 1;
}

function computeLayout(
  groups: { plugin: string; channels: string[] }[],
  collapsed: ReadonlySet<string>,
): { layout: LaneRow[]; contentH: number } {
  const layout: LaneRow[] = [];
  let y = 0;
  for (const g of groups) {
    const isCollapsed = collapsed.has(g.plugin);
    // A single-channel group needs no header row.
    const soloChannel = g.channels.length === 1 ? g.channels[0] : null;
    if (soloChannel) {
      layout.push({ kind: "lane", y, h: LANE_H, plugin: g.plugin, channel: soloChannel, channels: g.channels, collapsed: false, groupStart: true, solo: true });
      y += LANE_H;
      continue;
    }
    layout.push({ kind: "group", y, h: GROUP_H, plugin: g.plugin, channels: g.channels, collapsed: isCollapsed, groupStart: true });
    y += GROUP_H;
    if (isCollapsed) {
      layout.push({ kind: "summary", y, h: LANE_H, plugin: g.plugin, channels: g.channels, collapsed: true });
      y += LANE_H;
    } else {
      for (const c of g.channels) {
        layout.push({ kind: "lane", y, h: LANE_H, plugin: g.plugin, channel: c, channels: g.channels, collapsed: false });
        y += LANE_H;
      }
    }
  }
  return { layout, contentH: y };
}

/* ---- small helpers ------------------------------------------------------- */

/** Short label for a channel within its plugin group (see channelLabel). */
function channelShortIn(pluginOf: Map<string, string>, ch: string): string {
  const plugin = pluginOf.get(ch);
  const siblings = [...pluginOf].filter(([, p]) => p === plugin).map(([c]) => c);
  return channelLabel(ch, plugin, siblings);
}

function clampHour(h: number): number {
  return Math.max(0, Math.min(24, h));
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  const rr = Math.min(r, h / 2, w / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function withAlpha(rgb: string, a: number): string {
  // rgb is "#rrggbb" or "rgb(r,g,b)"; convert to rgba.
  if (rgb.startsWith("#")) {
    const n = parseInt(rgb.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
  const m = /rgba?\(([^)]+)\)/.exec(rgb);
  if (m) {
    const parts = m[1]!.split(",").slice(0, 3).map((s) => s.trim());
    return `rgba(${parts.join(", ")}, ${a})`;
  }
  return rgb;
}

function readTokens(el: HTMLElement): { ink2: string; ink3: string; line: string; brass: string } {
  const s = getComputedStyle(el);
  const g = (n: string, f: string): string => s.getPropertyValue(n).trim() || f;
  return {
    ink2: g("--ink-2", "#A6A392"),
    ink3: g("--ink-3", "#6B6E72"),
    line: g("--line", "#2A3040"),
    brass: g("--brass", "#D9A64A"),
  };
}
