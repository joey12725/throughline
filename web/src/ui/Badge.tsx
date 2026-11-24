import type { ReactNode } from "react";
import clsx from "clsx";
import styles from "./Badge.module.css";
import { STATE, type SourceState, type Tone } from "./status";

export interface BadgeProps {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}

/** A small label pill. Tone maps to a status colour but never a source hue. */
export function Badge({ tone = "neutral", children, className }: BadgeProps): ReactNode {
  return <span className={clsx(styles.badge, styles[tone], className)}>{children}</span>;
}

export interface StatusGlyphProps {
  state: SourceState;
  /** show the word beside the glyph (default true) */
  label?: boolean;
  className?: string;
  size?: number;
}

/**
 * A source/plugin state: glyph + word + status colour. Glyph and word are always
 * present so it reads without colour (veil, colour-blindness).
 */
export function StatusGlyph({ state, label = true, className, size = 14 }: StatusGlyphProps): ReactNode {
  const d = STATE[state];
  const Glyph = d.glyph;
  return (
    <span className={clsx(styles.status, styles[d.tone], className)} role="status">
      <Glyph size={size} strokeWidth={2.25} aria-hidden />
      {label && <span className={styles.word}>{d.word}</span>}
      {!label && <span className="sr-only">{d.word}</span>}
    </span>
  );
}
