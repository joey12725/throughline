/*
 * Dense grids on hand-written Canvas 2D: calendar_heat, weekday_hour,
 * status_matrix, thread_tapestry, link_matrix, interval_clock and the large
 * strip. Canvas reads tokens for the active theme (it cannot read CSS vars) and
 * re-paints on theme or size change via useCanvas.
 */
import { useRef, useState, type ReactNode } from "react";
import { col1, numAt, optArr, optNum, strAt, timeMsAt } from "../runtime/accessors";
import { exactNumber, formatMeasure, hourLabel, humanLabel, timeTickLabel, tooltipDay, tooltipTime, weekdayLabel } from "../runtime/format";
import { grainMs } from "../runtime/scales";
import { useCanvas } from "../runtime/hooks";
import { statusTone, type ChartTokens } from "../runtime/tokens";
import { useChartColors } from "../primitives/colors";
import { HeatGrid, type GridAxisItem, type GridCell, type ColKind } from "../primitives/HeatGrid";
import { ChartBox, ChartEmpty, Legend, TruncationBanner, type LegendItem } from "../primitives/parts";
import { ChartTooltip, type TipState } from "../primitives/Tooltip";
import type { Coverage, LensRenderProps } from "../types";

function colKindFor(coverage: Coverage | null | undefined, now: number) {
  const covFrom = coverage?.from ? Date.parse(coverage.from) : NaN;
  const covTo = coverage?.to ? Date.parse(coverage.to) : now;
  return (ms: number): ColKind => {
    if (ms > now) return "future";
    if (coverage && Number.isFinite(covFrom)) return ms >= covFrom && ms <= covTo ? "covered" : "uncovered";
    return "covered";
  };
}

/* ---- calendar_heat -------------------------------------------------------- */

