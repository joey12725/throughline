import type { ReactNode } from "react";

/** A tiny trend sparkline (inline SVG, no axes) for dense list rows. */
export function Sparkline({
  values,
  width = 76,
  height = 18,
  color = "var(--brass)",
}: {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
}): ReactNode {
  if (!values || values.length === 0) {
    return <svg width={width} height={height} aria-hidden="true" />;
  }
  const max = Math.max(1, ...values);
  const n = values.length;
  const step = n > 1 ? width / (n - 1) : 0;
  const pts = values.map((v, i) => {
    const x = n > 1 ? i * step : width / 2;
    const y = height - 1 - (v / max) * (height - 2);
    return [x, y] as const;
  });
  const path = pts.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${path} L${width},${height} L0,${height} Z`;
  const last = pts[pts.length - 1];
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true" style={{ overflow: "visible" }}>
      <path d={area} fill={color} opacity={0.12} />
      <path d={path} fill="none" stroke={color} strokeWidth={1.25} strokeLinejoin="round" strokeLinecap="round" />
      {last && <circle cx={last[0]} cy={last[1]} r={1.6} fill={color} />}
    </svg>
  );
}
