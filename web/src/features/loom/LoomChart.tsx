/*
 * The Loom — the signature image. One lane per channel, grouped and coloured by
 * plugin, across the whole time range; ink density is records per week on a log
 * scale. Canvas for the dense marks (DPR aware, drawn once to an offscreen
 * buffer), an SVG overlay for year ticks, the brass now-rule, the hover
 * crosshair and the brush, and an HTML gutter for crisp, clickable labels.
 *
 * The time axis is density-aware: by default the span holding the bulk of the
 * records (the focus) takes most of the width, and the sparse early years are
 * compressed into a labelled context band at the left with a visible break. A
 * Linear toggle restores an even axis. Brushing and clicking map back to the
 * right instant under either scale (loomScale.ts, unit-tested).
 *
 * Plugins with many small channels auto-collapse to a single summary band that
 * expands on click, so the gutter stays legible and the whole thing fits one
 * screen. A toggle redraws the exact same grid as a stacked river (sqrt heights,
 * or 100% share); the two cross-fade unless the viewer prefers reduced motion.
 *
 * Brushing sets the global time scope; clicking a cell opens that channel at
 * that week.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { stack, stackOffsetWiggle, stackOffsetExpand, stackOrderInsideOut, area, curveBasis } from "d3";
import { channelLabel, Segmented, useSourceColor } from "@/ui";
import { chartTokens } from "@/charts";
import { useScopes } from "@/store/scopes";
import type { ChannelIndex, ChannelInfo, PluginGroup } from "./useChannelIndex";
import { useLoomData, WEEK_MS, type LoomGrid } from "./loomData";
import { makeLoomScale, choosePivot, type LoomScale, type ScaleMode } from "./loomScale";
import styles from "./LoomChart.module.css";

const GUTTER = 164;
const GUTTER_NARROW = 116;
const HEADER_H = 34;
const AXIS_H = 24;
const GROUP_H = 16;
const MIN_LH = 11;
const MAX_LH = 22;
const LANE_PAD = 1;
/** A plugin with at least this many channels, all individually minor, collapses. */
const AUTO_COLLAPSE_MIN = 6;
const AUTO_COLLAPSE_SHARE = 0.02;

type Mode = "loom" | "river";
type RiverFill = "stack" | "share";

interface LayoutRow {
  kind: "group" | "lane";
  y: number;
  h: number;
  group: PluginGroup;
  collapsed: boolean;
  info?: ChannelInfo;
}

interface Hover {
  x: number;
  channel: string | null;
  weekIdx: number;
  count: number;
  weekMs: number;
}

