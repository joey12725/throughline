/*
 * Time lenses drawn with Observable Plot: spine, timeseries, small_multiples,
 * composition, sawtooth, gantt. Each gets the shared time-grammar + brush overlay
 * through PlotChart. Colour: a single series uses the source hue; several series
 * use the categorical ramp (identity) or, for ordered family members, sequential.
 */
import * as Plot from "@observablehq/plot";
import type { ReactNode } from "react";
import { colN, col1, numAt, timeMsAt, strAt, unitOf, optStr } from "../runtime/accessors";
import { compactNumber, formatMeasure, humanLabel } from "../runtime/format";
import { useChartColors } from "../primitives/colors";
import { PlotChart } from "../primitives/PlotChart";
import { ChartEmpty, type LegendItem } from "../primitives/parts";
import type { LensRenderProps } from "../types";

function sig(p: LensRenderProps, extra = ""): string {
  return `${p.spec.lens}|${p.rows.length}|${JSON.stringify(p.spec.encode)}|${p.truncated}|${extra}`;
}

/** Drop the shared leading tokens of sibling field names so facet labels read short. */
function shortNames(names: string[]): string[] {
  if (names.length < 2) return names.map(humanLabel);
  const split = names.map((n) => n.split("_"));
  let common = 0;
  const first = split[0]!;
  outer: for (; common < first.length - 1; common++) {
    const tok = first[common];
    for (const s of split) if (s[common] !== tok || common >= s.length - 1) break outer;
  }
  return split.map((s) => humanLabel(s.slice(common).join("_")));
}

// marginTop leaves room for a top-anchored y-axis label so it never collides with
// the top tick value (e.g. a "records" label overrunning a "500" tick).
const COMMON = { marginTop: 22, marginRight: 14, marginBottom: 26, marginLeft: 46 };

/** Centred N-point moving average over an ordered series; skips NaN samples. */
function rollingMean(data: { t: Date; y: number }[], win: number): { t: Date; y: number }[] {
  const half = Math.floor(win / 2);
  const out: { t: Date; y: number }[] = [];
  for (let i = 0; i < data.length; i++) {
    const lo = Math.max(0, i - half);
    const hi = Math.min(data.length, i + (win - half));
    let sum = 0;
    let k = 0;
    for (let j = lo; j < hi; j++) {
      const v = data[j]!.y;
      if (Number.isFinite(v)) {
        sum += v;
        k++;
      }
    }
    if (k > 0) out.push({ t: data[i]!.t, y: sum / k });
  }
  return out;
}

/* ---- spine ---------------------------------------------------------------- */

export function SpineChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const xCol = col1(p.spec, "x");
  const yCol = col1(p.spec, "y");
  const data = p.rows
    .map((r) => ({ t: new Date(timeMsAt(r, xCol)), n: numAt(r, yCol) }))
    .filter((d) => !Number.isNaN(d.t.getTime()) && Number.isFinite(d.n));
  if (data.length === 0) return <ChartEmpty message="No records in range" />;
  const stroke = c.source(0);

  // Coverage mode (fixed-cadence channels): a thin presence ribbon with gaps,
  // rather than a count band that is always ~1. Each present period is a tick;
  // a missing period reads as a gap in the ribbon.
  if (optStr(p.spec, "mode") === "coverage") {
    const ribbon = data.map((d) => ({ t: d.t, v: 0.5 }));
    return (
      <PlotChart
        p={p}
        tokens={c.tokens}
        timeAxis
        signature={sig(p, "coverage")}
        build={() => ({
          ...COMMON,
          marginLeft: 20,
          x: { type: "utc", label: null, tickSize: 0, ticks: 6 },
          y: { domain: [0, 1], axis: null },
          marks: [
            Plot.ruleX(ribbon, { x: "t", y1: 0.32, y2: 0.68, stroke, strokeWidth: 1.4, strokeOpacity: 0.9 }),
            Plot.tip(
              ribbon,
              Plot.pointerX({
                x: "t",
                y: "v",
                fill: c.tokens.bg2,
                stroke: c.tokens.lineStrong,
                title: (d: { t: Date }) => `${d.t.toISOString().slice(0, 10)}\npresent`,
              }),
            ),
          ],
        })}
      />
    );
  }

  const fill = c.source(0, 0.16);
  return (
    <PlotChart
      p={p}
      tokens={c.tokens}
      timeAxis
      signature={sig(p)}
      build={() => ({
        ...COMMON,
        x: { type: "utc", label: null, tickSize: 0, ticks: 6 },
        y: { label: null, tickSize: 0, ticks: 4, tickFormat: compactNumber, insetTop: 4 },
        marks: [
          Plot.areaY(data, { x: "t", y: "n", fill, curve: "monotone-x" }),
          Plot.lineY(data, { x: "t", y: "n", stroke, strokeWidth: 1.5, curve: "monotone-x" }),
          Plot.ruleY([0], { stroke: c.tokens.line }),
          Plot.tip(data, Plot.pointerX({ x: "t", y: "n", fill: c.tokens.bg2, stroke: c.tokens.lineStrong })),
        ],
      })}
    />
  );
}

