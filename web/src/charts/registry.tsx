/*
 * The renderer registry: every lens id in the engine maps to a renderer. SVG /
 * Plot and hand-canvas renderers are eager (one chunk with the charts); the two
 * heavy, rarely-first lenses — maps (MapLibre) and the graph (sigma) — are their
 * own lazy chunks so a channel that never shows them never pays for them.
 */
import { lazy } from "react";
import type { LensId } from "@/lens";
import type { LensRenderer } from "./types";
import { CompositionChart, GanttChart, SawtoothChart, SmallMultiplesChart, SpineChart, TimeseriesChart } from "./lenses/timeLenses";
import { DwellRankChart, HistogramChart, ReplyLatencyChart, ScatterChart, SpeakerShareChart, TagsChart, TopValuesChart } from "./lenses/distributionLenses";
import { CalendarHeatChart, IntervalClockChart, LinkMatrixChart, StatusMatrixChart, StripChart, ThreadTapestryChart, WeekdayHourChart } from "./lenses/gridLenses";
import { JsonTreeChart, ReaderChart, StatChart, TableChart } from "./lenses/readingLenses";

const MapLens = lazy(() => import("./lenses/mapLenses"));
const EgoGraph = lazy(() => import("./lenses/graphLenses"));

export const RENDERERS: Record<LensId, LensRenderer> = {
  // time
  spine: SpineChart,
  timeseries: TimeseriesChart,
  small_multiples: SmallMultiplesChart,
  calendar_heat: CalendarHeatChart,
  weekday_hour: WeekdayHourChart,
  interval_clock: IntervalClockChart,
  gantt: GanttChart,
  sawtooth: SawtoothChart,
  // distribution
  histogram: HistogramChart,
  top_values: TopValuesChart,
  composition: CompositionChart,
  scatter: ScatterChart,
  strip: StripChart,
  // structure
  thread_tapestry: ThreadTapestryChart,
  speaker_share: SpeakerShareChart,
  reply_latency: ReplyLatencyChart,
  status_matrix: StatusMatrixChart,
  tags: TagsChart,
  json_tree: JsonTreeChart,
  // space
  track_map: (props) => <MapLens {...props} />,
  pin_map: (props) => <MapLens {...props} />,
  dwell_rank: DwellRankChart,
  // relation
  ego_graph: (props) => <EgoGraph {...props} />,
  link_matrix: LinkMatrixChart,
  // reading
  reader: ReaderChart,
  table: TableChart,
  stat: StatChart,
};

export function rendererFor(lens: LensId): LensRenderer {
  return RENDERERS[lens] ?? TableChart;
}
