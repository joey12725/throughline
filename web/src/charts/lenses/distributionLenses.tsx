/*
 * Distribution lenses. The ranked bars (top_values, tags, dwell_rank,
 * speaker_share, reply_latency) are hand SVG for direct labels and click-drill;
 * histogram and scatter are Plot. Colour: one series -> source hue; a coloured
 * category -> categorical ramp.
 */
import * as Plot from "@observablehq/plot";
import type { ReactNode } from "react";
import { col1, numAt, optArr, strAt, unitOf } from "../runtime/accessors";
import { compactNumber, exactNumber, formatMeasure, humanDuration, humanLabel } from "../runtime/format";
import { useChartColors } from "../primitives/colors";
import { PlotChart } from "../primitives/PlotChart";
import { RankedBars, type BarItem } from "../primitives/RankedBars";
import { ChartEmpty, type LegendItem } from "../primitives/parts";
import type { DrillEvent, LensRenderProps } from "../types";

function sig(p: LensRenderProps, extra = ""): string {
  return `${p.spec.lens}|${p.rows.length}|${JSON.stringify(p.spec.encode)}|${p.truncated}|${extra}`;
}

/* ---- top_values / tags ---------------------------------------------------- */

function rankedFrom(p: LensRenderProps, labelCol: string | undefined, valueCol: string | undefined, color: string): BarItem[] {
  return p.rows
    .map((r, i) => {
      const label = strAt(r, labelCol);
      return { key: `${label}|${i}`, label, value: numAt(r, valueCol), color, valueText: exactNumber(numAt(r, valueCol)) };
    })
    .filter((b) => Number.isFinite(b.value))
    .sort((a, b) => b.value - a.value);
}

export function TopValuesChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const items = rankedFrom(p, col1(p.spec, "label"), col1(p.spec, "value"), c.source(0, 0.9));
  const field = Array.isArray(p.spec.provenance.fields) ? p.spec.provenance.fields[0] : undefined;
  return (
    <RankedBars
      items={items}
      width={p.width}
      height={p.height}
      tokens={c.tokens}
      truncated={p.truncated}
      measureLabel="records"
      onPick={(_k, label) => p.onDrill?.({ kind: "category", field, value: label } as DrillEvent)}
    />
  );
}

export function TagsChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const items = rankedFrom(p, col1(p.spec, "label"), col1(p.spec, "value"), c.source(1, 0.9));
  return (
    <RankedBars
      items={items}
      width={p.width}
      height={p.height}
      tokens={c.tokens}
      truncated={p.truncated}
      measureLabel="records"
      onPick={(_k, label) => p.onDrill?.({ kind: "category", value: label })}
    />
  );
}

/* ---- dwell_rank ----------------------------------------------------------- */

export function DwellRankChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const labelCol = col1(p.spec, "label");
  const valueCol = col1(p.spec, "value");
  const visitsCol = col1(p.spec, "visits");
  const latCol = col1(p.spec, "lat");
  const lonCol = col1(p.spec, "lon");
  const items: BarItem[] = p.rows
    .map((r, i) => {
      const label = strAt(r, labelCol);
      const value = numAt(r, valueCol);
      const visits = visitsCol ? numAt(r, visitsCol) : NaN;
      return {
        key: `${label}|${i}`,
        label,
        value,
        color: c.source(0, 0.9),
        valueText: humanDuration(value),
        sub: Number.isFinite(visits) ? `${exactNumber(visits)} stays` : undefined,
        lat: latCol ? numAt(r, latCol) : NaN,
        lon: lonCol ? numAt(r, lonCol) : NaN,
      };
    })
    .filter((b) => Number.isFinite(b.value))
    .sort((a, b) => b.value - a.value);
  return (
    <RankedBars
      items={items}
      width={p.width}
      height={p.height}
      tokens={c.tokens}
      truncated={p.truncated}
      measureLabel="time spent"
      onPick={(_k, label) => {
        const it = items.find((x) => x.label === label) as (BarItem & { lat?: number; lon?: number }) | undefined;
        p.onDrill?.({ kind: "place", label, lat: it?.lat, lon: it?.lon });
      }}
    />
  );
}

/* ---- speaker_share -------------------------------------------------------- */

export function SpeakerShareChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const labelCol = col1(p.spec, "label");
  const valueCol = col1(p.spec, "value");
  const charsCol = col1(p.spec, "chars");
  const items: BarItem[] = p.rows
    .map((r, i) => {
      const label = strAt(r, labelCol);
      const value = numAt(r, valueCol);
      const chars = charsCol ? numAt(r, charsCol) : NaN;
      return {
        key: `${label}|${i}`,
        label,
        value,
        color: c.source(i % 6, 0.9),
        valueText: exactNumber(value),
        sub: Number.isFinite(chars) ? `${compactNumber(chars)} chars` : undefined,
      };
    })
    .filter((b) => Number.isFinite(b.value))
    .sort((a, b) => b.value - a.value);
  return (
    <RankedBars
      items={items}
      width={p.width}
      height={p.height}
      tokens={c.tokens}
      truncated={p.truncated}
      measureLabel="records per speaker"
      onPick={(_k, label) => p.onDrill?.({ kind: "person", id: label, label })}
    />
  );
}

