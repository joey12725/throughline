/*
 * A thin React wrapper over Observable Plot. Plot owns axes, scales, legends,
 * facets and its own hover tip; we own the theme (every colour passed in, both
 * themes) and expose the rendered plot so a time-brush overlay can read its x
 * scale. The figure background is transparent; the surrounding Panel owns depth.
 */
import { useEffect, useRef, type ReactNode } from "react";
import * as Plot from "@observablehq/plot";
import type { ChartTokens } from "../runtime/tokens";

export interface PlotNode extends HTMLElement {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  scale(name: string): any;
}

export interface PlotFigureProps {
  /** Build a Plot.plot() options object; style/width/height are added for you. */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  build: () => Record<string, any>;
  /** A string that changes exactly when the plot should be rebuilt. */
  signature: string;
  width: number;
  height: number;
  tokens: ChartTokens;
  /** Receives the rendered plot node (for scale reads / brush), and a cleanup. */
  onPlot?: (node: PlotNode | null) => void;
  className?: string;
}

export function PlotFigure({ build, signature, width, height, tokens, onPlot, className }: PlotFigureProps): ReactNode {
  const host = useRef<HTMLDivElement>(null);
  const buildRef = useRef(build);
  buildRef.current = build;
  const onPlotRef = useRef(onPlot);
  onPlotRef.current = onPlot;
  useEffect(() => {
    const el = host.current;
    if (!el || width <= 0 || height <= 0) return;
    const node = Plot.plot({
      width,
      height,
      style: {
        background: "transparent",
        color: tokens.ink2,
        fontFamily: tokens.fontUi,
        fontSize: "11px",
        overflow: "visible",
      },
      ...buildRef.current(),
    }) as unknown as PlotNode;
    el.replaceChildren(node);
    onPlotRef.current?.(node);
    return () => {
      onPlotRef.current?.(null);
      node.remove?.();
    };
  }, [signature, width, height, tokens.ink]);
  return <div ref={host} className={className} style={{ width, height }} />;
}
