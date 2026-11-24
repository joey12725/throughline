// The lens catalogue: one module per lens of the design's table.

import type { LensGroup, LensId } from "../types";
import { LENS_IDS } from "../types";
import { calendarHeat } from "./calendar_heat";
import type { LensDef } from "./common";
import { composition } from "./composition";
import { dwellRank } from "./dwell_rank";
import { egoGraph } from "./ego_graph";
import { gantt } from "./gantt";
import { histogram } from "./histogram";
import { intervalClock } from "./interval_clock";
import { jsonTree } from "./json_tree";
import { linkMatrix } from "./link_matrix";
import { pinMap } from "./pin_map";
import { reader } from "./reader";
import { replyLatency } from "./reply_latency";
import { sawtooth } from "./sawtooth";
import { scatter } from "./scatter";
import { smallMultiples } from "./small_multiples";
import { speakerShare } from "./speaker_share";
import { spine } from "./spine";
import { stat } from "./stat";
import { statusMatrix } from "./status_matrix";
import { strip } from "./strip";
import { table } from "./table";
import { tags } from "./tags";
import { threadTapestry } from "./thread_tapestry";
import { timeseries } from "./timeseries";
import { topValues } from "./top_values";
import { trackMap } from "./track_map";
import { weekdayHour } from "./weekday_hour";

export const LENSES: Record<LensId, LensDef> = {
  spine,
  timeseries,
  small_multiples: smallMultiples,
  calendar_heat: calendarHeat,
  weekday_hour: weekdayHour,
  interval_clock: intervalClock,
  gantt,
  sawtooth,
  histogram,
  top_values: topValues,
  composition,
  scatter,
  strip,
  thread_tapestry: threadTapestry,
  speaker_share: speakerShare,
  reply_latency: replyLatency,
  status_matrix: statusMatrix,
  tags,
  json_tree: jsonTree,
  track_map: trackMap,
  pin_map: pinMap,
  dwell_rank: dwellRank,
  ego_graph: egoGraph,
  link_matrix: linkMatrix,
  reader,
  table,
  stat,
};

export const LENS_LIST: LensDef[] = LENS_IDS.map((id) => LENSES[id]);

export interface LensCatalogEntry {
  id: LensId;
  group: LensGroup;
  summary: string;
  needs: string[];
  /** Encode channel -> what it binds. */
  channels: Record<string, string>;
  /** Whether the lens can draw an arbitrary SQL result. */
  results: boolean;
}

/** What each lens is and which encode channels it reads: for renderers, the picker and the helper. */
export const LENS_CATALOG: LensCatalogEntry[] = LENS_LIST.map((l) => ({
  id: l.id,
  group: l.group,
  summary: l.summary,
  needs: l.needs,
  channels: l.channels,
  results: typeof l.buildResult === "function",
}));

export type { ChannelCtx, LensDef, ResultCtx } from "./common";
export { readerVariant, type ReaderVariant } from "./reader";
export { REPLY_BINS } from "./reply_latency";
