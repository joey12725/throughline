import { useState, type ReactNode } from "react";
import clsx from "clsx";
import styles from "./Sensitive.module.css";
import { usePrefs } from "@/store/prefs";

export interface SensitiveProps {
  children: ReactNode;
  /** block element instead of inline */
  block?: boolean;
  className?: string;
  /** describe what is hidden, for screen readers when veiled */
  as?: "span" | "div";
}

/**
 * Wraps personal text, names and places. When the privacy veil is on it is
 * blurred (for use in front of other people) and reveals on hover or focus.
 * Charts and counts are never wrapped, so they stay sharp.
 */
export function Sensitive({ children, block, className, as = "span" }: SensitiveProps): ReactNode {
  const veil = usePrefs((s) => s.veil);
  const [revealed, setRevealed] = useState(false);
  const Tag = as;
  if (!veil) {
    return <Tag className={className}>{children}</Tag>;
  }
  return (
    <Tag
      className={clsx(styles.veil, block && styles.block, revealed && styles.revealed, className)}
      tabIndex={0}
      role="button"
      aria-label={revealed ? undefined : "hidden by privacy veil — reveal"}
      onMouseEnter={() => setRevealed(true)}
      onMouseLeave={() => setRevealed(false)}
      onFocus={() => setRevealed(true)}
      onBlur={() => setRevealed(false)}
    >
      {children}
    </Tag>
  );
}
