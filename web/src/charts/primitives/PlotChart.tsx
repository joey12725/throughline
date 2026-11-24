/*
 * Composes the common Plot-based chart: the positioned box, the themed Plot
 * figure, an optional time-grammar + brush overlay, a legend for >= 2 series, and
 * the truncation banner. A renderer supplies the Plot options and says whether
 * its x axis is time. One place carries the shared visual system.
 */
import { useState, type ReactNode } from "react";
import { useScopes } from "@/store/scopes";
import type { ChartTokens } from "../runtime/tokens";
import type { LensRenderProps } from "../types";
import { ChartBox, Legend, TruncationBanner, type LegendItem } from "./parts";
import { PlotFigure, type PlotNode } from "./PlotFigure";
import { TimeOverlay } from "./TimeOverlay";

export interface PlotChartProps {
  p: LensRenderProps;
  tokens: ChartTokens;
  /** Build Plot options (recomputed on each change the signature captures). */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  build: () => Record<string, any>;
  signature: string;
  legend?: LegendItem[];
  /** The x axis is a time axis: draw the grammar and allow brushing. */
  timeAxis?: boolean;
  /** Reserve this much height at the bottom for the legend row. */
  legendHeight?: number;
}

export function PlotChart({ p, tokens, build, signature, legend, timeAxis, legendHeight }: PlotChartProps): ReactNode {
  const [plot, setPlot] = useState<PlotNode | null>(null);
  const { from, to } = useScopes();
  const legendH = legend && legend.length >= 2 ? (legendHeight ?? 22) : 0;
  const plotH = Math.max(40, p.height - legendH);
  return (
    <ChartBox width={p.width} height={p.height}>
      <TruncationBanner truncated={p.truncated} />
      <div style={{ position: "relative", width: p.width, height: plotH }}>
        <PlotFigure build={build} signature={`${signature}|${p.width}x${plotH}|${p.theme}`} width={p.width} height={plotH} tokens={tokens} onPlot={setPlot} />
        {timeAxis && (
          <TimeOverlay
            plot={plot}
            width={p.width}
            height={plotH}
            tokens={tokens}
            now={p.now ?? Date.now()}
            coverage={p.coverage}
            hasScope={from !== null || to !== null}
            onBrush={(r) => p.onBrush?.(r)}
          />
        )}
      </div>
      {legend && <Legend items={legend} />}
    </ChartBox>
  );
}
