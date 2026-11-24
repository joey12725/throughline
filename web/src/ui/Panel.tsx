import type { ReactNode } from "react";
import clsx from "clsx";
import styles from "./Panel.module.css";

export interface PanelProps {
  title?: ReactNode;
  /** small description under the title */
  subtitle?: ReactNode;
  /** right-aligned controls in the header (buttons, segmented, menu) */
  actions?: ReactNode;
  /** footer row (e.g. "showing 200 of 1,460", truncation banner) */
  footer?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** remove body padding (for charts/tables that bleed to the edge) */
  flush?: boolean;
  /** raise onto the bg-2 surface */
  raised?: boolean;
  as?: "section" | "div" | "article";
}

/**
 * The standard surface: a titled card with header actions and a footer slot.
 * In the full product a panel is a view spec; here it is the chrome every screen
 * composes from. Depth is one background step plus a hairline — no shadows.
 */
export function Panel({
  title,
  subtitle,
  actions,
  footer,
  children,
  className,
  bodyClassName,
  flush,
  raised,
  as = "section",
}: PanelProps): ReactNode {
  const Tag = as;
  return (
    <Tag className={clsx(styles.panel, raised && styles.raised, className)}>
      {(title || actions) && (
        <header className={styles.header}>
          <div className={styles.heading}>
            {title && <h2 className={styles.title}>{title}</h2>}
            {subtitle && <p className={styles.subtitle}>{subtitle}</p>}
          </div>
          {actions && <div className={styles.actions}>{actions}</div>}
        </header>
      )}
      <div className={clsx(styles.body, flush && styles.flush, bodyClassName)}>{children}</div>
      {footer && <footer className={styles.footer}>{footer}</footer>}
    </Tag>
  );
}
