import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import clsx from "clsx";
import { X } from "lucide-react";
import styles from "./Chip.module.css";

export interface ChipProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** render as a toggle; when active it carries the brass cast */
  active?: boolean;
  /** leading node (e.g. a SourceDot) */
  lead?: ReactNode;
  /** show a remove affordance; onRemove fires instead of onClick when hit */
  onRemove?: () => void;
  children: ReactNode;
}

/**
 * A small interactive token: a filter, a facet, a selected scope. When not
 * interactive, pass `as="span"` via the static Chip.Static below.
 */
export const Chip = forwardRef<HTMLButtonElement, ChipProps>(function Chip(
  { active, lead, onRemove, className, children, type, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      className={clsx(styles.chip, active && styles.active, className)}
      aria-pressed={rest.onClick ? active : undefined}
      {...rest}
    >
      {lead && <span className={styles.lead}>{lead}</span>}
      <span className={styles.text}>{children}</span>
      {onRemove && (
        <span
          className={styles.remove}
          role="button"
          tabIndex={-1}
          aria-label="remove"
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
        >
          <X size={12} strokeWidth={2.5} />
        </span>
      )}
    </button>
  );
});

export function StaticChip({
  lead,
  children,
  className,
}: {
  lead?: ReactNode;
  children: ReactNode;
  className?: string;
}): ReactNode {
  return (
    <span className={clsx(styles.chip, styles.static, className)}>
      {lead && <span className={styles.lead}>{lead}</span>}
      <span className={styles.text}>{children}</span>
    </span>
  );
}