export function CalendarHeatChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const dateCol = col1(p.spec, "date");
  const valueCol = col1(p.spec, "value");
  const [tip, setTip] = useState<TipState | null>(null);
  const geom = useRef({ x0: 0, y0: 0, cell: 0, firstMs: 0, cols: 0 });

  const data = p.rows
    .map((r) => ({ ms: dayMs(r[dateCol ?? ""] ), v: numAt(r, valueCol) }))
    .filter((d) => Number.isFinite(d.ms) && Number.isFinite(d.v));
  if (data.length === 0) return <ChartEmpty message="No days to draw" />;
  const byDay = new Map<number, number>();
  let max = 0;
  let minMs = Infinity;
  let maxMs = -Infinity;
  for (const d of data) {
    byDay.set(d.ms, (byDay.get(d.ms) ?? 0) + d.v);
    minMs = Math.min(minMs, d.ms);
    maxMs = Math.max(maxMs, d.ms);
  }
  for (const v of byDay.values()) max = Math.max(max, v);
  const DAY = 86_400_000;
  const startMon = startOfWeekMonday(minMs);
  const weeks = Math.max(1, Math.ceil((maxMs - startMon) / DAY / 7) + 1);
  const unit = valueCol ? (p.spec.options?.units as Record<string, string> | undefined)?.[valueCol] : undefined;
  const now = p.now ?? Date.now();

  const bannerH = p.truncated ? 26 : 0;
  const topLabels = 14;
  const leftLabels = 22;

  const canvasRef = useCanvas(
    (ctx, size) => {
      const availW = size.width - leftLabels - 4;
      const availH = size.height - bannerH - topLabels - 2;
      const cell = Math.max(4, Math.min(availW / weeks, availH / 7) - 1);
      const gap = Math.min(2, cell * 0.14);
      const x0 = leftLabels;
      const y0 = bannerH + topLabels;
      geom.current = { x0, y0, cell: cell + gap, firstMs: startMon, cols: weeks };
      ctx.font = `9px ${c.tokens.fontUi}`;
      ctx.textBaseline = "middle";
      // Weekday labels (M W F).
      ctx.fillStyle = c.tokens.ink3;
      ctx.textAlign = "right";
      for (const wd of [0, 2, 4]) ctx.fillText(weekdayLabel(wd + 1)[0]!, leftLabels - 4, y0 + wd * (cell + gap) + cell / 2);
      // Cells + month labels.
      ctx.textAlign = "left";
      let lastMonth = -1;
      for (let w = 0; w < weeks; w++) {
        for (let d = 0; d < 7; d++) {
          const ms = startMon + (w * 7 + d) * DAY;
          const x = x0 + w * (cell + gap);
          const y = y0 + d * (cell + gap);
          const month = new Date(ms).getUTCMonth();
          if (d === 0 && month !== lastMonth && x < size.width - 18) {
            ctx.fillStyle = c.tokens.ink3;
            ctx.fillText(timeTickLabel(ms, "month").split(" ")[0]!, x, bannerH + topLabels / 2);
            lastMonth = month;
          }
          const v = byDay.get(ms);
          if (ms > now) {
            ctx.fillStyle = c.tokens.gridSoft;
          } else if (v === undefined) {
            ctx.fillStyle = c.tokens.gridSoft;
          } else {
            const t = max > 0 ? v / max : 0;
            ctx.fillStyle = c.sourceRgb(0, 0.15 + 0.85 * t);
          }
          ctx.fillRect(x, y, cell, cell);
        }
      }
    },
    { width: p.width, height: p.height },
    [c.tokens, byDay, max, weeks, startMon, bannerH],
  );

  const onMove = (e: React.PointerEvent): void => {
    const g = geom.current;
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - host.left;
    const py = e.clientY - host.top;
    const w = Math.floor((px - g.x0) / g.cell);
    const d = Math.floor((py - g.y0) / g.cell);
    if (w < 0 || d < 0 || d > 6 || w >= g.cols) {
      setTip(null);
      return;
    }
    const ms = g.firstMs + (w * 7 + d) * 86_400_000;
    const v = byDay.get(ms);
    setTip({ x: px, y: py, title: tooltipDay(ms, "UTC"), rows: [{ label: unit ? humanLabel(valueCol!) : "records", value: v === undefined ? "—" : formatMeasure(v, unit), swatch: c.source(0) }] });
  };
  const onClick = (e: React.PointerEvent): void => {
    const g = geom.current;
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const w = Math.floor((e.clientX - host.left - g.x0) / g.cell);
    const d = Math.floor((e.clientY - host.top - g.y0) / g.cell);
    if (w < 0 || d < 0 || d > 6 || w >= g.cols) return;
    const ms = g.firstMs + (w * 7 + d) * 86_400_000;
    p.onDrill?.({ kind: "bucket", from: new Date(ms).toISOString(), to: new Date(ms + 86_400_000).toISOString(), label: tooltipDay(ms, "UTC") });
  };

  return (
    <ChartBox width={p.width} height={p.height}>
      <TruncationBanner truncated={p.truncated} />
      <div style={{ position: "relative", width: p.width, height: p.height - bannerH }} onPointerMove={onMove} onPointerLeave={() => setTip(null)} onClick={onClick as unknown as React.MouseEventHandler}>
        <canvas ref={canvasRef} role="img" aria-label="Calendar heatmap of records per day" style={{ cursor: "pointer" }} />
        <ChartTooltip tip={tip} width={p.width} height={p.height - bannerH} />
      </div>
    </ChartBox>
  );
}

/* ---- weekday_hour --------------------------------------------------------- */

export function WeekdayHourChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const wdCol = col1(p.spec, "weekday");
  const hrCol = col1(p.spec, "hour");
  const valCol = col1(p.spec, "value");
  const base = optNum(p.spec, "weekday_base") ?? 1;
  const map = new Map<string, number>();
  let max = 0;
  for (const r of p.rows) {
    const wd = Math.round(numAt(r, wdCol));
    const hr = Math.round(numAt(r, hrCol));
    const v = numAt(r, valCol);
    if (!Number.isFinite(wd) || !Number.isFinite(hr) || !Number.isFinite(v)) continue;
    const iso = base === 0 ? (wd === 0 ? 7 : wd) : wd; // normalise to ISO 1..7
    map.set(`${iso}|${hr}`, v);
    max = Math.max(max, v);
  }
  if (max === 0) return <ChartEmpty message="No activity to grid" />;
  const rows: GridAxisItem[] = [1, 2, 3, 4, 5, 6, 7].map((d) => ({ key: String(d), label: weekdayLabel(d) }));
  const cols: GridAxisItem[] = Array.from({ length: 24 }, (_, h) => ({ key: String(h), label: h % 6 === 0 ? hourLabel(h) : "" }));
  const cell = (rk: string, ck: string): GridCell | null => {
    const v = map.get(`${rk}|${ck}`) ?? 0;
    return { value: v, color: c.sourceRgb(0, 0.15 + 0.85 * (v / max)), title: `${weekdayLabel(Number(rk))} · ${hourLabel(Number(ck))}`, rows: [{ label: "records", value: exactNumber(v), swatch: c.source(0) }] };
  };
  return (
    <HeatGrid width={p.width} height={p.height} tokens={c.tokens} rows={rows} cols={cols} cell={cell} rowHeaderWidth={38} colLabelEvery={1} truncated={p.truncated} ariaLabel="Weekday against hour of day" onPick={(rk, ck) => p.onDrill?.({ kind: "category", value: `${weekdayLabel(Number(rk))} ${hourLabel(Number(ck))}` })} />
  );
}

