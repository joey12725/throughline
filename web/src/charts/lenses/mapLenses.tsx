/*
 * Maps (track_map, pin_map) on hand-written Canvas 2D, lazily loaded. There is NO
 * map library, NO worker and NO remote tiles: the base is a flat background plus
 * bundled Natural Earth land (from the installed world-atlas package) projected
 * equirectangularly; the track, the pins and their labels come from the rows
 * themselves. Street-level tiles are an explicit opt-in that stays off here, so
 * the origin never makes an outside request. Both themes; DPR-aware.
 *
 * (MapLibre GL was the brief's choice, but its render worker is blocked by the
 * shell's strict Content-Security-Policy — worker-src — so a dependency-free
 * canvas map is both safer against the no-egress rule and lighter.)
 */
import { useRef, useState, type ReactNode } from "react";
import { feature } from "topojson-client";
import type { Feature, FeatureCollection, MultiPolygon, Polygon } from "geojson";
// eslint-disable-next-line @typescript-eslint/no-explicit-any
import landTopo from "world-atlas/land-110m.json";
import { col1, numAt, strAt, timeMsAt } from "../runtime/accessors";
import { clip, exactNumber, tooltipTime } from "../runtime/format";
import { useCanvas } from "../runtime/hooks";
import { useChartColors } from "../primitives/colors";
import { ChartBox, ChartEmpty, TruncationBanner } from "../primitives/parts";
import { ChartTooltip, type TipState } from "../primitives/Tooltip";
import type { LensRenderProps } from "../types";

let LAND: number[][][] | null = null;
/** Land as a flat list of rings ([lon,lat] pairs), computed once. */
function landRings(): number[][][] {
  if (LAND) return LAND;
  // land-110m's "land" object is a GeometryCollection, so feature() yields a
  // FeatureCollection; flatten every polygon of every feature to rings.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const out = feature(landTopo as any, (landTopo as any).objects.land) as unknown as
    | FeatureCollection<Polygon | MultiPolygon>
    | Feature<Polygon | MultiPolygon>;
  const feats = "features" in out ? out.features : [out];
  const rings: number[][][] = [];
  for (const f of feats) {
    const geom = f.geometry;
    if (!geom) continue;
    const polys = geom.type === "MultiPolygon" ? geom.coordinates : [geom.coordinates];
    for (const poly of polys) for (const ring of poly) rings.push(ring as number[][]);
  }
  LAND = rings;
  return rings;
}

interface Pt {
  lat: number;
  lon: number;
  t: number;
  size: number;
  label: string;
}

