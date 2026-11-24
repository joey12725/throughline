/*
 * A slim time strip under the map: weekly geo-record density as a bar histogram
 * (so coverage gaps read as gaps, not a ramp), the brass "now" rule with the
 * future dimmed, and a drag brush that sets the global time scope (:from/:to)
 * every other panel reads. Canvas for the density, a thin interaction layer on
 * top. Both themes; reduced-motion safe (no animation).
 */
import { useRef, useState } from "react";
import { useChartColors } from "@/charts";
import { useCanvas, useElementSize } from "@/charts/runtime/hooks";
import { formatInTz } from "@/store/scopes";
import styles from "./places.module.css";

export interface DensityDay {
  t: number; // epoch ms (day start)
  n: number;
}

interface Props {
  days: DensityDay[];
  from: string | null;
  to: string | null;
  tz: string;
  now: number;
  plugin: string;
  onBrush: (from: string | null, to: string | null) => void;
}

export default function TimelineStrip({ days, from, to, tz, now, plugin, onBrush }: Props) {
  const c = useChartColors(plugin);
  const [hostRef, size] = useElementSize<HTMLDivElement>();
  const dragRef = useRef<{ x0: number; x1: number } | null>(null);
  const [drag, setDrag] = useState<{ x0: number; x1: number } | null>(null);

  const WEEK = 7 * 86400_000;
  const t0 = days.length ? days[0]!.t : now - WEEK;
  const t1 = Math.max(days.length ? days[days.length - 1]!.t + WEEK : now, now);
  const span = Math.max(1, t1 - t0);
  const xOf = (t: number) => ((t - t0) / span) * size.width;
  const tOf = (x: number) => t0 + (x / Math.max(1, size.width)) * span;
  const maxN = days.reduce((m, d) => Math.max(m, d.n), 1);
  const fromMs = from ? Date.parse(from) : null;
  const toMs = to ? Date.parse(to) : null;

  const canvasRef = useCanvas(
    (ctx, box) => {
      ctx.clearRect(0, 0, box.width, box.height);
      const baseY = box.height - 1;
      // Future shade (right of now).
      const nowX = xOf(now);
      if (nowX < box.width) {
        ctx.fillStyle = c.tokens.bg2;
        ctx.globalAlpha = 0.5;
        ctx.fillRect(nowX, 0, box.width - nowX, box.height);
        ctx.globalAlpha = 1;
      }
      // Baseline hairline, so the empty weeks still read as time.
      ctx.strokeStyle = c.tokens.line;
      ctx.globalAlpha = 0.6;
      ctx.beginPath();
      ctx.moveTo(0, baseY + 0.5);
      ctx.lineTo(box.width, baseY + 0.5);
      ctx.stroke();
      ctx.globalAlpha = 1;
      // Density bars, one per week — gaps in coverage stay empty.
      const barW = Math.max(1.4, (size.width / Math.max(1, span / WEEK)) - 0.6);
      ctx.fillStyle = c.sourceRgb(0, 0.72);
      for (const d of days) {
        const x = xOf(d.t);
        const h = Math.max(1, (Math.log1p(d.n) / Math.log1p(maxN)) * (box.height - 4));
        ctx.fillRect(x, baseY - h, barW, h);
      }
      // Selection band.
      const sel = drag ?? (fromMs != null || toMs != null ? { x0: xOf(fromMs ?? t0), x1: xOf(toMs ?? t1) } : null);
      if (sel) {
        const x0 = Math.min(sel.x0, sel.x1);
        const x1 = Math.max(sel.x0, sel.x1);
        ctx.fillStyle = c.tokens.brass;
        ctx.globalAlpha = 0.16;
        ctx.fillRect(x0, 0, x1 - x0, box.height);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = c.tokens.brass;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x0 + 0.5, 0);
        ctx.lineTo(x0 + 0.5, box.height);
        ctx.moveTo(x1 - 0.5, 0);
        ctx.lineTo(x1 - 0.5, box.height);
        ctx.stroke();
      }
      // Now rule.
      if (nowX >= 0 && nowX <= box.width) {
        ctx.strokeStyle = c.tokens.brass;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(nowX + 0.5, 0);
        ctx.lineTo(nowX + 0.5, box.height);
        ctx.stroke();
      }
    },
    size,
    [days, c.tokens, maxN, fromMs, toMs, drag, now, size.width],
  );

  const pos = (e: React.PointerEvent) => {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    return Math.max(0, Math.min(size.width, e.clientX - r.left));
  };
  const onDown = (e: React.PointerEvent) => {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const x = pos(e);
    dragRef.current = { x0: x, x1: x };
    setDrag({ x0: x, x1: x });
  };
  const onMove = (e: React.PointerEvent) => {
    if (!dragRef.current) return;
    const x = pos(e);
    dragRef.current.x1 = x;
    setDrag({ x0: dragRef.current.x0, x1: x });
  };
  const onUp = (e: React.PointerEvent) => {
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    const d = dragRef.current;
    dragRef.current = null;
    setDrag(null);
    if (!d) return;
    if (Math.abs(d.x1 - d.x0) < 4) {
      onBrush(null, null); // a click clears the scope
      return;
    }
    const a = new Date(tOf(Math.min(d.x0, d.x1))).toISOString();
    const b = new Date(tOf(Math.max(d.x0, d.x1))).toISOString();
    onBrush(a, b);
  };

  const label = fromMs != null && toMs != null ? `${formatInTz(fromMs, tz, { dateStyle: "medium" })} → ${formatInTz(toMs, tz, { dateStyle: "medium" })}` : "All time · drag to scope";

  return (
    <div className={styles.timeline}>
      <div ref={hostRef} className={styles.timelineCanvasWrap} onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp} role="slider" aria-label="Time scope. Drag to select a range; click to clear." aria-valuetext={label} tabIndex={0}>
        <canvas ref={canvasRef} className={styles.timelineCanvas} />
      </div>
      <span className={styles.timelineLabel}>{label}</span>
    </div>
  );
}