/* ---- status_matrix -------------------------------------------------------- */

export function StatusMatrixChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const xCol = col1(p.spec, "x");
  const laneCol = col1(p.spec, "lane");
  const statusCol = col1(p.spec, "color");
  const valCol = col1(p.spec, "value");
  const grain = p.grain ?? "day";
  const laneOf = (r: (typeof p.rows)[number]) => (laneCol ? strAt(r, laneCol) : "all");

  const bucketSet = new Map<number, string>();
  const laneSet = new Set<string>();
  // lane|bucket -> status -> count
  const cells = new Map<string, Map<string, number>>();
  let max = 0;
  for (const r of p.rows) {
    const ms = bucketStart(timeMsAt(r, xCol), grain);
    if (!Number.isFinite(ms)) continue;
    const lane = laneOf(r);
    const status = strAt(r, statusCol) || "—";
    const v = valCol ? numAt(r, valCol) : 1;
    bucketSet.set(ms, timeTickLabel(ms, grain));
    laneSet.add(lane);
    const key = `${lane}|${ms}`;
    let m = cells.get(key);
    if (!m) cells.set(key, (m = new Map()));
    m.set(status, (m.get(status) ?? 0) + v);
  }
  if (cells.size === 0) return <ChartEmpty message="No status history" />;
  const bucketMs = [...bucketSet.keys()].sort((a, b) => a - b);
  for (const m of cells.values()) {
    let tot = 0;
    for (const v of m.values()) tot += v;
    max = Math.max(max, tot);
  }
  const statuses = new Set<string>();
  for (const m of cells.values()) for (const s of m.keys()) statuses.add(s);
  const statusList = [...statuses].slice(0, 8);
  const toneColor = (s: string) => {
    const t = statusTone(s);
    return t === "ok" ? c.tokens.ok : t === "warn" ? c.tokens.warn : t === "bad" ? c.tokens.bad : c.tokens.ink3;
  };
  const rows: GridAxisItem[] = [...laneSet].slice(0, 60).map((l) => ({ key: l, label: l }));
  const cols: GridAxisItem[] = bucketMs.map((ms) => ({ key: String(ms), label: bucketSet.get(ms) ?? "", ms }));
  const cell = (rk: string, ck: string): GridCell | null => {
    const m = cells.get(`${rk}|${ck}`);
    if (!m) return { value: 0, color: "transparent", title: "" };
    let tot = 0;
    let dom = "";
    let domN = -1;
    for (const [s, v] of m) {
      tot += v;
      if (v > domN) {
        domN = v;
        dom = s;
      }
    }
    const alpha = 0.35 + 0.65 * (tot / Math.max(1, max));
    const base = toneColor(dom);
    return {
      value: tot,
      color: mix(base, alpha),
      title: `${rk} · ${timeTickLabel(Number(ck), grain)}`,
      rows: [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4).map(([s, v]) => ({ label: s, value: exactNumber(v), swatch: toneColor(s) })),
    };
  };
  const legend: LegendItem[] = statusList.map((s) => ({ label: s, color: toneColor(s), glyph: statusTone(s) === "ok" ? "●" : statusTone(s) === "bad" ? "■" : statusTone(s) === "warn" ? "▲" : "○" }));
  return (
    <HeatGrid
      width={p.width}
      height={p.height}
      tokens={c.tokens}
      rows={rows}
      cols={cols}
      cell={cell}
      colKind={colKindFor(p.coverage, p.now ?? Date.now())}
      legend={legend}
      truncated={p.truncated}
      ariaLabel="Sources against days, coloured by status"
      onPick={(rk, ck) => p.onDrill?.({ kind: "bucket", from: new Date(Number(ck)).toISOString(), to: new Date(Number(ck) + grainMs(grain)).toISOString(), label: rk })}
    />
  );
}