/* ---- timeseries ----------------------------------------------------------- */

export function TimeseriesChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const xCol = col1(p.spec, "x");
  const yCols = colN(p.spec, "y");
  const seriesCol = col1(p.spec, "series");
  const loCol = col1(p.spec, "lo");
  const hiCol = col1(p.spec, "hi");
  const agg = optStr(p.spec, "agg");
  const stacked = p.spec.options?.stacked === true;

  // Case A: one measure, optionally with a lo/hi band.
  if (!seriesCol && yCols.length === 1) {
    const yCol = yCols[0]!;
    const unit = unitOf(p.spec, yCol);
    const data = p.rows
      .map((r) => ({ t: new Date(timeMsAt(r, xCol)), y: numAt(r, yCol), lo: loCol ? numAt(r, loCol) : NaN, hi: hiCol ? numAt(r, hiCol) : NaN }))
      .filter((d) => !Number.isNaN(d.t.getTime()));
    if (data.length === 0) return <ChartEmpty message="No values in range" />;
    const stroke = c.source(0);
    // Rolling mean: when the engine marks a noisy per-day measure, draw a centred
    // N-point moving average bold over the raw series held faint beneath.
    const win = Math.round(Number(p.spec.options?.rolling) || 0);
    const rolling = win >= 2 && data.length > win * 2 ? rollingMean(data, win) : null;
    return (
      <PlotChart
        p={p}
        tokens={c.tokens}
        timeAxis
        signature={sig(p, `${yCol}|${win}`)}
        build={() => ({
          ...COMMON,
          x: { type: "utc", label: null, tickSize: 0, ticks: 6 },
          y: { label: humanLabel(yCol), labelAnchor: "top", tickSize: 0, ticks: 4, tickFormat: (v: number) => formatMeasure(v, unit) },
          marks: [
            loCol && hiCol ? Plot.areaY(data, { x: "t", y1: "lo", y2: "hi", fill: c.source(0, 0.14), curve: "monotone-x" }) : null,
            rolling
              ? Plot.dot(data, { x: "t", y: "y", r: 1.1, fill: stroke, fillOpacity: 0.28 })
              : null,
            rolling
              ? Plot.lineY(data, { x: "t", y: "y", stroke, strokeWidth: 1, strokeOpacity: 0.22, curve: "monotone-x" })
              : Plot.lineY(data, { x: "t", y: "y", stroke, strokeWidth: 1.5, curve: "monotone-x", marker: data.length <= 40 ? "circle" : undefined }),
            rolling ? Plot.lineY(rolling, { x: "t", y: "y", stroke, strokeWidth: 1.9, curve: "monotone-x" }) : null,
            Plot.tip(data, Plot.pointerX({ x: "t", y: "y", fill: c.tokens.bg2, stroke: c.tokens.lineStrong, title: (d: { t: Date; y: number }) => `${d.t.toISOString().slice(0, 10)}\n${formatMeasure(d.y, unit)}` })),
          ].filter(Boolean),
        })}
      />
    );
  }

  // Case B: a count (or value) per category, as stacked/overlaid series.
  const yCol = yCols[0] ?? "n";
  const data = p.rows
    .map((r) => ({ t: new Date(timeMsAt(r, xCol)), y: numAt(r, yCol), s: seriesCol ? strAt(r, seriesCol) : humanLabel(yCol) }))
    .filter((d) => !Number.isNaN(d.t.getTime()));
  if (data.length === 0) return <ChartEmpty message="No values in range" />;
  const cats = [...new Set(data.map((d) => d.s))].slice(0, 12);
  const palette = c.categorical(cats.length);
  const colorFor = new Map(cats.map((s, i) => [s, palette[i % palette.length]!]));
  const legend: LegendItem[] = cats.map((s, i) => ({ label: s || "—", color: palette[i % palette.length]! }));
  return (
    <PlotChart
      p={p}
      tokens={c.tokens}
      timeAxis
      legend={legend}
      signature={sig(p, cats.join(","))}
      build={() => ({
        ...COMMON,
        x: { type: "utc", label: null, tickSize: 0, ticks: 6 },
        y: { label: agg === "count" ? "records" : humanLabel(yCol), labelAnchor: "top", tickSize: 0, ticks: 4, tickFormat: compactNumber },
        color: { domain: cats, range: cats.map((s) => colorFor.get(s)!) },
        marks: [
          stacked
            ? Plot.areaY(data, { x: "t", y: "y", z: "s", fill: "s", fillOpacity: 0.85, curve: "monotone-x" })
            : Plot.lineY(data, { x: "t", y: "y", z: "s", stroke: "s", strokeWidth: 1.5, curve: "monotone-x" }),
          Plot.ruleY([0], { stroke: c.tokens.line }),
          Plot.tip(data, Plot.pointerX({ x: "t", y: "y", z: "s", channels: { series: "s" }, fill: c.tokens.bg2, stroke: c.tokens.lineStrong })),
        ],
      })}
    />
  );
}

