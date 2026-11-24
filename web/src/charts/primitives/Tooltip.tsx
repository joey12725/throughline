/*
 * A chart tooltip: a small raised card positioned near the pointer, inside the
 * chart's own relatively-positioned container. Exact values live here; the chart
 * surface carries only the mark. Monochrome chrome; the swatch carries identity.
 */
import { useCallback, useState, type ReactNode } from "react";
import styles from "./Tooltip.module.css";

export interface TipRow {
  swatch?: string;
  label: ReactNode;
  value?: ReactNode;
}

export interface TipState {
  x: number;
  y: number;
  title?: ReactNode;
  rows: TipRow[];
}

export interface TooltipHandle {
  tip: TipState | null;
  show: (tip: TipState) => void;
  hide: () => void;
}

export function useTooltip(): TooltipHandle {
  const [tip, setTip] = useState<TipState | null>(null);
  const show = useCallback((t: TipState) => setTip(t), []);
  const hide = useCallback(() => setTip(null), []);
  return { tip, show, hide };
}

/** Renders the tooltip inside a chart container. Container must be position:relative. */
export function ChartTooltip({ tip, width, height }: { tip: TipState | null; width: number; height: number }): ReactNode {
  if (!tip) return null;
  // Flip to keep the card on-screen.
  const flipX = tip.x > width - 180;
  const flipY = tip.y > height - 80;
  const style: React.CSSProperties = {
    left: flipX ? undefined : tip.x + 12,
    right: flipX ? Math.max(0, width - tip.x + 12) : undefined,
    top: flipY ? undefined : tip.y + 12,
    bottom: flipY ? Math.max(0, height - tip.y + 12) : undefined,
  };
  return (
    <div className={styles.tip} style={style} role="tooltip">
      {tip.title != null && <div className={styles.title}>{tip.title}</div>}
      {tip.rows.map((r, i) => (
        <div key={i} className={styles.row}>
          {r.swatch !== undefined && <span className={styles.swatch} style={{ background: r.swatch }} />}
          <span className={styles.label}>{r.label}</span>
          {r.value !== undefined && <span className={styles.value}>{r.value}</span>}
        </div>
      ))}
    </div>
  );
}