export function LoomChart({ index, height }: { index: ChannelIndex; height: number }): ReactNode {
  const now = useMemo(() => Date.now(), []);
  const grid = useLoomData(index, now);
  const { rgbFor, theme } = useSourceColor();
  const reduceMotion = useMemo(() => prefersReducedMotion(), []);
  const setRange = useScopes((s) => s.setRange);
  const scopeFrom = useScopes((s) => s.from);
  const scopeTo = useScopes((s) => s.to);
  const navigate = useNavigate();

  const [mode, setMode] = useState<Mode>("loom");
  const [scaleMode, setScaleMode] = useState<ScaleMode>("focus");
  const [riverFill, setRiverFill] = useState<RiverFill>("stack");
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => autoCollapsed(index));
  const [plotW, setPlotW] = useState(900);
  const [gutter, setGutter] = useState(GUTTER);
  const [hover, setHover] = useState<Hover | null>(null);
  const [drag, setDrag] = useState<{ x0: number; x1: number } | null>(null);

  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const loomBufRef = useRef<HTMLCanvasElement | null>(null);
  const riverBufRef = useRef<HTMLCanvasElement | null>(null);
  const blendRef = useRef(0); // 0 = loom, 1 = river
  const rafRef = useRef<number | null>(null);

  // Measure the plot width.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const cw = el.clientWidth;
      const g = cw < 560 ? GUTTER_NARROW : GUTTER;
      setGutter(g);
      setPlotW(Math.max(200, Math.floor(cw - g)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Vertical layout: group headers + lanes, sized to fit the available height.
  const { layout, contentH } = useMemo(
    () => computeLayout(index.groups, collapsed, height - HEADER_H - AXIS_H),
    [index.groups, collapsed, height],
  );

  // Density-aware time scale (focus+context by default, linear on toggle).
  const pivot = useMemo(
    () => (scaleMode === "linear" ? null : choosePivot(grid.weeks, grid.weekTotals)),
    [grid.weeks, grid.weekTotals, scaleMode],
  );
  const scale = useMemo(
    () =>
      makeLoomScale({
        domainFrom: grid.domainFrom,
        domainTo: grid.domainTo,
        plotW,
        mode: scaleMode,
        pivotMs: pivot,
      }),
    [grid.domainFrom, grid.domainTo, plotW, scaleMode, pivot],
  );

  const yearTicks = useMemo(
    () => labelledYearTicks(grid.domainFrom, grid.domainTo, scale),
    [grid.domainFrom, grid.domainTo, scale],
  );

  // Draw the two offscreen buffers whenever the data, size, layout or theme change.
  useEffect(() => {
    if (!grid.weeks.length || plotW <= 0 || contentH <= 0) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const tokens = chartTokens(theme);

    const loomBuf = ensureBuffer(loomBufRef, plotW, contentH, dpr);
    drawLoom(loomBuf, { grid, layout, plotW, contentH, dpr, rgbFor, tokens, scale, now });

    const riverBuf = ensureBuffer(riverBufRef, plotW, contentH, dpr);
    drawRiver(riverBuf, { grid, index, plotW, contentH, dpr, rgbFor, scale, tokens, fill: riverFill });

    paint(canvasRef.current, loomBufRef.current, riverBufRef.current, blendRef.current, plotW, contentH, dpr);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grid, index, layout, plotW, contentH, theme, scale, riverFill, now]);

  // Animate the cross-fade when the mode changes.
  useEffect(() => {
    const target = mode === "river" ? 1 : 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (reduceMotion) {
      blendRef.current = target;
      paint(canvasRef.current, loomBufRef.current, riverBufRef.current, target, plotW, contentH, dpr);
      return;
    }
    const start = performance.now();
    const from = blendRef.current;
    const dur = 460;
    const tick = (t: number): void => {
      const k = Math.min(1, (t - start) / dur);
      const eased = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      blendRef.current = from + (target - from) * eased;
      paint(canvasRef.current, loomBufRef.current, riverBufRef.current, blendRef.current, plotW, contentH, dpr);
      if (k < 1) rafRef.current = requestAnimationFrame(tick);
    };
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [mode, reduceMotion, plotW, contentH]);

  const toggleGroup = useCallback((plugin: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(plugin)) next.delete(plugin);
      else next.add(plugin);
      return next;
    });
  }, []);

  // --- Pointer interaction (hover crosshair, brush, click-through) ----------
  const laneAtY = useCallback(
    (y: number): LayoutRow | null => {
      for (const r of layout) if (r.kind === "lane" && y >= r.y && y < r.y + r.h) return r;
      return null;
    },
    [layout],
  );

  const weekAtX = useCallback((px: number): number => grid.weekIndex(scale.toTime(px)), [grid, scale]);

  const onMove = (e: React.PointerEvent): void => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    if (drag) {
      setDrag({ ...drag, x1: clamp(x, 0, plotW) });
      return;
    }
    const wi = weekAtX(x);
    const row = mode === "loom" ? laneAtY(y) : null;
    const channel = row?.info?.name ?? null;
    const count = channel && wi >= 0 ? grid.counts.get(channel)?.[wi] ?? 0 : 0;
    setHover({ x, channel, weekIdx: wi, count, weekMs: grid.weeks[wi] ?? grid.domainFrom });
  };
  const onLeave = (): void => setHover(null);
  const onDown = (e: React.PointerEvent): void => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = clamp(e.clientX - rect.left, 0, plotW);
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setDrag({ x0: x, x1: x });
  };
  const onUp = (e: React.PointerEvent): void => {
    if (!drag) return;
    const a = Math.min(drag.x0, drag.x1);
    const b = Math.max(drag.x0, drag.x1);
    const rect = e.currentTarget.getBoundingClientRect();
    const y = e.clientY - rect.top;
    setDrag(null);
    if (b - a < 4) {
      // A click, not a brush: open the channel at this week.
      const row = laneAtY(y);
      const wi = weekAtX(a);
      const ws = grid.weeks[wi];
      if (row?.info && wi >= 0 && ws !== undefined) {
        setRange(new Date(ws).toISOString(), new Date(ws + WEEK_MS).toISOString());
        navigate(`/c/${encodeURIComponent(row.info.name)}`);
      }
      return;
    }
    const from = new Date(scale.toTime(a)).toISOString();
    const to = new Date(scale.toTime(b)).toISOString();
    setRange(from, to);
  };

  const nowX = now >= grid.domainFrom && now <= grid.domainTo ? scale.toX(now) : null;
  const scopeBand = useMemo(() => {
    if (!scopeFrom || !scopeTo) return null;
    const a = scale.toX(Date.parse(scopeFrom));
    const b = scale.toX(Date.parse(scopeTo));
    if (Number.isNaN(a) || Number.isNaN(b)) return null;
    return { x: Math.min(a, b), w: Math.abs(b - a) };
  }, [scopeFrom, scopeTo, scale]);

  const tokens = chartTokens(theme);
  const breakX = scale.pivotX !== null ? scale.pivotX + scale.breakW / 2 : null;
  const contextYears =
    scale.pivotMs !== null
      ? `${new Date(grid.domainFrom).getUTCFullYear()}–${new Date(scale.pivotMs).getUTCFullYear()}`
      : null;

  return (
    <div className={styles.root} ref={wrapRef} style={{ height }} data-testid="loom">
      {/* the whole-life marks render below; this hook helps e2e assert presence */}
      <div className={styles.header} style={{ height: HEADER_H }}>
        <div className={styles.headTitle}>
          <span className={styles.headName}>The Loom</span>
          <span className={styles.headHint}>
            every channel across {grid.weeks.length ? `${new Date(grid.domainFrom).getUTCFullYear()}–${new Date(grid.domainTo).getUTCFullYear()}` : "time"} · ink is records per week · drag to scope
          </span>
        </div>
        <div className={styles.headControls}>
          {mode === "river" && (
            <Segmented
              size="sm"
              value={riverFill}
              onChange={(v) => setRiverFill(v as RiverFill)}
              items={[
                { value: "stack", label: "Totals" },
                { value: "share", label: "Share" },
              ]}
              aria-label="River fill"
            />
          )}
          <Segmented
            size="sm"
            value={scaleMode}
            onChange={(v) => setScaleMode(v as ScaleMode)}
            items={[
              { value: "focus", label: "Focus" },
              { value: "linear", label: "Linear" },
            ]}
            aria-label="Time axis scale"
          />
          <Segmented
            size="sm"
            value={mode}
            onChange={(v) => setMode(v as Mode)}
            items={[
              { value: "loom", label: "Loom" },
              { value: "river", label: "River" },
            ]}
            aria-label="Loom display mode"
          />
        </div>
      </div>

      {/* Year axis (fixed on top) */}
      <div className={styles.axis} style={{ height: AXIS_H, marginLeft: gutter }}>
        <svg width={plotW} height={AXIS_H} aria-hidden="true">
          {scale.pivotX !== null && (
            <rect x={0} y={0} width={scale.contextW} height={AXIS_H} fill={tokens.ink3} opacity={0.05} />
          )}
          {yearTicks.map((t) => (
            <g key={t.ms}>
              <line x1={t.x} y1={AXIS_H - 6} x2={t.x} y2={AXIS_H} stroke={tokens.line} />
              {t.label && (
                <text x={t.x + 3} y={AXIS_H - 10} className={styles.yearLabel} fill={tokens.ink3}>
                  {t.year}
                </text>
              )}
            </g>
          ))}
          {contextYears && scale.pivotX !== null && (
            <text x={scale.contextW / 2} y={11} className={styles.ctxLabel} fill={tokens.ink3} textAnchor="middle">
              {contextYears}
            </text>
          )}
          {breakX !== null && <BreakGlyph x={breakX} h={AXIS_H} color={tokens.ink3} />}
        </svg>
      </div>

      <div className={styles.body} style={{ height: height - HEADER_H - AXIS_H }}>
        {/* Left gutter: plugin headers + channel labels (crisp, clickable) */}
        <div className={styles.gutter} data-mode={mode} style={{ width: gutter, height: contentH }}>
          {layout.map((r, i) =>
            r.kind === "group" ? (
              <button
                key={`g${i}`}
                type="button"
                className={styles.groupHead}
                data-collapsed={r.collapsed}
                style={{ top: r.y, height: r.h }}
                onClick={() => toggleGroup(r.group.plugin)}
                aria-expanded={!r.collapsed}
                title={`${r.group.plugin} — ${r.group.channels.length} channels, ${r.group.rows.toLocaleString("en-US")} records · click to ${r.collapsed ? "expand" : "collapse"}`}
              >
                <span className={styles.caret} data-collapsed={r.collapsed}>▸</span>
                <span className={styles.groupDot} style={{ background: rgbFor(r.group.plugin, 0) }} />
                <span className={styles.groupName}>{r.group.plugin}</span>
                {r.collapsed && r.group.channels.length > 1 && (
                  <span className={styles.groupChans}>{r.group.channels.length}ch</span>
                )}
                <span className={styles.groupCount}>{compact(r.group.rows)}</span>
              </button>
            ) : (
              <a
                key={`l${i}`}
                className={styles.laneLabel}
                data-testid="loom-lane"
                style={{ top: r.y, height: r.h }}
                href={`/ui/c/${encodeURIComponent(r.info!.name)}`}
                onClick={(e) => {
                  e.preventDefault();
                  navigate(`/c/${encodeURIComponent(r.info!.name)}`);
                }}
                data-active={hover?.channel === r.info!.name}
                title={`${r.info!.name} — ${r.info!.row.rows.toLocaleString("en-US")} records`}
              >
                <span className={styles.laneName}>{channelLabel(r.info!.name, r.group.plugin, r.group.channels.map((c) => c.name))}</span>
              </a>
            ),
          )}
          {/* lanes for collapsed groups are hidden; their density draws as a summary band */}
          {layout
            .filter((r) => r.kind === "group" && r.collapsed)
            .map((r, i) => (
              <span key={`sl${i}`} className={styles.summaryTag} style={{ top: r.y, height: r.h }} aria-hidden />
            ))}
        </div>

        {/* Plot: canvas marks + SVG overlay */}
        <div className={styles.plot} style={{ width: plotW, height: contentH }}>
          <canvas ref={canvasRef} className={styles.canvas} data-testid="loom-canvas" />
          <svg
            className={styles.overlay}
            data-testid="loom-overlay"
            role="img"
            aria-label={`The Loom: records per week for ${index.byName.size} channels across ${index.groups.length} sources, ${grid.weeks.length ? new Date(grid.domainFrom).getUTCFullYear() : ""} to ${grid.weeks.length ? new Date(grid.domainTo).getUTCFullYear() : ""}. Drag to set the time scope.`}
            width={plotW}
            height={contentH}
            onPointerDown={onDown}
            onPointerMove={onMove}
            onPointerUp={onUp}
            onPointerLeave={onLeave}
          >
            {/* context band shade */}
            {scale.pivotX !== null && (
              <rect x={0} y={0} width={scale.contextW} height={contentH} fill={tokens.ink3} opacity={0.04} />
            )}
            {/* future dim */}
            {nowX !== null && nowX < plotW && (
              <rect x={nowX} y={0} width={plotW - nowX} height={contentH} fill={tokens.bg0} opacity={0.35} />
            )}
            {/* year gridlines */}
            {yearTicks.map((t) => (
              <line key={t.ms} x1={t.x} y1={0} x2={t.x} y2={contentH} stroke={tokens.gridSoft} />
            ))}
            {/* break rule */}
            {breakX !== null && (
              <line x1={breakX} y1={0} x2={breakX} y2={contentH} stroke={tokens.line} strokeDasharray="2 3" opacity={0.6} />
            )}
            {/* active scope band */}
            {scopeBand && (
              <rect x={scopeBand.x} y={0} width={scopeBand.w} height={contentH} fill={tokens.brass} opacity={0.1} />
            )}
            {/* now-rule */}
            {nowX !== null && (
              <>
                <line x1={nowX} y1={0} x2={nowX} y2={contentH} stroke={tokens.brass} strokeWidth={1.25} />
                <circle cx={nowX} cy={0} r={2.5} fill={tokens.brass} />
              </>
            )}
            {/* hover crosshair */}
            {hover && !drag && (
              <line x1={hover.x} y1={0} x2={hover.x} y2={contentH} stroke={tokens.ink2} strokeWidth={1} opacity={0.5} strokeDasharray="2 2" />
            )}
            {/* brush */}
            {drag && (
              <rect
                x={Math.min(drag.x0, drag.x1)}
                y={0}
                width={Math.abs(drag.x1 - drag.x0)}
                height={contentH}
                fill={tokens.brass}
                opacity={0.16}
                stroke={tokens.brass}
              />
            )}
          </svg>

          {hover && hover.channel && hover.count > 0 && !drag && (
            <div
              className={styles.tip}
              style={{ left: clamp(hover.x + 10, 0, plotW - 150), top: 4 }}
            >
              <div className={styles.tipCh}>{hover.channel}</div>
              <div className={styles.tipRow}>
                <span>{weekLabel(hover.weekMs)}</span>
                <strong>{hover.count.toLocaleString("en-US")}</strong>
              </div>
            </div>
          )}
        </div>
      </div>

      {grid.loading && <div className={styles.loadingBar} aria-hidden />}
    </div>
  );
}