/* ---- small multiples ------------------------------------------------------ */

export function SmallMultiplesChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const xCol = col1(p.spec, "x");
  const yCols = colN(p.spec, "y");
  const facetCol = col1(p.spec, "facet");

  // Wide form: one row per bucket, several measure columns -> one facet each.
  if (!facetCol && yCols.length >= 1) {
    const cap = Math.max(2, Math.floor(p.height / 26));
    const cols = yCols.slice(0, cap);
    const labels = shortNames(cols);
    const byCol = new Map(cols.map((col, i) => [col, labels[i]!]));
    const data: { t: Date; y: number; f: string }[] = [];
    for (const r of p.rows) {
      const t = new Date(timeMsAt(r, xCol));
      if (Number.isNaN(t.getTime())) continue;
      for (const col of cols) data.push({ t, y: numAt(r, col), f: byCol.get(col)! });
    }
    if (data.length === 0) return <ChartEmpty message="No values in range" />;
    return (
      <PlotChart
        p={p}
        tokens={c.tokens}
        signature={sig(p, cols.join(","))}
        build={() => ({
          ...COMMON,
          marginLeft: 36,
          marginRight: 108,
          x: { type: "utc", label: null, tickSize: 0, ticks: 3 },
          y: { label: null, ticks: 2, tickSize: 0, tickFormat: compactNumber },
          fy: { label: null, domain: labels, tickSize: 0 },
          marks: [
            Plot.lineY(data, { x: "t", y: "y", fy: "f", stroke: c.source(0), strokeWidth: 1.3, curve: "monotone-x" }),
            Plot.frame({ stroke: c.tokens.line, strokeOpacity: 0.4 }),
          ],
        })}
      />
    );
  }

  // Long form: a measure faceted by a group key.
  const yCol = yCols[0] ?? "n";
  const cap = Math.max(2, Math.floor(p.height / 26));
  const data = p.rows
    .map((r) => ({ t: new Date(timeMsAt(r, xCol)), y: numAt(r, yCol), f: facetCol ? strAt(r, facetCol) : "" }))
    .filter((d) => !Number.isNaN(d.t.getTime()));
  if (data.length === 0) return <ChartEmpty message="No values in range" />;
  const facets = [...new Set(data.map((d) => d.f))].slice(0, cap);
  const keep = new Set(facets);
  const shown = data.filter((d) => keep.has(d.f));
  return (
    <PlotChart
      p={p}
      tokens={c.tokens}
      signature={sig(p, facets.join(","))}
      build={() => ({
        ...COMMON,
        marginLeft: 36,
        marginRight: 108,
        x: { type: "utc", label: null, tickSize: 0, ticks: 3 },
        y: { label: null, ticks: 2, tickSize: 0, tickFormat: compactNumber },
        fy: { label: null, domain: facets, tickSize: 0 },
        marks: [
          Plot.areaY(shown, { x: "t", y: "y", fy: "f", fill: c.source(0, 0.16), curve: "monotone-x" }),
          Plot.lineY(shown, { x: "t", y: "y", fy: "f", stroke: c.source(0), strokeWidth: 1.2, curve: "monotone-x" }),
          Plot.frame({ stroke: c.tokens.line, strokeOpacity: 0.4 }),
        ],
      })}
    />
  );
}

