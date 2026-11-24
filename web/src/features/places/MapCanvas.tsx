/*
 * The full-bleed Places map: a hand-drawn Canvas 2D instrument with wheel/drag
 * pan-zoom and +/- keys, projecting world → city through one Web-Mercator
 * viewport (projection.ts). Bundled Natural Earth land gives context when zoomed
 * out and fades as you descend, where the track itself becomes the map — backed
 * then by a faint graticule and a kilometre scale bar so distance stays legible.
 * No map library, no worker, no remote tiles — the origin never makes an outside
 * call. Three layers (track, places, heat) compose; both themes; DPR-aware;
 * keyboard reachable; honours reduced motion (it never animates — every change
 * is a direct repaint).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useChartColors } from "@/charts";
import { oklchStringToRgb } from "@/charts/primitives/colors";
import { useCanvas, useElementSize } from "@/charts/runtime/hooks";
import { landRings } from "./land";
import {
  clampViewport,
  densestBounds,
  fitBounds,
  hexbin,
  lonLatToWorld,
  metersPerPixel,
  niceRound,
  panBy,
  project,
  robustBounds,
  segmentByDay,
  unproject,
  worldToLonLat,
  zoomAt,
  zoomLevel,
  type Bounds,
  type DaySegment,
  type LonLat,
  type TrackPoint,
  type Viewport,
} from "./projection";
import styles from "./places.module.css";

export interface PlaceMark extends LonLat {
  id: number;
  name: string;
  records: number;
  address?: string | null;
}
export interface VisitMark extends LonLat {
  dwellS: number;
  at: number;
}

export type TrackColorMode = "recency" | "source";
export interface MapLayers {
  track: boolean;
  places: boolean;
  heat: boolean;
}

interface Props {
  track: TrackPoint[];
  visits: VisitMark[];
  places: PlaceMark[];
  layers: MapLayers;
  trackColor: TrackColorMode;
  plugin: string;
  tz: string;
  selectedId: number | null;
  /** When set (a side-list pick), the map centres on this point once. */
  focus?: (LonLat & { nonce: number }) | null;
  onSelectPlace: (p: PlaceMark | null) => void;
  onPickPoint?: (ll: LonLat) => void;
}

const LAND_FADE_START = 5.5;
const LAND_FADE_END = 8.5;

