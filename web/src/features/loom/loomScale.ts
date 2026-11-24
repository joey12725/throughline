/*
 * The Loom's time scale. A whole-life archive is almost always lopsided: a few
 * channels carry a long, sparse past (2016) while ~95% of the records land in
 * the last couple of years. A plain linear axis squeezes everything recent into
 * a sliver at the right edge. So the default is a *focus + context* scale: the
 * span holding the bulk of the records takes most of the width (the focus), and
 * the sparse early years are compressed into a narrow, labelled context band at
 * the left, with a visible break between them. A linear scale is available as a
 * toggle.
 *
 * Both scales are pure, invertible functions of time <-> pixels so brushing and
 * clicking map back to the correct instant under either. Unit-tested in
 * loomScale.test.ts.
 */

export type ScaleMode = "focus" | "linear";

export interface LoomScale {
  mode: ScaleMode;
  /** instant (ms) -> x pixel */
  toX: (ms: number) => number;
  /** x pixel -> instant (ms) */
  toTime: (px: number) => number;
  domainFrom: number;
  domainTo: number;
  plotW: number;
  /** boundary instant between the compressed context and the focus (focus mode only) */
  pivotMs: number | null;
  /** x of the left edge of the focus band (null in linear mode) */
  pivotX: number | null;
  /** pixel width given to the context band (0 in linear mode) */
  contextW: number;
  /** pixel width of the visible break between context and focus */
  breakW: number;
}

export interface ScaleInput {
  domainFrom: number;
  domainTo: number;
  plotW: number;
  mode: ScaleMode;
  /** boundary chosen by choosePivot; ignored in linear mode or when null */
  pivotMs?: number | null;
  /** fraction of the plot width given to the context band (focus mode) */
  contextFraction?: number;
  /** pixel width of the break marker */
  breakPx?: number;
}

const DEFAULT_CONTEXT_FRACTION = 0.15;
const DEFAULT_BREAK_PX = 12;

function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}

export function makeLoomScale(input: ScaleInput): LoomScale {
  const { domainFrom, domainTo, plotW } = input;
  const span = domainTo - domainFrom || 1;
  const usePivot =
    input.mode === "focus" &&
    input.pivotMs != null &&
    input.pivotMs > domainFrom &&
    input.pivotMs < domainTo;

  if (!usePivot) {
    // One linear segment across the whole domain.
    const toX = (ms: number): number => ((ms - domainFrom) / span) * plotW;
    const toTime = (px: number): number => domainFrom + (clamp(px, 0, plotW) / (plotW || 1)) * span;
    return {
      mode: input.mode === "focus" ? "focus" : "linear",
      toX,
      toTime,
      domainFrom,
      domainTo,
      plotW,
      pivotMs: null,
      pivotX: null,
      contextW: 0,
      breakW: 0,
    };
  }

  const pivotMs = input.pivotMs as number;
  const breakW = Math.min(input.breakPx ?? DEFAULT_BREAK_PX, plotW * 0.1);
  const contextW = clamp((input.contextFraction ?? DEFAULT_CONTEXT_FRACTION) * plotW, 18, plotW * 0.4);
  const focusX0 = contextW + breakW;
  const focusW = Math.max(1, plotW - focusX0);
  const ctxSpan = pivotMs - domainFrom || 1;
  const focusSpan = domainTo - pivotMs || 1;

  const toX = (ms: number): number => {
    if (ms < pivotMs) return ((ms - domainFrom) / ctxSpan) * contextW;
    return focusX0 + ((ms - pivotMs) / focusSpan) * focusW;
  };
  const toTime = (px: number): number => {
    const x = clamp(px, 0, plotW);
    if (x < contextW) return domainFrom + (x / (contextW || 1)) * ctxSpan;
    if (x <= focusX0) return pivotMs; // inside the break: snap to the boundary
    return pivotMs + ((x - focusX0) / focusW) * focusSpan;
  };

  return {
    mode: "focus",
    toX,
    toTime,
    domainFrom,
    domainTo,
    plotW,
    pivotMs,
    pivotX: contextW,
    contextW,
    breakW,
  };
}

/**
 * Choose the context/focus boundary: the start of the earliest week such that
 * the records from that week onward make up at least `focusShare` of the total.
 * Everything before becomes the compressed context band. Returns null when there
 * is no meaningful sparse tail to compress (the data already starts dense, or
 * the tail is too short to be worth a band).
 */
export function choosePivot(
  weeks: readonly number[],
  weekTotals: ArrayLike<number>,
  opts: { focusShare?: number; minContextWeeks?: number } = {},
): number | null {
  const n = weeks.length;
  if (n < 2) return null;
  const focusShare = opts.focusShare ?? 0.95;
  const minContextWeeks = opts.minContextWeeks ?? 8;

  let total = 0;
  for (let i = 0; i < n; i++) total += weekTotals[i] ?? 0;
  if (total <= 0) return null;

  // Walk from the end accumulating until we hold focusShare of the records.
  const target = focusShare * total;
  let acc = 0;
  let pivotIdx = 0;
  for (let i = n - 1; i >= 0; i--) {
    acc += weekTotals[i] ?? 0;
    if (acc >= target) {
      pivotIdx = i;
      break;
    }
  }
  // Need a sparse tail long enough to be worth compressing, and a focus that is
  // not the whole chart.
  if (pivotIdx < minContextWeeks) return null;
  if (pivotIdx > n - 2) return null;
  return weeks[pivotIdx] ?? null;
}
