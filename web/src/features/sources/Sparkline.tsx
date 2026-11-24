/*
 * An arrivals sparkline for one source, drawn against the source's OWN cadence
 * band (a p10-p90 range of its weekly history). A dip below the band reads as a
 * genuine slow-down for that source, not against some absolute scale. SVG, both
 * themes, no animation.
 */
import { useMemo, type ReactNode } from "react";
import { cadenceBand } from "./status";
import styles from "./Sources.module.css";

export interface SparklineProps {
  counts: readonly number[];
  color?: string;
  width?: number;
  height?: number;
  ariaLabel?: string;
}

export function Sparkline({
  counts,
  color = "var(--brass)",
  width = 120,
  height = 28,
  ariaLabel,
}: SparklineProps): ReactNode {
  const { path, bandTop, bandBottom, max, last } = useMemo(() => {
    const n = counts.length;
    const peak = Math.max(1, ...counts);
    const band = cadenceBand(counts);
    const x = (i: number): number => (n <= 1 ? 0 : (i / (n - 1)) * width);
    const y = (v: number): number => height - (v / peak) * (height - 2) - 1;
    const d =
      n === 0
        ? ""
        : counts.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    return {
      path: d,
      bandTop: band ? y(band.hi) : null,
      bandBottom: band ? y(band.lo) : null,
      max: peak,
      last: counts[n - 1] ?? 0,
    };
  }, [counts, width, height]);

  if (counts.length === 0) {
    return <span className={styles.sparkEmpty}>no history</span>;
  }

  return (
    <svg
      className={styles.spark}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={ariaLabel ?? `Arrivals history, peak ${max}, latest ${last}`}
    >
      {bandTop !== null && bandBottom !== null ? (
        <rect
          x={0}
          y={Math.min(bandTop, bandBottom)}
          width={width}
          height={Math.max(1, Math.abs(bandBottom - bandTop))}
          fill={color}
          opacity={0.14}
        />
      ) : null}
      <path d={path} fill="none" stroke={color} strokeWidth={1.4} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