/* ---- Layout -------------------------------------------------------------- */

/** Collapse plugins that have many individually-minor channels, by default. */
function autoCollapsed(index: ChannelIndex): Set<string> {
  const out = new Set<string>();
  const total = index.totalRows || 1;
  for (const g of index.groups) {
    if (g.channels.length < AUTO_COLLAPSE_MIN) continue;
    const maxRows = g.channels.reduce((m, c) => Math.max(m, c.row.rows || 0), 0);
    if (maxRows < AUTO_COLLAPSE_SHARE * total) out.add(g.plugin);
  }
  return out;
}

function computeLayout(
  groups: PluginGroup[],
  collapsed: ReadonlySet<string>,
  bodyH: number,
): { layout: LayoutRow[]; contentH: number; laneH: number } {
  let visibleLanes = 0;
  for (const g of groups) if (!collapsed.has(g.plugin)) visibleLanes += g.channels.length;
  const avail = bodyH - groups.length * GROUP_H;
  const laneH = clamp(visibleLanes > 0 ? Math.floor(avail / visibleLanes) : MAX_LH, MIN_LH, MAX_LH);

  const layout: LayoutRow[] = [];
  let y = 0;
  for (const g of groups) {
    const isCollapsed = collapsed.has(g.plugin);
    layout.push({ kind: "group", y, h: GROUP_H, group: g, collapsed: isCollapsed });
    y += GROUP_H;
    if (!isCollapsed) {
      for (const info of g.channels) {
        layout.push({ kind: "lane", y, h: laneH, group: g, info, collapsed: false });
        y += laneH;
      }
    }
  }
  return { layout, contentH: y, laneH };
}

