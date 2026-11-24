import type { CSSProperties, ReactNode } from "react";
import clsx from "clsx";
import styles from "./Page.module.css";

export interface PageProps {
  children: ReactNode;
  /** Content width: full-bleed (default), a wide cap (~1200px) or a readable column (~860px). */
  width?: "full" | "wide" | "readable";
  /** Remove the standard vertical rhythm between direct children (feature lays it out itself). */
  flush?: boolean;
  className?: string;
  style?: CSSProperties;
}

/**
 * The shared screen frame: one page gutter and one vertical rhythm for every
 * feature, so the app reads as a single surface. Narrows to a phone gutter and
 * clears the bottom tab bar automatically. Compose a `PageHeader` as the first
 * child for a consistent title treatment.
 */
export function Page({ children, width = "full", flush, className, style }: PageProps): ReactNode {
  return (
    <div className={clsx(styles.page, styles[width], flush && styles.flush, className)} style={style}>
      {children}
    </div>
  );
}

export interface PageHeaderProps {
  title: ReactNode;
  /** One editorial line under the title. */
  lede?: ReactNode;
  /** Right-aligned controls (buttons, segmented…). Wrap below the title on phones. */
  actions?: ReactNode;
  /** A small element above the title, e.g. a back link or eyebrow. */
  eyebrow?: ReactNode;
  className?: string;
}

/** The standard page title block: eyebrow, h1, lede and an actions slot. */
export function PageHeader({ title, lede, actions, eyebrow, className }: PageHeaderProps): ReactNode {
  return (
    <header className={clsx(styles.header, className)}>
      <div className={styles.headingCol}>
        {eyebrow && <div className={styles.eyebrow}>{eyebrow}</div>}
        <h1 className={styles.h1}>{title}</h1>
        {lede && <p className={styles.lede}>{lede}</p>}
      </div>
      {actions && <div className={styles.actions}>{actions}</div>}
    </header>
  );
}
