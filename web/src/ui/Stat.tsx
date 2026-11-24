import type { ReactNode } from "react";
import clsx from "clsx";
import styles from "./Stat.module.css";

export interface StatProps {
  label: ReactNode;
  value: ReactNode;
  /** small unit or secondary note after the value */
  unit?: ReactNode;
  /** a sparkline / mini-chart node rendered beneath the value */
  spark?: ReactNode;
  /** optional trailing hint under the label (e.g. "per week") */
  hint?: ReactNode;
  onClick?: () => void;
  className?: string;
  size?: "sm" | "md" | "lg";
}

/** A number with a label and an optional sparkline slot. Links to its query. */
export function Stat({ label, value, unit, spark, hint, onClick, className, size = "md" }: StatProps): ReactNode {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag
      type={onClick ? "button" : undefined}
      className={clsx(styles.stat, styles[size], onClick && styles.clickable, className)}
      onClick={onClick}
    >
      <div className={styles.label}>{label}</div>
      <div className={styles.valueRow}>
        <span className={clsx("tnum", styles.value)}>{value}</span>
        {unit && <span className={styles.unit}>{unit}</span>}
      </div>
      {hint && <div className={styles.hint}>{hint}</div>}
      {spark && <div className={styles.spark}>{spark}</div>}
    </Tag>
  );
}

export interface KeyValueRow {
  key: ReactNode;
  value: ReactNode;
}

/** A definition list for fields, provenance, metadata. */
export function KeyValue({ rows, className }: { rows: ReadonlyArray<KeyValueRow>; className?: string }): ReactNode {
  return (
    <dl className={clsx(styles.kv, className)}>
      {rows.map((r, i) => (
        <div key={i} className={styles.kvRow}>
          <dt className={styles.kvKey}>{r.key}</dt>
          <dd className={styles.kvVal}>{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}