/* ---- Canvas drawing ------------------------------------------------------ */

function ensureBuffer(
  ref: React.MutableRefObject<HTMLCanvasElement | null>,
  w: number,
  h: number,
  dpr: number,
): HTMLCanvasElement {
  let c = ref.current;
  if (!c) {
    c = document.createElement("canvas");
    ref.current = c;
  }
  c.width = Math.max(1, Math.floor(w * dpr));
  c.height = Math.max(1, Math.floor(h * dpr));
  return c;
}

interface DrawLoomCtx {
  grid: LoomGrid;
  layout: LayoutRow[];
  plotW: number;
  contentH: number;
  dpr: number;
  rgbFor: (plugin: string, channelIndex?: number, alpha?: number) => string;
  tokens: ReturnType<typeof chartTokens>;
  scale: LoomScale;
  now: number;
}

function drawLoom(buf: HTMLCanvasElement, c: DrawLoomCtx): void {
  const ctx = buf.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(c.dpr, 0, 0, c.dpr, 0, 0);
  ctx.clearRect(0, 0, c.plotW, c.contentH);
  const logMax = Math.log1p(c.grid.globalMax);
  const weeks = c.grid.weeks;

  // Precompute week pixel edges under the scale.
  const edges = new Float64Array(weeks.length + 1);
  for (let i = 0; i < weeks.length; i++) edges[i] = c.scale.toX(weeks[i] ?? 0);
  edges[weeks.length] = c.scale.toX(c.grid.domainTo);

  const drawBand = (arr: ArrayLike<number> | undefined, plugin: string, channelIndex: number, top: number, h: number, bandMax: number): void => {
    if (!arr) return;
    ctx.fillStyle = c.rgbFor(plugin, channelIndex);
    const lmax = Math.log1p(bandMax) || 1;
    for (let i = 0; i < weeks.length; i++) {
      const n = arr[i] ?? 0;
      if (n <= 0) continue;
      const x0 = edges[i] ?? 0;
      const x1 = edges[i + 1] ?? x0;
      const w = Math.max(1, x1 - x0);
      const a = 0.16 + 0.84 * (Math.log1p(n) / lmax);
      ctx.globalAlpha = a;
      ctx.fillRect(Math.floor(x0), top, Math.ceil(w), h);
    }
    ctx.globalAlpha = 1;
  };

  for (const r of c.layout) {
    const top = r.y + LANE_PAD;
    const h = Math.max(1, r.h - LANE_PAD * 2);

    if (r.kind === "group") {
      if (!r.collapsed) continue;
      // Collapsed group: draw its aggregated per-week density as one summary band.
      const arr = c.grid.pluginCounts.get(r.group.plugin);
      let max = 1;
      if (arr) for (let i = 0; i < arr.length; i++) if ((arr[i] ?? 0) > max) max = arr[i] ?? 0;
      drawBand(arr, r.group.plugin, 0, top, h, max);
      continue;
    }

    if (!r.info) continue;
    // faint lane rule so empty lanes read as present
    ctx.fillStyle = c.tokens.gridSoft;
    ctx.fillRect(0, r.y + r.h - 0.5, c.plotW, 0.5);
    // coverage hatch outside covered span
    hatchUncovered(ctx, r.info, top, h, c);
    const arr = c.grid.counts.get(r.info.name);
    drawBand(arr, r.info.plugin, r.info.channelIndex, top, h, c.grid.globalMax);
  }

  void logMax;
}

