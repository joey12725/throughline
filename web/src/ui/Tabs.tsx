import { useId, type ReactNode } from "react";
import clsx from "clsx";
import styles from "./Tabs.module.css";

export interface TabItem<T extends string = string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
  count?: number;
}

export interface TabsProps<T extends string = string> {
  items: ReadonlyArray<TabItem<T>>;
  value: T;
  onChange: (value: T) => void;
  className?: string;
  /** underline (default) or enclosed pills */
  look?: "underline" | "enclosed";
}

export function Tabs<T extends string = string>({
  items,
  value,
  onChange,
  className,
  look = "underline",
}: TabsProps<T>): ReactNode {
  const id = useId();
  function onKey(e: React.KeyboardEvent, i: number): void {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const dir = e.key === "ArrowRight" ? 1 : -1;
    const next = (i + dir + items.length) % items.length;
    const item = items[next];
    if (item) onChange(item.value);
    const el = document.getElementById(`${id}-tab-${next}`);
    el?.focus();
  }
  return (
    <div role="tablist" className={clsx(styles.tabs, styles[look], className)}>
      {items.map((it, i) => {
        const selected = it.value === value;
        return (
          <button
            key={it.value}
            id={`${id}-tab-${i}`}
            role="tab"
            type="button"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            className={clsx(styles.tab, selected && styles.selected)}
            onClick={() => onChange(it.value)}
            onKeyDown={(e) => onKey(e, i)}
          >
            {it.icon && <span className={styles.icon} aria-hidden>{it.icon}</span>}
            <span>{it.label}</span>
            {it.count !== undefined && <span className={clsx("tnum", styles.count)}>{it.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

export interface SegmentedProps<T extends string = string> {
  items: ReadonlyArray<{ value: T; label: ReactNode; icon?: ReactNode }>;
  value: T;
  onChange: (value: T) => void;
  "aria-label": string;
  size?: "sm" | "md";
  className?: string;
}

/** A compact one-of-N control (e.g. Loom/River, theme, density). */
export function Segmented<T extends string = string>({
  items,
  value,
  onChange,
  size = "md",
  className,
  ...rest
}: SegmentedProps<T>): ReactNode {
  return (
    <div
      role="radiogroup"
      aria-label={rest["aria-label"]}
      className={clsx(styles.segmented, styles[size], className)}
    >
      {items.map((it) => {
        const selected = it.value === value;
        return (
          <button
            key={it.value}
            role="radio"
            type="button"
            aria-checked={selected}
            className={clsx(styles.segment, selected && styles.segmentOn)}
            onClick={() => onChange(it.value)}
            title={typeof it.label === "string" ? it.label : undefined}
          >
            {it.icon && <span className={styles.icon} aria-hidden>{it.icon}</span>}
            {it.label && <span className={styles.segLabel}>{it.label}</span>}
          </button>
        );
      })}
    </div>
  );
}
