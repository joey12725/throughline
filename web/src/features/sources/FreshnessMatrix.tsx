/*
 * The freshness matrix: channels x the last N days as a canvas heat grid.
 * Count per local day on a log scale in the source's colour. Empty days are not
 * all equal — a *hole* (the channel's rhythm predicts records but none arrived)
 * is outlined, a source-*reported* gap is a solid cross-hatch, and an *inferred*
 * quiet day is a faint hatch; days outside a channel's span stay blank. So you
 * can tell "nothing happened" from "something is wrong".
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSourceColor } from "@/ui";
import { usePrefs } from "@/store/prefs";
import { classifyChannel, intensity, type DayClass, type ReportedGap } from "./matrix";
import { recentDays, weekdayOf, MATRIX_DAYS } from "./data";
import styles from "./Sources.module.css";

export interface MatrixChannel {
  name: string;
  plugin: string;
  channelIndex: number;
}

export interface FreshnessMatrixProps {
  channels: MatrixChannel[];
  index: Map<string, Map<string, number>>;
  reported?: Map<string, ReportedGap[]>;
  days?: number;
  /** Row height in CSS pixels. */
  rowH?: number;
  labelW?: number;
}

interface Hover {
  x: number;
  y: number;
  channel: string;
  day: string;
  cls: DayClass;
}

function cssVar(el: HTMLElement, name: string): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