/* ---- thread_tapestry ------------------------------------------------------ */

export function ThreadTapestryChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const rowCol = col1(p.spec, "row");
  const labelCol = col1(p.spec, "label");
  const xCol = col1(p.spec, "x");
  const valCol = col1(p.spec, "value");
  const peopleCol = col1(p.spec, "people");
  const orderCol = col1(p.spec, "order");
  const grain = p.grain ?? "day";

  const threads = new Map<string, { label: string; order: number; people: number }>();
  const bucketSet = new Map<number, string>();
  const cells = new Map<string, number>();
  let max = 0;
  for (const r of p.rows) {
    const tid = strAt(r, rowCol);
    const ms = bucketStart(timeMsAt(r, xCol), grain);
    if (!tid || !Number.isFinite(ms)) continue;
    const v = valCol ? numAt(r, valCol) : 1;
    const ord = orderCol ? timeMsAt(r, orderCol) : ms;
    if (!threads.has(tid)) threads.set(tid, { label: labelCol ? strAt(r, labelCol) : tid, order: ord, people: peopleCol ? numAt(r, peopleCol) : NaN });
    bucketSet.set(ms, timeTickLabel(ms, grain));
    const key = `${tid}|${ms}`;
    const nv = (cells.get(key) ?? 0) + v;
    cells.set(key, nv);
    max = Math.max(max, nv);
  }
  if (threads.size === 0) return <ChartEmpty message="No threads in range" />;
  const bucketMs = [...bucketSet.keys()].sort((a, b) => a - b);
  const ordered = [...threads.entries()].sort((a, b) => a[1].order - b[1].order);
  const rows: GridAxisItem[] = ordered.map(([key, t]) => ({ key, label: t.label }));
  const cols: GridAxisItem[] = bucketMs.map((ms) => ({ key: String(ms), label: bucketSet.get(ms) ?? "", ms }));
  const cell = (rk: string, ck: string): GridCell | null => {
    const v = cells.get(`${rk}|${ck}`) ?? 0;
    const t = threads.get(rk);
    return {
      value: v,
      color: c.sourceRgb(0, 0.18 + 0.82 * (v / Math.max(1, max))),
      title: t?.label ?? rk,
      rows: v ? [{ label: "records", value: exactNumber(v), swatch: c.source(0) }, ...(t && Number.isFinite(t.people) ? [{ label: "people", value: exactNumber(t.people) }] : [])] : [],
    };
  };
  return (
    <HeatGrid
      width={p.width}
      height={p.height}
      tokens={c.tokens}
      rows={rows}
      cols={cols}
      cell={cell}
      colKind={colKindFor(p.coverage, p.now ?? Date.now())}
      truncated={p.truncated}
      ariaLabel="Threads over time, cells by activity"
      onPick={(rk) => p.onDrill?.({ kind: "thread", id: rk, label: threads.get(rk)?.label })}
    />
  );
}

/* ---- link_matrix ---------------------------------------------------------- */

