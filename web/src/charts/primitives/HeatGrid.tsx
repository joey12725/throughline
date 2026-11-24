/*
 * A rows x columns cell grid on <canvas>, DPR-aware and themed, with an HTML
 * tooltip overlay and click-drill. The shared engine behind weekday_hour,
 * status_matrix, thread_tapestry and link_matrix. Optional per-column time
 * grammar (future dimmed, uncovered hatched) keeps those four consistent.
 */
import { useRef, useState, type ReactNode } from "react";
import type { ChartTokens } from "../runtime/tokens";
import { clip } from "../runtime/format";
import { useCanvas } from "../runtime/hooks";
import { ChartBox, Legend, TruncationBanner, type LegendItem } from "./parts";
import { ChartTooltip, type TipState } from "./Tooltip";

export interface GridCell {
  value: number;
  color: string;
  title: string;
  rows?: { label: string; value: string; swatch?: string }[];
}

export interface GridAxisItem {
  key: string;
  label: string;
  /** Milliseconds, for the optional time grammar and sparse labels. */
  ms?: number;
}

export type ColKind = "covered" | "uncovered" | "future";

export interface HeatGridProps {
  width: number;
  height: number;
  tokens: ChartTokens;
  rows: GridAxisItem[];
  cols: GridAxisItem[];
  cell: (rowKey: string, colKey: string) => GridCell | null;
  rowHeaderWidth?: number;
  /** Show a column label every N columns (0 = none). */
  colLabelEvery?: number;
  colKind?: (ms: number) => ColKind;
  legend?: LegendItem[];
  truncated?: boolean;
  onPick?: (rowKey: string, colKey: string, cell: GridCell) => void;
  ariaLabel?: string;
}