export function FreshnessMatrix({
  channels,
  index,
  reported,
  days = MATRIX_DAYS,
  rowH = 14,
  labelW = 150,
}: FreshnessMatrixProps): ReactNode {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const { rgbFor, theme } = useSourceColor();
  const tz = usePrefs((s) => s.tz);
  const [width, setWidth] = useState(720);
  const [hover, setHover] = useState<Hover | null>(null);

  const dayKeys = useMemo(() => recentDays(days, tz), [days, tz]);

  const classified = useMemo(
    () =>
      channels.map((ch) =>
        classifyChannel(dayKeys, index.get(ch.name) ?? new Map(), weekdayOf, reported?.get(ch.name) ?? []),
      ),
    [channels, dayKeys, index, reported],
  );

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(Math.max(320, Math.floor(w)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Keep cells at a legible minimum; when that is wider than the panel the wrap
  // scrolls horizontally (fine on phones) rather than collapsing to sub-pixel cells.
  const minCell = 5;
  const gridW = Math.max(days * minCell, width - labelW - 8);
  const cellW = gridW / days;
  const canvasW = labelW + gridW + 8;
  const topPad = 16;
  const height = topPad + channels.length * rowH + 2;

  const geom = useRef({ cellW, rowH, labelW, topPad });
  geom.current = { cellW, rowH, labelW, topPad };

  useEffect(() => {
    const canvas = canvasRef.current;
    const el = wrapRef.current;
    if (!canvas || !el) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.floor(canvasW * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = `${canvasW}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, canvasW, height);

    const gridline = cssVar(el, "--line") || "rgba(128,128,128,0.18)";
    const faint = cssVar(el, "--ink-3") || "rgba(128,128,128,0.5)";
    const holeStroke = cssVar(el, "--warn") || "#c98a00";
    const reportedCol = cssVar(el, "--bad") || "#c0392b";
    const text = cssVar(el, "--ink-2") || "#888";
    const isDark = theme === "nocturne";

    // Canvas ignores CSS custom properties in `font`, so name a concrete stack.
    const LABEL_FONT = '11px "Instrument Sans Variable", "Instrument Sans", system-ui, sans-serif';
    ctx.font = LABEL_FONT;
    ctx.textBaseline = "middle";

    // Month ticks along the top.
    ctx.fillStyle = text;
    ctx.textAlign = "left";
    let lastMonth = "";
    dayKeys.forEach((dayKey, d) => {
      const mon = dayKey.slice(0, 7);
      if (mon !== lastMonth) {
        lastMonth = mon;
        const x = labelW + d * cellW;
        const label = new Date(`${dayKey}T00:00:00Z`).toLocaleDateString(undefined, {
          month: "short",
          timeZone: "UTC",
        });
        ctx.fillText(label, x + 1, topPad / 2);
        ctx.strokeStyle = gridline;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, topPad);
        ctx.lineTo(Math.round(x) + 0.5, height);
        ctx.stroke();
      }
    });

    channels.forEach((ch, r) => {
      const y = topPad + r * rowH;
      const base = classified[r];
      if (!base) return;
      // Row label.
      ctx.fillStyle = text;
      ctx.textAlign = "left";
      const short = ch.name.length > 22 ? ch.name.slice(0, 21) + "…" : ch.name;
      ctx.fillText(short, 2, y + rowH / 2);

      base.days.forEach((cls, d) => {
        const x = labelW + d * cellW;
        const cx = Math.round(x);
        const cw = Math.max(1, Math.ceil(cellW) - 1);
        const ch2 = rowH - 2;
        const cy = y + 1;
        if (cls.kind === "active") {
          const t = intensity(cls.count, base.maxCount);
          const alpha = 0.22 + 0.78 * t;
          ctx.fillStyle = rgbFor(ch.plugin, ch.channelIndex, alpha);
          ctx.fillRect(cx, cy, cw, ch2);
        } else if (cls.kind === "reported") {
          ctx.fillStyle = isDark ? "rgba(192,57,43,0.22)" : "rgba(192,57,43,0.14)";
          ctx.fillRect(cx, cy, cw, ch2);
          hatch(ctx, cx, cy, cw, ch2, reportedCol, 0.7);
        } else if (cls.kind === "hole") {
          ctx.strokeStyle = holeStroke;
          ctx.globalAlpha = 0.8;
          ctx.strokeRect(cx + 0.5, cy + 0.5, cw - 1, ch2 - 1);
          ctx.globalAlpha = 1;
        } else if (cls.kind === "quiet") {
          hatch(ctx, cx, cy, cw, ch2, faint, 0.35);
        }
        // "outside": nothing drawn.
      });
    });

    // today marker (rightmost column)
    ctx.strokeStyle = cssVar(el, "--brass") || "#b8893b";
    ctx.beginPath();
    const tx = labelW + days * cellW;
    ctx.moveTo(Math.round(tx) + 0.5, topPad);
    ctx.lineTo(Math.round(tx) + 0.5, height);
    ctx.stroke();
  }, [channels, classified, dayKeys, width, height, cellW, days, labelW, rowH, topPad, rgbFor, theme]);

  const onMove = (e: React.MouseEvent): void => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const r = Math.floor((py - topPad) / rowH);
    const d = Math.floor((px - labelW) / cellW);
    if (r < 0 || r >= channels.length || d < 0 || d >= days) {
      setHover(null);
      return;
    }
    const cls = classified[r]?.days[d];
    const ch = channels[r];
    if (!cls || !ch) {
      setHover(null);
      return;
    }
    setHover({ x: px, y: py, channel: ch.name, day: cls.day, cls });
  };

  return (
    <div ref={wrapRef} className={styles.matrixWrap} onMouseLeave={() => setHover(null)}>
      <canvas
        ref={canvasRef}
        onMouseMove={onMove}
        role="img"
        aria-label={`Freshness of ${channels.length} channels over the last ${days} days`}
      />
      {hover ? (
        <div
          className={styles.matrixTip}
          style={{ left: Math.min(hover.x + 12, canvasW - 180), top: hover.y + 18 }}
          role="status"
        >
          <strong>{hover.channel}</strong>
          <span>{hover.day}</span>
          <span className={styles.tipKind} data-kind={hover.cls.kind}>
            {hover.cls.kind === "active"
              ? `${hover.cls.count} record${hover.cls.count === 1 ? "" : "s"}`
              : KIND_WORD[hover.cls.kind]}
          </span>
        </div>
      ) : null}
    </div>
  );
}

const KIND_WORD: Record<string, string> = {
  hole: "missing — a record was expected",
  quiet: "quiet — no activity expected",
  reported: "reported gap — the source flagged this",
  outside: "outside this channel's span",
};

function hatch(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  color: string,
  alpha: number,
): void {
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  for (let i = -h; i < w; i += 3) {
    ctx.beginPath();
    ctx.moveTo(x + i, y + h);
    ctx.lineTo(x + i + h, y);
    ctx.stroke();
  }
  ctx.restore();
}