export function LinkMatrixChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const rowCol = col1(p.spec, "row");
  const colCol = col1(p.spec, "col");
  const valCol = col1(p.spec, "value");
  const rowTot = new Map<string, number>();
  const colTot = new Map<string, number>();
  const cells = new Map<string, number>();
  let max = 0;
  for (const r of p.rows) {
    const a = strAt(r, rowCol) || "—";
    const b = strAt(r, colCol) || "—";
    const v = valCol ? numAt(r, valCol) : 1;
    if (!Number.isFinite(v)) continue;
    cells.set(`${a}\u0001${b}`, (cells.get(`${a}\u0001${b}`) ?? 0) + v);
    rowTot.set(a, (rowTot.get(a) ?? 0) + v);
    colTot.set(b, (colTot.get(b) ?? 0) + v);
    max = Math.max(max, cells.get(`${a}\u0001${b}`)!);
  }
  if (cells.size === 0) return <ChartEmpty message="No pairs to cross" />;
  const topRows = [...rowTot.entries()].sort((a, b) => b[1] - a[1]).slice(0, 30).map((e) => e[0]);
  const topCols = [...colTot.entries()].sort((a, b) => b[1] - a[1]).slice(0, 30).map((e) => e[0]);
  const rows: GridAxisItem[] = topRows.map((k) => ({ key: k, label: k }));
  const cols: GridAxisItem[] = topCols.map((k) => ({ key: k, label: k }));
  const cell = (rk: string, ck: string): GridCell | null => {
    const v = cells.get(`${rk}\u0001${ck}`) ?? 0;
    return { value: v, color: c.sourceRgb(0, 0.12 + 0.88 * (v / Math.max(1, max))), title: `${rk} × ${ck}`, rows: v ? [{ label: "shared", value: exactNumber(v), swatch: c.source(0) }] : [] };
  };
  return <HeatGrid width={p.width} height={p.height} tokens={c.tokens} rows={rows} cols={cols} cell={cell} colLabelEvery={Math.max(1, Math.ceil(topCols.length / 8))} truncated={p.truncated} ariaLabel="One grouping against another" />;
}

/* ---- interval_clock ------------------------------------------------------- */

