import type { ReactNode } from "react";
import clsx from "clsx";
import styles from "./Source.module.css";
import { useSourceColor } from "./SourceColorContext";

export interface SourceDotProps {
  plugin: string;
  /** channel index within the plugin, for the lightness step */
  channelIndex?: number;
  size?: number;
  /** hollow ring instead of a filled dot */
  ring?: boolean;
  className?: string;
}

/** A plugin/channel colour dot. Colour derives entirely from the assignment. */
export function SourceDot({ plugin, channelIndex = 0, size = 9, ring, className }: SourceDotProps): ReactNode {
  const { cssFor } = useSourceColor();
  const color = cssFor(plugin, channelIndex);
  return (
    <span
      className={clsx(styles.dot, ring && styles.ring, className)}
      style={{ width: size, height: size, ["--dot" as string]: color }}
      aria-hidden
    />
  );
}

export interface SourceChipProps {
  /** channel name (shown) */
  channel?: string;
  /** plugin name (owns the hue) */
  plugin: string;
  channelIndex?: number;
  /** override the visible label (defaults to channel ?? plugin) */
  label?: string;
  className?: string;
  onClick?: () => void;
}

/** A labelled source token: dot + name, the name tinted by the source colour. */
export function SourceChip({
  channel,
  plugin,
  channelIndex = 0,
  label,
  className,
  onClick,
}: SourceChipProps): ReactNode {
  const { cssFor } = useSourceColor();
  const color = cssFor(plugin, channelIndex);
  const text = label ?? channel ?? plugin;
  const Tag = onClick ? "button" : "span";
  return (
    <Tag
      type={onClick ? "button" : undefined}
      className={clsx(styles.chip, onClick && styles.interactive, className)}
      style={{ ["--src" as string]: color }}
      onClick={onClick}
    >
      <span className={styles.chipDot} aria-hidden />
      <span className={styles.chipText}>{text}</span>
    </Tag>
  );
}
