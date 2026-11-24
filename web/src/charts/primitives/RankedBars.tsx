/*
 * A horizontal ranked-bar chart in hand SVG, so we get direct value labels at the
 * bar end, keyboard focus per bar, and a click that emits a drill event — things
 * Plot does not give cheaply. Used by top_values, tags, dwell_rank, speaker_share
 * and reply_latency. Thin marks, 2px rounded data-ends, one surface gap.
 */
import type { ReactNode } from "react";
import type { ChartTokens } from "../runtime/tokens";
import { clamp } from "../runtime/scales";
import { clip } from "../runtime/format";
import { ChartBox, ChartEmpty, TruncationBanner } from "./parts";

export interface BarItem {
  key: string;
  label: string;
  value: number;
  color: string;
  /** Formatted value shown at the bar end. */
  valueText: string;
  /** Optional secondary note (e.g. characters, visits). */
  sub?: string;
  glyph?: string;
}

export interface RankedBarsProps {
  items: BarItem[];
  width: number;
  height: number;
  tokens: ChartTokens;
  truncated?: boolean;
  /** Label for the axis of what the bars measure (sr-only / tooltip). */
  measureLabel?: string;
  onPick?: (key: string, label: string) => void;
}

export function RankedBars({ items, width, height, tokens, truncated, measureLabel, onPick }: RankedBarsProps): ReactNode {
  if (items.length === 0) return <ChartEmpty message="No values to rank" />;
  const bannerH = truncated ? 26 : 0;
  const rowGap = 2;
  const maxRows = Math.max(1, Math.floor((height - bannerH) / 20));
  const rows = items.slice(0, maxRows);
  const rest = items.length - rows.length;
  const h = height - bannerH;
  const rowH = Math.min(30, (h - (rows.length - 1) * rowGap) / rows.length);
  const hasSub = rows.some((r) => r.sub);
  const subW = hasSub ? 78 : 0;
  const labelW = Math.min(170, Math.max(70, width * 0.3));
  // Reserve enough room for the widest value label (mono ~6.6px/char at 11px) so a
  // full-width bar's value never overruns the right edge or the sub column.
  const longestValue = rows.reduce((m, r) => Math.max(m, r.valueText.length), 0);
  const valueW = Math.min(120, Math.max(44, Math.ceil(longestValue * 6.6) + 12));
  const barX = labelW + 8;
  const barMax = Math.max(10, width - barX - valueW - subW);
  const maxVal = Math.max(...rows.map((r) => r.value), 1);

  return (
    <ChartBox width={width} height={height}>
      <TruncationBanner truncated={truncated} />
      <svg width={width} height={h} role="img" aria-label={measureLabel ? `Ranked by ${measureLabel}` : "Ranked bars"}>
        {rows.map((it, i) => {
          const y = i * (rowH + rowGap);
          const w = clamp((it.value / maxVal) * barMax, it.value > 0 ? 2 : 0, barMax);
          const interactive = !!onPick;
          return (
            <g
              key={it.key}
              transform={`translate(0 ${y})`}
              className={interactive ? "bar-interactive" : undefined}
              role={interactive ? "button" : undefined}
              tabIndex={interactive ? 0 : undefined}
              style={{ cursor: interactive ? "pointer" : "default" }}
              onClick={interactive ? () => onPick!(it.key, it.label) : undefined}
              onKeyDown={interactive ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick!(it.key, it.label); } } : undefined}
            >
              <title>{`${it.label}: ${it.valueText}${it.sub ? ` · ${it.sub}` : ""}`}</title>
              <text x={labelW} y={rowH / 2} textAnchor="end" dominantBaseline="central" fontSize={12} fill={tokens.ink} fontFamily={tokens.fontUi}>
                {clip(it.label || "—", Math.floor(labelW / 7))}
              </text>
              <rect x={barX} y={rowH * 0.18} width={barMax} height={rowH * 0.64} rx={2} fill={tokens.gridSoft} />
              <rect x={barX} y={rowH * 0.18} width={w} height={rowH * 0.64} rx={2} fill={it.color} />
              <text x={barX + w + 6} y={rowH / 2} dominantBaseline="central" fontSize={11} fill={tokens.ink2} fontFamily={tokens.fontMono}>
                {it.valueText}
              </text>
              {it.sub && (
                <text x={width} y={rowH / 2} textAnchor="end" dominantBaseline="central" fontSize={10} fill={tokens.ink3} fontFamily={tokens.fontMono}>
                  {it.sub}
                </text>
              )}
            </g>
          );
        })}
        {rest > 0 && (
          <text x={barX} y={h - 4} fontSize={11} fill={tokens.ink3} fontFamily={tokens.fontUi}>
            +{rest.toLocaleString("en-US")} more
          </text>
        )}
      </svg>
    </ChartBox>
  );
}