export function HeatGrid(props: HeatGridProps): ReactNode {
  const { width, height, tokens, rows, cols, cell, colKind, legend, truncated, onPick, ariaLabel } = props;
  const [tip, setTip] = useState<TipState | null>(null);
  const geom = useRef({ headerW: 0, top: 0, cellW: 0, cellH: 0 });

  const bannerH = truncated ? 26 : 0;
  const legendH = legend && legend.length >= 2 ? 22 : 0;
  const headerW = props.rowHeaderWidth ?? Math.min(150, Math.max(40, width * 0.22));
  const topLabels = 16;
  const top = bannerH + topLabels;
  const gridW = Math.max(10, width - headerW - 6);
  const gridH = Math.max(10, height - top - legendH);
  const cellW = gridW / Math.max(1, cols.length);
  const cellH = gridH / Math.max(1, rows.length);
  geom.current = { headerW, top, cellW, cellH };
  const labelEvery = props.colLabelEvery ?? Math.max(1, Math.ceil(cols.length / Math.max(2, Math.floor(gridW / 64))));

  const canvasRef = useCanvas(
    (ctx) => {
      ctx.font = `10px ${tokens.fontUi}`;
      ctx.textBaseline = "middle";
      // Cells.
      for (let ri = 0; ri < rows.length; ri++) {
        const row = rows[ri]!;
        const y = top + ri * cellH;
        for (let ci = 0; ci < cols.length; ci++) {
          const col = cols[ci]!;
          const x = headerW + ci * cellW;
          const cl = cell(row.key, col.key);
          ctx.fillStyle = tokens.gridSoft;
          ctx.fillRect(x + 0.5, y + 0.5, Math.max(0, cellW - 1), Math.max(0, cellH - 1));
          if (cl && cl.value > 0) {
            ctx.fillStyle = cl.color;
            ctx.fillRect(x + 0.5, y + 0.5, Math.max(0, cellW - 1), Math.max(0, cellH - 1));
          }
          // Time grammar per column.
          if (colKind && col.ms !== undefined) {
            const k = colKind(col.ms);
            if (k === "future") {
              ctx.fillStyle = tokens.bg0;
              ctx.globalAlpha = 0.4;
              ctx.fillRect(x, y, cellW, cellH);
              ctx.globalAlpha = 1;
            } else if (k === "uncovered") {
              hatch(ctx, x, y, cellW, cellH, tokens.ink3);
            }
          }
        }
      }
      // Row headers.
      ctx.fillStyle = tokens.ink2;
      ctx.textAlign = "right";
      const step = Math.max(1, Math.ceil(14 / cellH));
      for (let ri = 0; ri < rows.length; ri += step) {
        const row = rows[ri]!;
        const y = top + ri * cellH + cellH / 2;
        ctx.fillText(clip(row.label, Math.floor(headerW / 6)), headerW - 6, y);
      }
      // Column labels (sparse); only when there is horizontal room for them.
      const roomEvery = Math.max(labelEvery, Math.ceil(58 / Math.max(1, cellW)));
      const maxChars = Math.floor((cellW * roomEvery) / 6);
      ctx.fillStyle = tokens.ink3;
      ctx.textAlign = "center";
      if (maxChars >= 3) {
        for (let ci = 0; ci < cols.length; ci += roomEvery) {
          const col = cols[ci]!;
          if (!col.label) continue;
          ctx.fillText(clip(col.label, maxChars), headerW + ci * cellW + cellW / 2, top - 7);
        }
      }
      // now-rule across the grid.
      if (colKind) {
        const now = Date.now();
        for (let ci = 0; ci < cols.length - 1; ci++) {
          const a = cols[ci]!.ms;
          const b = cols[ci + 1]!.ms;
          if (a === undefined || b === undefined) continue;
          if (now >= a && now < b) {
            const frac = (now - a) / Math.max(1, b - a);
            const x = headerW + (ci + frac) * cellW;
            ctx.strokeStyle = tokens.brass;
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, top + gridH);
            ctx.stroke();
            break;
          }
        }
      }
    },
    { width, height },
    [tokens, rows, cols, cell, colKind, top, headerW, cellW, cellH],
  );

  const onMove = (e: React.PointerEvent): void => {
    const g = geom.current;
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - host.left;
    const py = e.clientY - host.top;
    const ci = Math.floor((px - g.headerW) / g.cellW);
    const ri = Math.floor((py - g.top) / g.cellH);
    if (ci < 0 || ri < 0 || ci >= cols.length || ri >= rows.length) {
      setTip(null);
      return;
    }
    const row = rows[ri]!;
    const col = cols[ci]!;
    const cl = cell(row.key, col.key);
    if (!cl) {
      setTip(null);
      return;
    }
    setTip({ x: px, y: py, title: cl.title, rows: cl.rows ?? [] });
  };

  const onClick = (e: React.PointerEvent): void => {
    if (!onPick) return;
    const g = geom.current;
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const ci = Math.floor((e.clientX - host.left - g.headerW) / g.cellW);
    const ri = Math.floor((e.clientY - host.top - g.top) / g.cellH);
    if (ci < 0 || ri < 0 || ci >= cols.length || ri >= rows.length) return;
    const row = rows[ri]!;
    const col = cols[ci]!;
    const cl = cell(row.key, col.key);
    if (cl) onPick(row.key, col.key, cl);
  };

  return (
    <ChartBox width={width} height={height}>
      <TruncationBanner truncated={truncated} />
      <div style={{ position: "relative", width, height: height - bannerH }} onPointerMove={onMove} onPointerLeave={() => setTip(null)} onClick={onClick as unknown as React.MouseEventHandler}>
        <canvas ref={canvasRef} role="img" aria-label={ariaLabel} style={{ cursor: onPick ? "pointer" : "default" }} />
        <ChartTooltip tip={tip} width={width} height={height - bannerH} />
      </div>
      {legend && <Legend items={legend} />}
    </ChartBox>
  );
}

function hatch(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, color: string): void {
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();
  ctx.strokeStyle = color;
  ctx.globalAlpha = 0.4;
  ctx.lineWidth = 1;
  for (let i = -h; i < w; i += 5) {
    ctx.beginPath();
    ctx.moveTo(x + i, y + h);
    ctx.lineTo(x + i + h, y);
    ctx.stroke();
  }
  ctx.restore();
}