/* ---- composition ---------------------------------------------------------- */

export function CompositionChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const xCol = col1(p.spec, "x");
  const parts = colN(p.spec, "parts");
  if (parts.length === 0) return <ChartEmpty message="No parts to stack" />;
  const data: { t: Date; v: number; part: string }[] = [];
  for (const r of p.rows) {
    const t = new Date(timeMsAt(r, xCol));
    if (Number.isNaN(t.getTime())) continue;
    for (const part of parts) data.push({ t, v: numAt(r, part), part: humanLabel(part) });
  }
  if (data.length === 0) return <ChartEmpty message="Nothing to compose" />;
  const names = parts.map(humanLabel);
  const palette = c.sequential(names.length);
  const legend: LegendItem[] = names.map((n, i) => ({ label: n, color: palette[i % palette.length]! }));
  return (
    <PlotChart
      p={p}
      tokens={c.tokens}
      timeAxis
      legend={legend}
      signature={sig(p, parts.join(","))}
      build={() => ({
        ...COMMON,
        x: { type: "utc", label: null, tickSize: 0, ticks: 6 },
        y: { label: null, tickSize: 0, ticks: 4, tickFormat: compactNumber },
        color: { domain: names, range: palette },
        marks: [
          Plot.areaY(data, { x: "t", y: "v", z: "part", fill: "part", fillOpacity: 0.9, curve: "monotone-x", order: names }),
          Plot.ruleY([0], { stroke: c.tokens.line }),
          Plot.tip(data, Plot.pointerX({ x: "t", y: "v", z: "part", channels: { part: "part" }, fill: c.tokens.bg2, stroke: c.tokens.lineStrong })),
        ],
      })}
    />
  );
}

/* ---- sawtooth ------------------------------------------------------------- */