export default function MapCanvas(props: Props) {
  const { track, visits, places, layers, trackColor, plugin, tz, selectedId, focus } = props;
  const c = useChartColors(plugin);
  const dark = c.theme !== "daybook";
  const [hostRef, size] = useElementSize<HTMLDivElement>();
  const [view, setView] = useState<Viewport | null>(null);
  const [hover, setHover] = useState<{ x: number; y: number; mark: PlaceMark } | null>(null);
  const placeHitsRef = useRef<{ x: number; y: number; r: number; mark: PlaceMark }[]>([]);
  const dragRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const userMoved = useRef(false);
  const lastFocus = useRef(0);

  const segments: DaySegment[] = segmentByDay(track, tz);

  // Home frame: the densest cluster of dwell — the city where time is actually
  // spent — so a trip abroad does not shrink it to a dot. Fall back to the
  // track, then the world.
  const homeBounds = useCallback((): Bounds => {
    const dwell: LonLat[] = [...visits, ...places];
    return (
      densestBounds(dwell.length >= 5 ? dwell : track) ??
      robustBounds(track, 0.02) ?? { west: -170, east: 170, south: -75, north: 78 }
    );
  }, [visits, places, track]);

  // Fit-all frame: every meaningful position (outliers trimmed), for the ⊡ key.
  const allBounds = useCallback((): Bounds => {
    return robustBounds([...visits, ...places, ...track], 0.03) ?? homeBounds();
  }, [visits, places, track, homeBounds]);

  // Auto-frame as data streams in (visits/places arrive after the track), until
  // the viewer pans or zooms — then their view is left alone.
  useEffect(() => {
    if (size.width < 2 || size.height < 2) return;
    if (userMoved.current) {
      setView((v) => (v ? clampViewport({ ...v, width: size.width, height: size.height }) : v));
      return;
    }
    if (track.length + places.length + visits.length === 0) return;
    setView(fitBounds(homeBounds(), size.width, size.height, 64));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size.width, size.height, track.length, places.length, visits.length]);

  // Centre on a place picked from the side list (fly-to without re-fitting).
  useEffect(() => {
    if (!focus || focus.nonce === lastFocus.current) return;
    lastFocus.current = focus.nonce;
    if (size.width < 2) return;
    setView((v) => {
      const base = v ?? fitBounds(homeBounds(), size.width, size.height, 64);
      userMoved.current = true;
      const [cxWorld, cyWorld] = lonLatToWorld(focus.lon, focus.lat);
      const k = Math.max(base.k, Math.min(1 << 20, Math.max(size.width, size.height) * 900));
      return clampViewport({ ...base, cx: cxWorld, cy: cyWorld, k });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.nonce]);

  const resetView = useCallback(
    (all = false) => {
      if (size.width < 2) return;
      userMoved.current = all; // a plain "home" reset re-enables auto-frame
      setView(fitBounds(all ? allBounds() : homeBounds(), size.width, size.height, 64));
    },
    [size.width, size.height, homeBounds, allBounds],
  );

  const ramp = c.sequential(9).map(oklchStringToRgb);
  const sourceLine = c.sourceRgb(0, 1);

  const canvasRef = useCanvas(
    (ctx, box) => {
      if (!view) return;
      const v = view;
      ctx.fillStyle = c.tokens.bg1;
      ctx.fillRect(0, 0, box.width, box.height);

      // Land, fading out as the city fills the frame.
      const zl = zoomLevel(v);
      const landAlpha = zl <= LAND_FADE_START ? 1 : zl >= LAND_FADE_END ? 0 : 1 - (zl - LAND_FADE_START) / (LAND_FADE_END - LAND_FADE_START);
      if (landAlpha > 0.01) {
        ctx.save();
        ctx.globalAlpha = landAlpha;
        ctx.fillStyle = c.tokens.bg3;
        ctx.strokeStyle = c.tokens.line;
        ctx.lineWidth = 0.6;
        ctx.beginPath();
        for (const ring of landRings()) {
          let started = false;
          for (const [lon, lat] of ring) {
            const [x, y] = project(v, lon as number, lat as number);
            if (x < -2000 || x > box.width + 2000 || y < -2000 || y > box.height + 2000) {
              started = false;
              continue;
            }
            if (!started) {
              ctx.moveTo(x, y);
              started = true;
            } else ctx.lineTo(x, y);
          }
        }
        ctx.fill();
        ctx.stroke();
        ctx.restore();
      }

      // A faint graticule appears as the land fades, so distance keeps a frame.
      if (landAlpha < 0.6) drawGraticule(ctx, v, box, c.tokens.line, 0.5 * (1 - landAlpha));

      // Heat: hex-bin density of the track (and visit arrivals).
      if (layers.heat) {
        const pts: [number, number][] = [];
        for (const p of track) pts.push(project(v, p.lon, p.lat));
        for (const p of visits) pts.push(project(v, p.lon, p.lat));
        const bins = hexbin(pts, 13);
        const maxC = bins.reduce((m, b) => Math.max(m, b.count), 1);
        const R = 13;
        for (const b of bins) {
          const t = Math.min(0.999, Math.log1p(b.count) / Math.log1p(maxC));
          ctx.fillStyle = ramp[Math.floor(t * (ramp.length - 1))]!;
          ctx.globalAlpha = 0.18 + 0.52 * t;
          hexPath(ctx, b.x, b.y, R * 0.98);
          ctx.fill();
        }
        ctx.globalAlpha = 1;
      }

      // Track: one polyline per day, coloured by recency or the source hue,
      // drawn with additive (dark) / multiply (light) blending so overlapping
      // days accumulate into weight — the route reads as a glowing density, and
      // a dot at every sampled point keeps even isolated days visible.
      if (layers.track && segments.length) {
        ctx.save();
        ctx.globalCompositeOperation = dark ? "lighter" : "multiply";
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        const lw = zl < 7 ? 1 : zl < 10 ? 1.3 : 1.7;
        for (const seg of segments) {
          const colour = trackColor === "recency" ? ramp[Math.floor(seg.recency * (ramp.length - 1))]! : sourceLine;
          ctx.strokeStyle = colour;
          ctx.fillStyle = colour;
          const base = trackColor === "recency" ? (dark ? 0.1 + 0.26 * seg.recency : 0.14 + 0.28 * seg.recency) : dark ? 0.16 : 0.26;
          ctx.globalAlpha = base;
          ctx.lineWidth = lw;
          ctx.beginPath();
          let px = NaN;
          let py = NaN;
          seg.points.forEach((p, i) => {
            const [x, y] = project(v, p.lon, p.lat);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
            px = x;
            py = y;
          });
          ctx.stroke();
          // A terminal dot anchors single-point / sparse days.
          if (seg.points.length <= 2 && Number.isFinite(px)) {
            ctx.globalAlpha = Math.min(0.9, base + 0.25);
            ctx.beginPath();
            ctx.arc(px, py, lw * 0.9, 0, Math.PI * 2);
            ctx.fill();
          }
        }
        ctx.restore();
        ctx.globalAlpha = 1;
      }

      // Visit dwell blooms (soft, under the named places).
      if (layers.places && visits.length) {
        const maxDwell = visits.reduce((m, p) => Math.max(m, p.dwellS), 1);
        ctx.fillStyle = c.sourceRgb(0, 0.14);
        for (const p of visits) {
          const [x, y] = project(v, p.lon, p.lat);
          const r = 3 + 10 * Math.sqrt(Math.min(1, p.dwellS / maxDwell));
          ctx.beginPath();
          ctx.arc(x, y, r, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // Named places: circles sized by records, the selected one in brass.
      placeHitsRef.current = [];
      if (layers.places && places.length) {
        const maxRec = places.reduce((m, p) => Math.max(m, p.records), 1);
        const ordered = [...places].sort((a, b) => b.records - a.records);
        ctx.font = `600 11px ${c.tokens.fontUi}`;
        ctx.textBaseline = "middle";
        const onScreen: { p: PlaceMark; x: number; y: number; r: number }[] = [];
        for (const p of ordered) {
          const [x, y] = project(v, p.lon, p.lat);
          if (x < -40 || x > box.width + 40 || y < -40 || y > box.height + 40) continue;
          const r = 3.5 + 13 * Math.sqrt(p.records / maxRec);
          const selected = p.id === selectedId;
          ctx.beginPath();
          ctx.arc(x, y, r, 0, Math.PI * 2);
          ctx.fillStyle = selected ? c.tokens.brass : c.sourceRgb(0, 0.72);
          ctx.fill();
          ctx.lineWidth = selected ? 2 : 1;
          ctx.strokeStyle = selected ? c.tokens.brass : c.tokens.bg0;
          ctx.stroke();
          placeHitsRef.current.push({ x, y, r, mark: p });
          onScreen.push({ p, x, y, r });
        }
        // Greedy, collision-free labels: strongest places first, a label kept
        // only where its box clears the ones already placed. Dropped names are
        // revealed on hover, and the selected place is always labelled.
        const placed: { x0: number; y0: number; x1: number; y1: number }[] = [];
        const drawLabel = (name: string, x: number, y: number, r: number, selected: boolean) => {
          const text = clip(name);
          const w = ctx.measureText(text).width;
          const box0 = { x0: x + r + 2, y0: y - 8, x1: x + r + 6 + w, y1: y + 8 };
          ctx.fillStyle = c.tokens.bg0;
          ctx.globalAlpha = 0.66;
          ctx.fillRect(box0.x0, box0.y0 + 1, w + 5, 14);
          ctx.globalAlpha = 1;
          ctx.fillStyle = selected ? c.tokens.brass : c.tokens.ink;
          ctx.fillText(text, x + r + 4, y);
          placed.push(box0);
        };
        const collides = (x: number, y: number, r: number, name: string) => {
          const w = ctx.measureText(clip(name)).width;
          const b = { x0: x + r + 2, y0: y - 8, x1: x + r + 6 + w, y1: y + 8 };
          return placed.some((q) => b.x0 < q.x1 && b.x1 > q.x0 && b.y0 < q.y1 && b.y1 > q.y0);
        };
        for (const { p, x, y, r } of onScreen) {
          if (x < 0 || x > box.width || y < 0 || y > box.height) continue;
          if (p.id === selectedId) continue; // drawn last, always
          if (collides(x, y, r, p.name)) continue;
          drawLabel(p.name, x, y, r, false);
        }
        const sel = onScreen.find((o) => o.p.id === selectedId);
        if (sel) drawLabel(sel.p.name, sel.x, sel.y, sel.r, true);
      }

      // Scale bar (lower-left) + attribution (lower-right).
      drawScaleBar(ctx, v, box, c.tokens.ink2, c.tokens.fontUi);
      ctx.fillStyle = c.tokens.ink3;
      ctx.font = `9px ${c.tokens.fontUi}`;
      ctx.textBaseline = "bottom";
      ctx.textAlign = "right";
      ctx.fillText("Natural Earth · no street tiles", box.width - 6, box.height - 5);
      ctx.textAlign = "left";
    },
    size,
    [view, c.tokens, c.theme, dark, segments, visits, places, layers.track, layers.places, layers.heat, trackColor, selectedId],
  );

  // --- interaction ---------------------------------------------------------
  const onWheel = (e: React.WheelEvent) => {
    if (!view) return;
    userMoved.current = true;
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const factor = Math.pow(1.0015, -e.deltaY);
    setView(zoomAt(view, e.clientX - host.left, e.clientY - host.top, factor));
  };
  const onPointerDown = (e: React.PointerEvent) => {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    dragRef.current = { x: e.clientX, y: e.clientY, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - host.left;
    const py = e.clientY - host.top;
    if (dragRef.current && view) {
      const dx = e.clientX - dragRef.current.x;
      const dy = e.clientY - dragRef.current.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) {
        dragRef.current.moved = true;
        userMoved.current = true;
      }
      dragRef.current.x = e.clientX;
      dragRef.current.y = e.clientY;
      setView((vv) => (vv ? panBy(vv, dx, dy) : vv));
      return;
    }
    let best: { x: number; y: number; r: number; mark: PlaceMark } | null = null;
    let bestD = Infinity;
    for (const h of placeHitsRef.current) {
      const d = Math.hypot(px - h.x, py - h.y);
      if (d < h.r + 7 && d < bestD) {
        bestD = d;
        best = h;
      }
    }
    setHover(best ? { x: px, y: py, mark: best.mark } : null);
  };
  const endDrag = (e: React.PointerEvent) => {
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    dragRef.current = null;
  };
  const onClick = (e: React.MouseEvent) => {
    if (dragRef.current?.moved) return;
    if (hover) {
      props.onSelectPlace(hover.mark);
      return;
    }
    if (view && props.onPickPoint) {
      const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
      props.onPickPoint(unproject(view, e.clientX - host.left, e.clientY - host.top));
    }
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!view) return;
    if (e.key !== "0") userMoved.current = true;
    const cxp = size.width / 2;
    const cyp = size.height / 2;
    if (e.key === "+" || e.key === "=") setView(zoomAt(view, cxp, cyp, 1.3));
    else if (e.key === "-" || e.key === "_") setView(zoomAt(view, cxp, cyp, 1 / 1.3));
    else if (e.key === "ArrowLeft") setView(panBy(view, 60, 0));
    else if (e.key === "ArrowRight") setView(panBy(view, -60, 0));
    else if (e.key === "ArrowUp") setView(panBy(view, 0, 60));
    else if (e.key === "ArrowDown") setView(panBy(view, 0, -60));
    else if (e.key === "0") resetView(false);
    else return;
    e.preventDefault();
  };

  return (
    <div
      ref={hostRef}
      className={styles.mapHost}
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onClick={onClick}
      onKeyDown={onKeyDown}
      tabIndex={0}
      role="application"
      aria-label="Map of places and track. Arrow keys pan, plus and minus zoom, zero fits the home city."
    >
      <canvas ref={canvasRef} className={styles.mapCanvas} style={{ cursor: hover ? "pointer" : dragRef.current ? "grabbing" : "grab" }} />
      <div className={styles.mapControls}>
        <button type="button" className={styles.mapBtn} onClick={(e) => { e.stopPropagation(); if (view) { userMoved.current = true; setView(zoomAt(view, size.width / 2, size.height / 2, 1.4)); } }} aria-label="Zoom in">+</button>
        <button type="button" className={styles.mapBtn} onClick={(e) => { e.stopPropagation(); if (view) { userMoved.current = true; setView(zoomAt(view, size.width / 2, size.height / 2, 1 / 1.4)); } }} aria-label="Zoom out">−</button>
        <button type="button" className={styles.mapBtn} onClick={(e) => { e.stopPropagation(); resetView(false); }} aria-label="Fit home city" title="Home city">⌂</button>
        <button type="button" className={styles.mapBtn} onClick={(e) => { e.stopPropagation(); resetView(true); }} aria-label="Fit all places" title="Fit all">⊡</button>
      </div>
      {hover && (
        <div className={styles.mapTip} style={{ left: Math.min(hover.x + 12, size.width - 170), top: Math.min(hover.y + 12, size.height - 60) }}>
          <strong>{hover.mark.name}</strong>
          <span>{hover.mark.records.toLocaleString("en-US")} records here</span>
        </div>
      )}
    </div>
  );
}

function drawGraticule(ctx: CanvasRenderingContext2D, v: Viewport, box: { width: number; height: number }, line: string, alpha: number) {
  if (alpha <= 0.02) return;
  const tl = worldToLonLat((0 - v.width / 2) / v.k + v.cx, (0 - v.height / 2) / v.k + v.cy);
  const br = worldToLonLat((box.width - v.width / 2) / v.k + v.cx, (box.height - v.height / 2) / v.k + v.cy);
  const lonStep = niceRound(((br.lon - tl.lon) || 1e-6) / 5);
  const latStep = niceRound(((tl.lat - br.lat) || 1e-6) / 5);
  if (!(lonStep > 0) || !(latStep > 0)) return;
  ctx.save();
  ctx.strokeStyle = line;
  ctx.globalAlpha = Math.min(0.5, alpha);
  ctx.lineWidth = 0.5;
  ctx.beginPath();
  for (let lon = Math.ceil(tl.lon / lonStep) * lonStep; lon < br.lon; lon += lonStep) {
    const [x] = project(v, lon, tl.lat);
    ctx.moveTo(x, 0);
    ctx.lineTo(x, box.height);
  }
  for (let lat = Math.ceil(br.lat / latStep) * latStep; lat < tl.lat; lat += latStep) {
    const [, y] = project(v, tl.lon, lat);
    ctx.moveTo(0, y);
    ctx.lineTo(box.width, y);
  }
  ctx.stroke();
  ctx.restore();
}

function drawScaleBar(ctx: CanvasRenderingContext2D, v: Viewport, box: { width: number; height: number }, ink: string, font: string) {
  const mpp = metersPerPixel(v);
  if (!(mpp > 0) || !Number.isFinite(mpp)) return;
  const meters = niceRound(mpp * 96);
  if (!(meters > 0)) return;
  const px = meters / mpp;
  const x0 = 14;
  const y0 = box.height - 16;
  ctx.save();
  ctx.strokeStyle = ink;
  ctx.fillStyle = ink;
  ctx.lineWidth = 1.25;
  ctx.globalAlpha = 0.9;
  ctx.beginPath();
  ctx.moveTo(x0, y0 - 4);
  ctx.lineTo(x0, y0);
  ctx.lineTo(x0 + px, y0);
  ctx.lineTo(x0 + px, y0 - 4);
  ctx.stroke();
  ctx.globalAlpha = 1;
  ctx.font = `10px ${font}`;
  ctx.textBaseline = "bottom";
  ctx.textAlign = "left";
  const label = meters >= 1000 ? `${(meters / 1000).toLocaleString("en-US")} km` : `${meters} m`;
  ctx.fillText(label, x0, y0 - 5);
  ctx.restore();
}

function hexPath(ctx: CanvasRenderingContext2D, cx: number, cy: number, r: number) {
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 180) * (60 * i - 90);
    const x = cx + r * Math.cos(a);
    const y = cy + r * Math.sin(a);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
}

function clip(s: string, n = 22): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
