/*
 * The chart layer. Feature authors compose screens from a few entry points:
 *   <ChartPanel spec />      a view spec made live (fetch + lens + controls)
 *   <LensGallery candidates/>a hero + ranked grid, lazily mounted
 *   <SpecView spec rows … />  a lens over rows already in the browser
 *   <LensChart … />           the bare dispatcher (measures, draws, summarises)
 * Everything derives from the lens engine and the design tokens; no channel or
 * plugin name is hard-coded, every chart ships both themes and an a11y summary.
 */
export { ChartPanel } from "./Panel";
export type { ChartPanelProps } from "./Panel";
export { LensGallery, LazyMount } from "./LensGallery";
export type { LensGalleryProps } from "./LensGallery";
export { SpecView } from "./SpecView";
export type { SpecViewProps } from "./SpecView";
export { LensChart } from "./LensChart";
export type { LensChartProps } from "./LensChart";
export { ProvenanceBadge } from "./ProvenanceBadge";
export { rendererFor, RENDERERS } from "./registry";
export type { DrillEvent, BrushRange, Coverage, LensRenderProps, LensRenderer } from "./types";

// Lower-level pieces, for authors building bespoke chart surfaces.
export { useChartColors } from "./primitives/colors";
export { chartTokens, statusTone } from "./runtime/tokens";
export type { ChartTokens } from "./runtime/tokens";