export function IntervalClockChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const startCol = col1(p.spec, "start");
  const endCol = col1(p.spec, "end");
  const colorCol = col1(p.spec, "color");
  const [tip, setTip] = useState<TipState | null>(null);
  const segsRef = useRef<{ x: number; y0: number; y1: number; label: string; s: number; e: number }[]>([]);
  const geomRef = useRef({ padL: 0, padR: 0, top: 0, bottom: 0, minDay: 0, maxDay: 0 });

  const recs = p.rows
    .map((r) => ({ s: timeMsAt(r, startCol), e: timeMsAt(r, endCol), g: colorCol ? strAt(r, colorCol) : "" }))
    .filter((d) => Number.isFinite(d.s));
  if (recs.length === 0) return <ChartEmpty message="No intervals in range" />;
  for (const d of recs) if (!Number.isFinite(d.e) || d.e <= d.s) d.e = d.s + 300_000;
  const cats = colorCol ? [...new Set(recs.map((d) => d.g))].slice(0, 8) : [];
  const palette = c.categoricalRgb(Math.max(cats.length, 1));
  const colorFor = (g: string) => (cats.length ? palette[Math.max(0, cats.indexOf(g)) % palette.length]! : c.sourceRgb(0, 0.85));
  const DAY = 86_400_000;
  let minDay = Infinity;
  let maxDay = -Infinity;
  for (const d of recs) {
    minDay = Math.min(minDay, dayFloor(d.s));
    maxDay = Math.max(maxDay, dayFloor(d.e));
  }
  const legend: LegendItem[] = cats.length >= 2 ? cats.map((g, i) => ({ label: g || "—", color: palette[i % palette.length]! })) : [];
  const bannerH = p.truncated ? 26 : 0;
  const legendH = legend.length ? 22 : 0;

  const canvasRef = useCanvas(
    (ctx, size) => {
      const padL = 34;
      const padR = 8;
      const top = bannerH + 6;
      const bottom = size.height - 20 - legendH;
      geomRef.current = { padL, padR, top, bottom, minDay, maxDay };
      const days = Math.max(1, (maxDay - minDay) / DAY + 1);
      const colW = (size.width - padL - padR) / days;
      const h = bottom - top;
      // Hour gridlines (hairline) and labels.
      ctx.font = `9px ${c.tokens.fontUi}`;
      ctx.textBaseline = "middle";
      ctx.textAlign = "right";
      ctx.strokeStyle = c.tokens.gridSoft;
      ctx.lineWidth = 1;
      for (const hr of [0, 6, 12, 18, 24]) {
        const y = top + (hr / 24) * h;
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(size.width - padR, y);
        ctx.stroke();
        ctx.fillStyle = c.tokens.ink3;
        if (hr < 24) ctx.fillText(hourLabel(hr), padL - 4, y + 5);
      }
      // Date labels along bottom (sparse).
      ctx.textAlign = "center";
      const every = Math.max(1, Math.ceil(days / Math.max(2, Math.floor((size.width - padL) / 56))));
      for (let di = 0; di < days; di += every) {
        const ms = minDay + di * DAY;
        ctx.fillStyle = c.tokens.ink3;
        ctx.fillText(timeTickLabel(ms, "day"), padL + (di + 0.5) * colW, bottom + 11);
      }
      // Bars (wrap across midnight).
      const segs: typeof segsRef.current = [];
      const barW = Math.max(2, Math.min(14, colW * 0.6));
      for (const d of recs) {
        let s = d.s;
        const end = d.e;
        while (s < end) {
          const day = dayFloor(s);
          const dayEnd = day + DAY;
          const segEnd = Math.min(end, dayEnd);
          const di = Math.round((day - minDay) / DAY);
          const x = padL + (di + 0.5) * colW - barW / 2;
          const y0 = top + ((s - day) / DAY) * h;
          const y1 = top + ((segEnd - day) / DAY) * h;
          ctx.fillStyle = colorFor(d.g);
          roundRect(ctx, x, y0, barW, Math.max(1.5, y1 - y0), 2);
          ctx.fill();
          segs.push({ x: x + barW / 2, y0, y1, label: d.g, s: d.s, e: d.e });
          s = segEnd;
        }
      }
      segsRef.current = segs;
    },
    { width: p.width, height: p.height },
    [c.tokens, recs, cats.join(","), minDay, maxDay, bannerH, legendH],
  );

  const onMove = (e: React.PointerEvent): void => {
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - host.left;
    const py = e.clientY - host.top;
    let best: (typeof segsRef.current)[number] | null = null;
    let bestD = 10;
    for (const s of segsRef.current) {
      if (py < s.y0 - 2 || py > s.y1 + 2) continue;
      const d = Math.abs(px - s.x);
      if (d < bestD) {
        bestD = d;
        best = s;
      }
    }
    if (!best) {
      setTip(null);
      return;
    }
    setTip({ x: px, y: py, title: best.label || "interval", rows: [{ label: "from", value: tooltipTime(best.s, p.tz ?? "UTC") }, { label: "to", value: tooltipTime(best.e, p.tz ?? "UTC") }, { label: "lasts", value: formatMeasure((best.e - best.s) / 1000, "s") }] });
  };

  return (
    <ChartBox width={p.width} height={p.height}>
      <TruncationBanner truncated={p.truncated} />
      <div style={{ position: "relative", width: p.width, height: p.height - bannerH - legendH }} onPointerMove={onMove} onPointerLeave={() => setTip(null)}>
        <canvas ref={canvasRef} role="img" aria-label="Date against time of day; each record a bar" />
        <ChartTooltip tip={tip} width={p.width} height={p.height - bannerH - legendH} />
      </div>
      {legend.length > 0 && <Legend items={legend} />}
    </ChartBox>
  );
}

/* ---- strip ---------------------------------------------------------------- */