function hatchUncovered(
  ctx: CanvasRenderingContext2D,
  info: ChannelInfo,
  top: number,
  h: number,
  c: DrawLoomCtx,
): void {
  const cov = info.row.coverage;
  if (!cov) return;
  const from = cov.covered_from ? Date.parse(cov.covered_from) : NaN;
  const to = cov.covered_to ? Date.parse(cov.covered_to) : NaN;
  // Only hatch the span between first data and now that the source does NOT cover.
  const firstMs = info.row.first_at ? Date.parse(info.row.first_at) : c.grid.domainFrom;
  const segs: [number, number][] = [];
  if (!Number.isNaN(from) && from > firstMs) segs.push([firstMs, from]);
  if (!Number.isNaN(to) && to < c.now) segs.push([to, c.now]);
  if (!segs.length) return;
  ctx.save();
  ctx.strokeStyle = c.tokens.ink3;
  ctx.globalAlpha = 0.28;
  ctx.lineWidth = 0.6;
  for (const [a, b] of segs) {
    const x0 = Math.max(0, c.scale.toX(a));
    const x1 = Math.min(c.plotW, c.scale.toX(b));
    if (x1 - x0 < 1) continue;
    ctx.save();
    ctx.beginPath();
    ctx.rect(x0, top, x1 - x0, h);
    ctx.clip();
    for (let x = x0 - h; x < x1; x += 5) {
      ctx.beginPath();
      ctx.moveTo(x, top + h);
      ctx.lineTo(x + h, top);
      ctx.stroke();
    }
    ctx.restore();
  }
  ctx.restore();
}

