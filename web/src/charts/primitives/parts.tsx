/*
 * Small shared chart parts: the positioned box every renderer draws into, a
 * direct/legend swatch row, an empty state, and the truncation / sampling banner
 * that rides onto any chart drawn from a cut-off result.
 */
import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import styles from "./parts.module.css";

export function ChartBox({ width, height, children, className }: { width: number; height: number; children: ReactNode; className?: string }): ReactNode {
  return (
    <div className={className ? `${styles.box} ${className}` : styles.box} style={{ width, height }}>
      {children}
    </div>
  );
}

export interface LegendItem {
  label: string;
  color: string;
  glyph?: string;
}

/** A legend for >= 2 series. Identity is never colour alone: a swatch + a label. */
export function Legend({ items, max = 10 }: { items: LegendItem[]; max?: number }): ReactNode {
  if (items.length < 2) return null;
  const shown = items.slice(0, max);
  const rest = items.length - shown.length;
  return (
    <div className={styles.legend} role="list">
      {shown.map((it) => (
        <span key={it.label} className={styles.legendItem} role="listitem">
          <span className={styles.legendSwatch} style={{ background: it.color }} aria-hidden>
            {it.glyph}
          </span>
          <span className={styles.legendLabel}>{it.label}</span>
        </span>
      ))}
      {rest > 0 && <span className={styles.legendMore}>+{rest} more</span>}
    </div>
  );
}

export function ChartEmpty({ message = "Nothing to draw", hint }: { message?: string; hint?: string }): ReactNode {
  return (
    <div className={styles.empty}>
      <div className={styles.emptyMark} aria-hidden />
      <p className={styles.emptyMsg}>{message}</p>
      {hint && <p className={styles.emptyHint}>{hint}</p>}
    </div>
  );
}

/** Rides onto any chart drawn from a truncated or sampled result. */
export function TruncationBanner({ truncated, sampled, rowCount, rowCap }: { truncated?: boolean; sampled?: boolean; rowCount?: number; rowCap?: number }): ReactNode {
  if (!truncated && !sampled) return null;
  const text = truncated
    ? `Cut off at the row cap${rowCap ? ` (${rowCap.toLocaleString("en-US")})` : ""}: this picture shows only what came back${rowCount ? `, ${rowCount.toLocaleString("en-US")} rows` : ""}.`
    : "Value statistics were sampled; counts stay exact.";
  return (
    <div className={styles.banner} role="status">
      <AlertTriangle size={13} aria-hidden />
      <span>{text}</span>
    </div>
  );
}