export function StripChart(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const xCol = col1(p.spec, "x");
  const colorCol = col1(p.spec, "color");
  const domain = (optArr(p.spec, "domain") ?? [0, 1]).map(Number);
  const [lo, hi] = [domain[0] ?? 0, domain[1] ?? 1];
  const [tip, setTip] = useState<TipState | null>(null);
  const ptsRef = useRef<{ x: number; y: number; v: number; g: string }[]>([]);
  const pts = p.rows
    .map((r) => ({ v: numAt(r, xCol), g: colorCol ? strAt(r, colorCol) : "" }))
    .filter((d) => Number.isFinite(d.v));
  if (pts.length === 0) return <ChartEmpty message="No values on the axis" />;
  const cats = colorCol ? [...new Set(pts.map((d) => d.g))].slice(0, 8) : [];
  const palette = c.categoricalRgb(Math.max(cats.length, 1));
  const colorFor = (g: string) => (cats.length ? palette[Math.max(0, cats.indexOf(g)) % palette.length]! : c.sourceRgb(0, 0.7));
  const legend: LegendItem[] = cats.length >= 2 ? cats.map((g, i) => ({ label: g || "—", color: palette[i % palette.length]! })) : [];
  const bannerH = p.truncated ? 26 : 0;
  const legendH = legend.length ? 22 : 0;

  const canvasRef = useCanvas(
    (ctx, size) => {
      const padL = 8;
      const padR = 8;
      const bottom = size.height - 20;
      const top = bannerH + 6;
      const w = size.width - padL - padR;
      const span = hi - lo || 1;
      const laneH = bottom - top;
      // Axis.
      ctx.strokeStyle = c.tokens.line;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, bottom);
      ctx.lineTo(size.width - padR, bottom);
      ctx.stroke();
      ctx.font = `9px ${c.tokens.fontUi}`;
      ctx.fillStyle = c.tokens.ink3;
      ctx.textBaseline = "top";
      ctx.textAlign = "center";
      for (let i = 0; i <= 4; i++) {
        const v = lo + (span * i) / 4;
        const x = padL + (w * i) / 4;
        ctx.fillText(formatMeasure(v), x, bottom + 4);
      }
      const store: typeof ptsRef.current = [];
      for (let i = 0; i < pts.length; i++) {
        const d = pts[i]!;
        const x = padL + ((d.v - lo) / span) * w;
        const y = top + 6 + ((i * 2654435761) % 1000) / 1000 * (laneH - 12);
        ctx.fillStyle = colorFor(d.g);
        ctx.globalAlpha = pts.length > 600 ? 0.5 : 0.8;
        ctx.beginPath();
        ctx.arc(x, y, 2, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
        store.push({ x, y, v: d.v, g: d.g });
      }
      ptsRef.current = store;
    },
    { width: p.width, height: p.height },
    [c.tokens, pts, cats.join(","), lo, hi, bannerH],
  );

  const onMove = (e: React.PointerEvent): void => {
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - host.left;
    const py = e.clientY - host.top;
    let best: (typeof ptsRef.current)[number] | null = null;
    let bestD = 8;
    for (const s of ptsRef.current) {
      const d = Math.hypot(px - s.x, py - s.y);
      if (d < bestD) {
        bestD = d;
        best = s;
      }
    }
    setTip(best ? { x: px, y: py, title: best.g || humanLabel(xCol ?? "value"), rows: [{ label: humanLabel(xCol ?? "value"), value: formatMeasure(best.v) }] } : null);
  };

  return (
    <ChartBox width={p.width} height={p.height}>
      <TruncationBanner truncated={p.truncated} />
      <div style={{ position: "relative", width: p.width, height: p.height - bannerH - legendH }} onPointerMove={onMove} onPointerLeave={() => setTip(null)}>
        <canvas ref={canvasRef} role="img" aria-label="Every record as a tick on one bounded axis" />
        <ChartTooltip tip={tip} width={p.width} height={p.height - bannerH - legendH} />
      </div>
      {legend.length > 0 && <Legend items={legend} />}
    </ChartBox>
  );
}

/* ---- helpers -------------------------------------------------------------- */

function dayMs(v: unknown): number {
  if (v == null) return NaN;
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) return Date.parse(v + "T00:00:00Z");
  const t = v instanceof Date ? v.getTime() : typeof v === "number" ? v : Date.parse(String(v));
  return Number.isNaN(t) ? NaN : dayFloor(t);
}
function dayFloor(ms: number): number {
  return Math.floor(ms / 86_400_000) * 86_400_000;
}
function startOfWeekMonday(ms: number): number {
  const d = new Date(dayFloor(ms));
  const wd = (d.getUTCDay() + 6) % 7; // 0 = Monday
  return dayFloor(ms) - wd * 86_400_000;
}
function bucketStart(ms: number, grain: string): number {
  if (!Number.isFinite(ms)) return NaN;
  const g = grainMs(grain);
  return Math.floor(ms / g) * g;
}
function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}
function mix(color: string, alpha: number): string {
  // color is a hex token; return rgba with the given alpha.
  const m = /^#([0-9a-f]{6})$/i.exec(color);
  if (!m) return color;
  const n = parseInt(m[1]!, 16);
  return `rgb(${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255} / ${alpha})`;
}