export default function MapLens(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const isTrack = p.spec.lens === "track_map";
  const latCol = col1(p.spec, "lat");
  const lonCol = col1(p.spec, "lon");
  const tCol = col1(p.spec, "t");
  const sizeCol = col1(p.spec, "size");
  const labelCol = col1(p.spec, "label");
  const [tip, setTip] = useState<TipState | null>(null);
  const screenRef = useRef<{ x: number; y: number; pt: Pt }[]>([]);
  const projRef = useRef<((lon: number, lat: number) => [number, number]) | null>(null);

  const pts: Pt[] = p.rows
    .map((r) => ({
      lat: numAt(r, latCol),
      lon: numAt(r, lonCol),
      t: tCol ? timeMsAt(r, tCol) : NaN,
      size: sizeCol ? numAt(r, sizeCol) : 1,
      label: labelCol ? strAt(r, labelCol) : "",
    }))
    .filter((d) => Number.isFinite(d.lat) && Number.isFinite(d.lon) && Math.abs(d.lat) <= 90 && Math.abs(d.lon) <= 180);
  if (pts.length === 0) return <ChartEmpty message="No positions to map" />;
  if (isTrack) pts.sort((a, b) => a.t - b.t);

  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  for (const d of pts) {
    w = Math.min(w, d.lon); e = Math.max(e, d.lon);
    s = Math.min(s, d.lat); n = Math.max(n, d.lat);
  }
  // Pad the bounds so marks are not on the edge.
  const padLon = Math.max((e - w) * 0.12, 0.01);
  const padLat = Math.max((n - s) * 0.12, 0.01);
  w -= padLon; e += padLon; s -= padLat; n += padLat;
  const midLat = (s + n) / 2;
  const cosMid = Math.max(0.2, Math.cos((midLat * Math.PI) / 180));
  const maxSize = Math.max(...pts.map((d) => d.size), 1);
  const bannerH = p.truncated ? 26 : 0;

  const canvasRef = useCanvas(
    (ctx, size) => {
      const pad = 6;
      const top = bannerH + pad;
      const plotW = size.width - pad * 2;
      const plotH = size.height - top - pad;
      const spanLon = (e - w) * cosMid;
      const spanLat = n - s;
      const k = Math.min(plotW / (spanLon || 1), plotH / (spanLat || 1));
      const offX = pad + (plotW - spanLon * k) / 2;
      const offY = top + (plotH - spanLat * k) / 2;
      const project = (lon: number, lat: number): [number, number] => [offX + (lon - w) * cosMid * k, offY + (n - lat) * k];
      projRef.current = project;

      // Water base + land.
      ctx.fillStyle = c.tokens.bg1;
      ctx.fillRect(0, top - pad, size.width, size.height);
      ctx.fillStyle = c.tokens.bg3;
      ctx.strokeStyle = c.tokens.line;
      ctx.lineWidth = 0.5;
      ctx.beginPath();
      for (const ring of landRings()) {
        let started = false;
        for (const [lon, lat] of ring) {
          const [x, y] = project(lon as number, lat as number);
          if (!started) { ctx.moveTo(x, y); started = true; }
          else ctx.lineTo(x, y);
        }
        ctx.closePath();
      }
      ctx.fill();
      ctx.stroke();

      if (isTrack) {
        // The track as a line, coloured by the source hue.
        ctx.strokeStyle = c.sourceRgb(0, 0.9);
        ctx.lineWidth = 1.6;
        ctx.lineJoin = "round";
        ctx.beginPath();
        pts.forEach((d, i) => {
          const [x, y] = project(d.lon, d.lat);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
        // Vertices.
        ctx.fillStyle = c.tokens.patina;
        for (const d of pts) {
          const [x, y] = project(d.lon, d.lat);
          ctx.beginPath();
          ctx.arc(x, y, 1.6, 0, Math.PI * 2);
          ctx.fill();
        }
        // Start (brass) and end (patina) markers.
        mark(ctx, project(pts[0]!.lon, pts[0]!.lat), c.tokens.brass, 4);
        mark(ctx, project(pts[pts.length - 1]!.lon, pts[pts.length - 1]!.lat), c.tokens.patina, 4);
        screenRef.current = [];
      } else {
        // Pins as circles sized by count; biggest drawn first so small sit on top.
        const order = [...pts].sort((a, b) => b.size - a.size);
        const screen: { x: number; y: number; pt: Pt }[] = [];
        for (const d of order) {
          const [x, y] = project(d.lon, d.lat);
          const r = 3 + 15 * Math.sqrt(d.size / maxSize);
          ctx.fillStyle = c.sourceRgb(0, 0.5);
          ctx.strokeStyle = c.tokens.bg0;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(x, y, r, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
          screen.push({ x, y, pt: d });
        }
        // Labels for the top few pins, skipping any that would collide.
        ctx.fillStyle = c.tokens.ink;
        ctx.font = `10px ${c.tokens.fontUi}`;
        ctx.textBaseline = "middle";
        const placed: [number, number][] = [];
        for (const d of order.slice(0, 8)) {
          if (!d.label) continue;
          const [x, y] = project(d.lon, d.lat);
          if (placed.some(([px, py]) => Math.abs(px - x) < 70 && Math.abs(py - y) < 13)) continue;
          const r = 3 + 15 * Math.sqrt(d.size / maxSize);
          ctx.fillText(clip(d.label, 18), x + r + 3, y);
          placed.push([x, y]);
        }
        screenRef.current = screen;
      }
      // Attribution.
      ctx.fillStyle = c.tokens.ink3;
      ctx.font = `9px ${c.tokens.fontUi}`;
      ctx.textBaseline = "bottom";
      ctx.textAlign = "right";
      ctx.fillText("Natural Earth", size.width - 4, size.height - 3);
      ctx.textAlign = "left";
    },
    { width: p.width, height: p.height },
    [c.tokens, pts, isTrack, maxSize, w, e, s, n, bannerH],
  );

  const onMove = (ev: React.PointerEvent): void => {
    if (isTrack) {
      // Nearest vertex tooltip.
      const host = (ev.currentTarget as HTMLElement).getBoundingClientRect();
      const px = ev.clientX - host.left;
      const py = ev.clientY - host.top;
      const proj = projRef.current;
      if (!proj) return;
      let best: Pt | null = null;
      let bestD = 10;
      for (const d of pts) {
        const [x, y] = proj(d.lon, d.lat);
        const dist = Math.hypot(px - x, py - y);
        if (dist < bestD) { bestD = dist; best = d; }
      }
      setTip(best ? { x: px, y: py, title: best.label || "position", rows: [{ label: "when", value: Number.isFinite(best.t) ? tooltipTime(best.t, p.tz ?? "UTC") : "—" }, { label: "lat, lon", value: `${best.lat.toFixed(4)}, ${best.lon.toFixed(4)}` }] } : null);
      return;
    }
    const host = (ev.currentTarget as HTMLElement).getBoundingClientRect();
    const px = ev.clientX - host.left;
    const py = ev.clientY - host.top;
    let best: { x: number; y: number; pt: Pt } | null = null;
    let bestD = 20;
    for (const sMark of screenRef.current) {
      const dist = Math.hypot(px - sMark.x, py - sMark.y);
      if (dist < bestD) { bestD = dist; best = sMark; }
    }
    setTip(best ? { x: px, y: py, title: best.pt.label || "place", rows: [{ label: "records", value: exactNumber(best.pt.size), swatch: c.source(0) }] } : null);
  };

  const onClick = (): void => {
    if (!tip || isTrack) return;
    const hit = screenRef.current.find((m) => m.pt.label === String(tip.title));
    if (hit) p.onDrill?.({ kind: "place", label: hit.pt.label, lat: hit.pt.lat, lon: hit.pt.lon });
  };

  return (
    <ChartBox width={p.width} height={p.height}>
      <TruncationBanner truncated={p.truncated} />
      <div style={{ position: "relative", width: p.width, height: p.height - bannerH, borderRadius: "var(--r-control)", overflow: "hidden" }} onPointerMove={onMove} onPointerLeave={() => setTip(null)} onClick={onClick}>
        <canvas ref={canvasRef} role="img" aria-label={isTrack ? "Track of positions over time" : "Places as sized circles"} style={{ cursor: isTrack ? "default" : "pointer" }} />
        <ChartTooltip tip={tip} width={p.width} height={p.height - bannerH} />
      </div>
    </ChartBox>
  );
}

function mark(ctx: CanvasRenderingContext2D, [x, y]: [number, number], color: string, r: number): void {
  ctx.fillStyle = color;
  ctx.strokeStyle = color;
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fill();
}
