/*
 * Chart layer types. A renderer is a React component that, given a resolved view
 * spec, the rows and columns a panel fetched, and a pixel size, draws one lens.
 * Nothing here fetches: Panel owns the scheduler call and hands rows down.
 */
import type { LensId, Row, SqlColumn, ViewSpec, RoleMap } from "@/lens";
import type { ThemeName } from "@/ui/sourceColor";

/** A click on a mark. Hosts route it: open a drawer, filter, or navigate. */
export type DrillEvent =
  | { kind: "record"; id: string | number; channel?: string }
  | { kind: "bucket"; from: string; to: string; label?: string }
  | { kind: "category"; field?: string; value: string }
  | { kind: "thread"; id: string | number; label?: string }
  | { kind: "person"; id: string | number; label?: string }
  | { kind: "place"; label: string; lat?: number; lon?: number };

/** A time brush result, in ISO instants. null clears the scope. */
export interface BrushRange {
  from: string | null;
  to: string | null;
}

/** Coverage windows a channel claims, so a time axis can hatch what is uncovered. */
export interface Coverage {
  from: string | null;
  to: string | null;
}

/** Everything a lens renderer is handed. Pure with respect to the network. */
export interface LensRenderProps {
  spec: ViewSpec;
  rows: readonly Row[];
  columns: readonly SqlColumn[];
  width: number;
  height: number;
  /** The result was cut off at the row cap; charts drawn from it carry a banner. */
  truncated: boolean;
  theme: ThemeName;
  /** Plugin that owns the data, for source colour; falls back to a neutral hue. */
  plugin?: string;
  /** The grain :grain resolved to, when the statement used one. */
  grain?: string | null;
  /** Inferred roles, when known: lets cells render by role (reader/table). */
  roles?: RoleMap;
  /** Coverage windows for the time grammar (future/uncovered hatching). */
  coverage?: Coverage | null;
  /** "now" for the time grammar; defaults to Date.now(). */
  now?: number;
  /** Timezone for day/weekday/hour bucketing and axis labels. */
  tz?: string;
  onDrill?: (e: DrillEvent) => void;
  /** A brush on a time axis sets the global time scope. */
  onBrush?: (range: BrushRange) => void;
  /** The canvas-ready rgb() resolver from the source-colour provider. */
  rgbFor?: (plugin: string, channelIndex?: number, alpha?: number) => string;
  /** oklch() css resolver from the source-colour provider. */
  cssFor?: (plugin: string, channelIndex?: number, alpha?: number) => string;
}

export type LensRenderer = (props: LensRenderProps) => React.ReactNode;

export interface RendererEntry {
  id: LensId;
  /** SVG/Plot, hand-canvas, a React component, a lazy map, or a lazy graph. */
  kind: "svg" | "canvas" | "react" | "map" | "graph";
  render: LensRenderer;
}
