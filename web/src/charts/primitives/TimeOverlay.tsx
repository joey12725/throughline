/*
 * The time grammar and the brush, as one SVG overlay over a Plot time chart. It
 * reads the rendered plot's x scale (pixel mapping), then paints the now-rule
 * (brass), the dimmed/dashed future, and hatching over periods a source does not
 * cover. Dragging sets the global time scope; a handle clears it.
 */
import { useId, useMemo, useRef, useState, type ReactNode } from "react";
import type { ChartTokens } from "../runtime/tokens";
import { timeSegments } from "../runtime/timeGrammar";
import type { BrushRange, Coverage } from "../types";
import type { PlotNode } from "./PlotFigure";
import styles from "./TimeOverlay.module.css";

interface XScale {
  apply(v: unknown): number;
  invert(px: number): unknown;
  range: number[];
  domain: unknown[];
}

export interface TimeOverlayProps {
  plot: PlotNode | null;
  width: number;
  height: number;
  tokens: ChartTokens;
  now: number;
  coverage?: Coverage | null;
  hasScope?: boolean;
  onBrush?: (r: BrushRange) => void;
}

function toMs(v: unknown): number {
  if (v instanceof Date) return v.getTime();
  if (typeof v === "number") return v;
  const t = Date.parse(String(v));
  return Number.isNaN(t) ? NaN : t;
}

export function TimeOverlay({ plot, width, height, tokens, now, coverage, hasScope, onBrush }: TimeOverlayProps): ReactNode {
  const hatchId = useId().replace(/:/g, "");
  const [drag, setDrag] = useState<{ x0: number; x1: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const scale = useMemo<XScale | null>(() => {
    if (!plot) return null;
    try {
      const s = plot.scale("x");
      if (!s || typeof s.apply !== "function" || !Array.isArray(s.domain)) return null;
      const d0 = toMs(s.domain[0]);
      const d1 = toMs(s.domain[s.domain.length - 1]);
      if (Number.isNaN(d0) || Number.isNaN(d1)) return null; // not a temporal axis
      return s as XScale;
    } catch {
      return null;
    }
  }, [plot]);

  if (!scale || width <= 0 || height <= 0) return null;
  const [rLeft, rRight] = [Math.min(scale.range[0]!, scale.range[1]!), Math.max(scale.range[0]!, scale.range[1]!)];
  const domFrom = toMs(scale.domain[0]);
  const domTo = toMs(scale.domain[scale.domain.length - 1]);
  const top = 6;
  const bottom = height - 24;
  const plotH = Math.max(0, bottom - top);

  const px = (ms: number): number => {
    try {
      return scale.apply(new Date(ms));
    } catch {
      return rLeft;
    }
  };

  const segs = timeSegments(domFrom, domTo, coverage ? { from: toMs(coverage.from), to: toMs(coverage.to) } : null, now);
  const nowPx = now >= domFrom && now <= domTo ? px(now) : null;

  const onDown = (e: React.PointerEvent): void => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = clampPx(e.clientX - rect.left, rLeft, rRight);
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setDrag({ x0: x, x1: x });
  };
  const onMove = (e: React.PointerEvent): void => {
    if (!drag) return;
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    setDrag({ ...drag, x1: clampPx(e.clientX - rect.left, rLeft, rRight) });
  };
  const onUp = (): void => {
    if (!drag) return;
    const a = Math.min(drag.x0, drag.x1);
    const b = Math.max(drag.x0, drag.x1);
    setDrag(null);
    if (b - a < 4) return; // a click, not a brush
    const from = new Date(toMs(scale.invert(a))).toISOString();
    const to = new Date(toMs(scale.invert(b))).toISOString();
    onBrush?.({ from, to });
  };

  return (
    <svg ref={svgRef} className={styles.overlay} width={width} height={height} onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} aria-hidden="true">
      <defs>
        <pattern id={`hatch-${hatchId}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke={tokens.ink3} strokeWidth="1" opacity="0.5" />
        </pattern>
      </defs>
      {segs.map((s, i) => {
        const x = px(s.from);
        const w = Math.max(0, px(s.to) - x);
        if (s.kind === "future") {
          return <rect key={i} x={x} y={top} width={w} height={plotH} fill={tokens.bg0} opacity={0.42} />;
        }
        if (s.kind === "uncovered") {
          return <rect key={i} x={x} y={top} width={w} height={plotH} fill={`url(#hatch-${hatchId})`} />;
        }
        return null;
      })}
      {nowPx !== null && (
        <>
          <line x1={nowPx} y1={top} x2={nowPx} y2={bottom} stroke={tokens.brass} strokeWidth="1.5" />
          <circle cx={nowPx} cy={top} r="2.5" fill={tokens.brass} />
        </>
      )}
      {drag && (
        <rect
          x={Math.min(drag.x0, drag.x1)}
          y={top}
          width={Math.abs(drag.x1 - drag.x0)}
          height={plotH}
          fill={tokens.brass}
          opacity={0.14}
          stroke={tokens.brass}
          strokeWidth="1"
        />
      )}
      {hasScope && (
        <g className={styles.clear} onPointerDown={(e) => { e.stopPropagation(); onBrush?.({ from: null, to: null }); }}>
          <rect x={width - 20} y={2} width="16" height="16" rx="3" fill={tokens.bg2} stroke={tokens.line} />
          <line x1={width - 16} y1={6} x2={width - 8} y2={14} stroke={tokens.ink2} strokeWidth="1.2" />
          <line x1={width - 8} y1={6} x2={width - 16} y2={14} stroke={tokens.ink2} strokeWidth="1.2" />
        </g>
      )}
    </svg>
  );
}

function clampPx(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}