interface DrawRiverCtx {
  grid: LoomGrid;
  index: ChannelIndex;
  plotW: number;
  contentH: number;
  dpr: number;
  rgbFor: (plugin: string, channelIndex?: number, alpha?: number) => string;
  scale: LoomScale;
  tokens: ReturnType<typeof chartTokens>;
  fill: RiverFill;
}

function drawRiver(buf: HTMLCanvasElement, c: DrawRiverCtx): void {
  const ctx = buf.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(c.dpr, 0, 0, c.dpr, 0, 0);
  ctx.clearRect(0, 0, c.plotW, c.contentH);

  // Heaviest plugins first so small ones are not lost in an insideOut order.
  const plugins = c.index.groups.map((g) => g.plugin);
  const weeks = c.grid.weeks;
  if (!weeks.length || !plugins.length) return;

  // sqrt compresses a dominant plugin so every band stays legible.
  const shape = c.fill === "share" ? (v: number): number => v : (v: number): number => Math.sqrt(v);
  const rows = weeks.map((_, i) => {
    const o: Record<string, number> = {};
    for (const g of c.index.groups) o[g.plugin] = shape(c.grid.pluginCounts.get(g.plugin)?.[i] ?? 0);
    return o;
  });

  const series = stack<Record<string, number>>()
    .keys(plugins)
    .offset(c.fill === "share" ? stackOffsetExpand : stackOffsetWiggle)
    .order(stackOrderInsideOut)(rows);

  let lo = Infinity;
  let hi = -Infinity;
  for (const s of series)
    for (const p of s) {
      if (p[0] < lo) lo = p[0];
      if (p[1] > hi) hi = p[1];
    }
  if (!Number.isFinite(lo) || hi <= lo) return;
  const padY = 8;
  const span = hi - lo || 1;
  const yScale = (v: number): number => clamp(padY + ((hi - v) / span) * (c.contentH - padY * 2), 0, c.contentH);
  const xAt = (i: number): number => c.scale.toX(weeks[i] ?? c.grid.domainFrom);

  const gen = area<[number, number]>()
    .x((_d, i) => xAt(i))
    .y0((d) => yScale(d[0]))
    .y1((d) => yScale(d[1]))
    .curve(curveBasis);

  // Draw bands, and record each band's thickest point for an inline label.
  interface Lbl { plugin: string; x: number; y: number; thick: number }
  const labels: Lbl[] = [];
  series.forEach((s, k) => {
    const plugin = plugins[k];
    if (!plugin) return;
    const path = new Path2D(gen(s as unknown as [number, number][]) ?? "");
    ctx.fillStyle = c.rgbFor(plugin, 0, 0.92);
    ctx.fill(path);
    ctx.lineWidth = 0.5;
    ctx.strokeStyle = c.rgbFor(plugin, 0, 1);
    ctx.globalAlpha = 0.5;
    ctx.stroke(path);
    ctx.globalAlpha = 1;

    let best = -1;
    let bestThick = 0;
    for (let i = 0; i < s.length; i++) {
      const p = s[i] as unknown as [number, number];
      const thick = yScale(p[0]) - yScale(p[1]);
      if (thick > bestThick) {
        bestThick = thick;
        best = i;
      }
    }
    if (best >= 0 && bestThick >= 15) {
      const p = s[best] as unknown as [number, number];
      labels.push({ plugin, x: xAt(best), y: (yScale(p[0]) + yScale(p[1])) / 2, thick: bestThick });
    }
  });

  // Inline band labels (only where the band is thick enough to hold text).
  ctx.font = `600 11px var(--font-sans), system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const l of labels) {
    const w = ctx.measureText(l.plugin).width;
    const x = clamp(l.x, w / 2 + 4, c.plotW - w / 2 - 4);
    ctx.lineJoin = "round";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,0.45)";
    ctx.strokeText(l.plugin, x, l.y);
    ctx.fillStyle = "rgba(255,255,255,0.96)";
    ctx.fillText(l.plugin, x, l.y);
  }
}

function paint(
  canvas: HTMLCanvasElement | null,
  loom: HTMLCanvasElement | null,
  river: HTMLCanvasElement | null,
  blend: number,
  w: number,
  h: number,
  dpr: number,
): void {
  if (!canvas) return;
  canvas.width = Math.max(1, Math.floor(w * dpr));
  canvas.height = Math.max(1, Math.floor(h * dpr));
  canvas.style.width = `${w}px`;
  canvas.style.height = `${h}px`;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (loom && blend < 1) {
    ctx.globalAlpha = 1 - blend;
    ctx.drawImage(loom, 0, 0);
  }
  if (river && blend > 0) {
    ctx.globalAlpha = blend;
    ctx.drawImage(river, 0, 0);
  }
  ctx.globalAlpha = 1;
}

function BreakGlyph({ x, h, color }: { x: number; h: number; color: string }): ReactNode {
  // A small double-slash to signal the compressed-time break.
  const mid = h / 2;
  return (
    <g stroke={color} strokeWidth={1} opacity={0.7} fill="none">
      <line x1={x - 3} y1={mid - 4} x2={x + 1} y2={mid + 4} />
      <line x1={x + 1} y1={mid - 4} x2={x + 5} y2={mid + 4} />
    </g>
  );
}

/* ---- small helpers ------------------------------------------------------- */

interface YearTick {
  ms: number;
  year: number;
  x: number;
  label: boolean;
}

/** Year ticks placed under the scale, labelling only those that will not overlap. */
function labelledYearTicks(from: number, to: number, scale: LoomScale): YearTick[] {
  const out: YearTick[] = [];
  const y0 = new Date(from).getUTCFullYear();
  const y1 = new Date(to).getUTCFullYear();
  // Years inside the compressed context band are summarised by the band's own
  // range label; labelling them individually only crowds the axis.
  const labelFrom = scale.pivotX !== null ? scale.contextW + 2 : 0;
  let lastLabelX = -Infinity;
  for (let y = y0; y <= y1; y++) {
    const ms = Date.UTC(y, 0, 1);
    if (ms < from || ms > to) continue;
    const x = scale.toX(ms);
    // Only label a year when it clears the context band and has room since the last.
    const label = x >= labelFrom && x - lastLabelX >= 30;
    if (label) lastLabelX = x;
    out.push({ ms, year: y, x, label });
  }
  return out;
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

function weekLabel(ms: number): string {
  const d = new Date(ms);
  return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}