/* ---- reply_latency -------------------------------------------------------- */

export function ReplyLatencyChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const binCol = col1(p.spec, "bin");
  const valueCol = col1(p.spec, "value");
  const bins = (optArr(p.spec, "bins") ?? []).map((x) => String(x));
  const palette = c.sequential(Math.max(bins.length, 8));
  const items: BarItem[] = p.rows
    .map((r) => {
      const bin = Math.round(numAt(r, binCol));
      const value = numAt(r, valueCol);
      const label = bins[bin] ?? `class ${bin}`;
      return { key: String(bin), label, value, color: palette[Math.min(bin, palette.length - 1)] ?? c.source(0), valueText: exactNumber(value), order: bin };
    })
    .filter((b) => Number.isFinite(b.value))
    .sort((a, b) => a.order - b.order);
  return (
    <RankedBars items={items} width={p.width} height={p.height} tokens={c.tokens} truncated={p.truncated} measureLabel="replies per latency class" />
  );
}

/* ---- histogram ------------------------------------------------------------ */

export function HistogramChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const x0 = col1(p.spec, "x0");
  const x1 = col1(p.spec, "x1");
  const yCol = col1(p.spec, "y");
  const label = p.spec.options?.label as string | undefined;
  const unit = label ? unitOf(p.spec, label) : undefined;
  const data = p.rows
    .map((r) => ({ lo: numAt(r, x0), hi: numAt(r, x1), n: numAt(r, yCol) }))
    .filter((d) => Number.isFinite(d.lo) && Number.isFinite(d.hi));
  if (data.length === 0) return <ChartEmpty message="No distribution to draw" />;
  return (
    <PlotChart
      p={p}
      tokens={c.tokens}
      signature={sig(p, String(data.length))}
      build={() => ({
        marginTop: 22,
        marginRight: 14,
        marginBottom: 28,
        marginLeft: 46,
        x: { label: label ? `${humanLabel(label)}${unit ? ` (${unit})` : ""}` : null, labelAnchor: "center", tickSize: 0, ticks: 6, tickFormat: (v: number) => formatMeasure(v, unit) },
        y: { label: "records", labelAnchor: "top", tickSize: 0, ticks: 4, tickFormat: compactNumber },
        marks: [
          Plot.rectY(data, { x1: "lo", x2: "hi", y: "n", fill: c.source(0, 0.85), inset: 0.5, rx: 1 }),
          Plot.ruleY([0], { stroke: c.tokens.line }),
          Plot.tip(data, Plot.pointerX({ x1: "lo", x2: "hi", y: "n", fill: c.tokens.bg2, stroke: c.tokens.lineStrong })),
        ],
      })}
    />
  );
}

/* ---- scatter -------------------------------------------------------------- */

export function ScatterChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const xCol = col1(p.spec, "x");
  const yCol = col1(p.spec, "y");
  const colorCol = col1(p.spec, "color");
  const all = p.rows
    .map((r) => ({ x: numAt(r, xCol), y: numAt(r, yCol), g: colorCol ? strAt(r, colorCol) : "" }))
    .filter((d) => Number.isFinite(d.x) && Number.isFinite(d.y));
  if (all.length === 0) return <ChartEmpty message="No pairs to plot" />;
  // Cap the number of marks so SVG stays responsive; sampling keeps the shape.
  const cap = 4000;
  const data = all.length > cap ? all.filter((_, i) => i % Math.ceil(all.length / cap) === 0) : all;
  const cats = colorCol ? [...new Set(data.map((d) => d.g))].slice(0, 10) : [];
  const palette = c.categorical(Math.max(cats.length, 1));
  const colorFor = new Map(cats.map((s, i) => [s, palette[i % palette.length]!]));
  const legend: LegendItem[] = cats.length >= 2 ? cats.map((s, i) => ({ label: s || "—", color: palette[i % palette.length]! })) : [];
  const xUnit = unitOf(p.spec, xCol);
  const yUnit = unitOf(p.spec, yCol);
  return (
    <PlotChart
      p={p}
      tokens={c.tokens}
      legend={legend}
      signature={sig(p, `${data.length}|${cats.join(",")}`)}
      build={() => ({
        marginTop: 22,
        marginRight: 16,
        marginBottom: 30,
        marginLeft: 50,
        x: { label: humanLabel(xCol || "x"), labelAnchor: "center", tickSize: 0, ticks: 6, tickFormat: (v: number) => formatMeasure(v, xUnit) },
        y: { label: humanLabel(yCol || "y"), labelAnchor: "top", tickSize: 0, ticks: 5, tickFormat: (v: number) => formatMeasure(v, yUnit) },
        color: cats.length ? { domain: cats, range: cats.map((s) => colorFor.get(s)!) } : undefined,
        marks: [
          Plot.dot(data, {
            x: "x",
            y: "y",
            r: 2.4,
            fill: cats.length ? "g" : c.source(0),
            fillOpacity: data.length > 800 ? 0.4 : 0.75,
            stroke: c.tokens.bg1,
            strokeWidth: 0.4,
          }),
          Plot.tip(data, Plot.pointer({ x: "x", y: "y", fill: c.tokens.bg2, stroke: c.tokens.lineStrong })),
        ],
      })}
    />
  );
}