export function SawtoothChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const xCol = col1(p.spec, "x");
  const yCol = col1(p.spec, "y");
  const seriesCol = col1(p.spec, "series");
  const markerCol = col1(p.spec, "marker");
  const unit = unitOf(p.spec, yCol);
  const data = p.rows
    .map((r) => ({ t: new Date(timeMsAt(r, xCol)), y: numAt(r, yCol), s: seriesCol ? strAt(r, seriesCol) : "level" }))
    .filter((d) => !Number.isNaN(d.t.getTime()) && Number.isFinite(d.y));
  if (data.length === 0) return <ChartEmpty message="No levels in range" />;
  const cats = [...new Set(data.map((d) => d.s))].slice(0, 10);
  const palette = c.categorical(cats.length);
  const colorFor = new Map(cats.map((s, i) => [s, palette[i % palette.length]!]));
  const markers = markerCol
    ? p.rows.map((r) => timeMsAt(r, markerCol)).filter((m) => Number.isFinite(m)).map((m) => new Date(m))
    : [];
  const legend: LegendItem[] = cats.length > 1 ? cats.map((s, i) => ({ label: s || "—", color: palette[i % palette.length]! })) : [];
  return (
    <PlotChart
      p={p}
      tokens={c.tokens}
      timeAxis
      legend={legend}
      signature={sig(p, cats.join(","))}
      build={() => ({
        ...COMMON,
        x: { type: "utc", label: null, tickSize: 0, ticks: 6 },
        y: { label: humanLabel(yCol || "level"), labelAnchor: "top", tickSize: 0, ticks: 4, tickFormat: (v: number) => formatMeasure(v, unit) },
        color: { domain: cats, range: cats.map((s) => colorFor.get(s)!) },
        marks: [
          markers.length ? Plot.ruleX(markers, { stroke: c.tokens.brass, strokeDasharray: "2,3", strokeOpacity: 0.7 }) : null,
          Plot.lineY(data, { x: "t", y: "y", z: "s", stroke: cats.length > 1 ? "s" : c.source(0), strokeWidth: 1.4, curve: "step-after" }),
          Plot.tip(data, Plot.pointerX({ x: "t", y: "y", z: "s", channels: { series: "s" }, fill: c.tokens.bg2, stroke: c.tokens.lineStrong })),
        ].filter(Boolean),
      })}
    />
  );
}

/* ---- gantt ---------------------------------------------------------------- */

export function GanttChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const startCol = col1(p.spec, "start");
  const endCol = col1(p.spec, "end");
  const labelCol = col1(p.spec, "label");
  const laneCol = col1(p.spec, "lane");
  const data = p.rows
    .map((r, i) => ({
      i,
      s: new Date(timeMsAt(r, startCol)),
      e: new Date(timeMsAt(r, endCol)),
      label: strAt(r, labelCol),
      lane: laneCol ? strAt(r, laneCol) : strAt(r, labelCol) || `#${i}`,
    }))
    .filter((d) => !Number.isNaN(d.s.getTime()));
  if (data.length === 0) return <ChartEmpty message="No intervals in range" />;
  for (const d of data) if (Number.isNaN(d.e.getTime()) || d.e <= d.s) d.e = new Date(d.s.getTime() + 60_000);
  const lanes = [...new Set(data.map((d) => d.lane))].slice(0, 40);
  const seen = new Set(lanes);
  const rows = data.filter((d) => seen.has(d.lane));
  return (
    <PlotChart
      p={p}
      tokens={c.tokens}
      timeAxis
      signature={sig(p, String(lanes.length))}
      build={() => ({
        ...COMMON,
        marginLeft: 120,
        x: { type: "utc", label: null, tickSize: 0, ticks: 6 },
        y: { label: null, domain: lanes, tickSize: 0 },
        marks: [
          Plot.barX(rows, {
            x1: "s",
            x2: "e",
            y: "lane",
            fill: c.source(0, 0.85),
            rx: 2,
            insetTop: 2,
            insetBottom: 2,
          }),
          Plot.tip(rows, Plot.pointerX({ x1: "s", x2: "e", y: "lane", channels: { label: "label" }, fill: c.tokens.bg2, stroke: c.tokens.lineStrong })),
        ],
      })}
    />
  );
}
