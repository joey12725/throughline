/*
 * The dispatcher. Measures its box, picks the renderer for the spec's lens, and
 * wraps it in a Suspense (for the lazy map/graph chunks), an error boundary and
 * an accessible text summary derived from the lens engine's own narrator
 * (describeResult). Every chart therefore carries a true sentence for a screen
 * reader, and a renderer that throws degrades to an error card, not a blank page.
 */
import { Suspense, useMemo, type ReactNode } from "react";
import { profileResult, describeResult } from "@/lens";
import { Skeleton } from "@/ui";
import { useScopes } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import { useSourceColor } from "@/ui";
import { useElementSize } from "./runtime/hooks";
import { rendererFor } from "./registry";
import { ChartErrorBoundary } from "./ChartErrorBoundary";
import type { LensRenderProps } from "./types";
import styles from "./LensChart.module.css";

export type LensChartProps = Omit<LensRenderProps, "width" | "height" | "theme" | "rgbFor" | "cssFor" | "onBrush" | "now" | "tz"> & {
  /** Fixed height, or the box fills its flex parent. */
  height?: number;
  minHeight?: number;
};

export function LensChart(props: LensChartProps): ReactNode {
  const { spec, rows, columns, truncated, height, minHeight = 160, ...rest } = props;
  const [ref, size] = useElementSize<HTMLDivElement>();
  const { theme, rgbFor, cssFor } = useSourceColor();
  const { setRange } = useScopes();
  const tz = usePrefs((s) => s.tz);
  const Renderer = rendererFor(spec.lens);

  const summary = useMemo(() => {
    try {
      const prof = profileResult(rows.slice(0, 2000) as never, columns as never, truncated);
      return describeResult(prof).join(" ");
    } catch {
      return `${spec.title}: ${rows.length} rows.`;
    }
  }, [rows, columns, truncated, spec.title]);

  const h = height ?? Math.max(minHeight, size.height || minHeight);
  const full: LensRenderProps = {
    ...rest,
    spec,
    rows,
    columns,
    truncated,
    theme,
    tz,
    now: Date.now(),
    width: size.width || 0,
    height: h,
    rgbFor,
    cssFor,
    onBrush: (r) => setRange(r.from, r.to),
  };

  return (
    <figure ref={ref} className={styles.figure} style={{ height: height ?? "100%", minHeight }} aria-label={spec.title}>
      <figcaption className="sr-only">{summary}</figcaption>
      <ChartErrorBoundary lens={spec.lens}>
        <Suspense fallback={<Skeleton height="100%" radius={8} />}>
          {size.width > 0 ? <Renderer {...full} /> : null}
        </Suspense>
      </ChartErrorBoundary>
    </figure>
  );
}
