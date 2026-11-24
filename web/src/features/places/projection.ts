/*
 * Pure map geometry for Places. No DOM, no React — all unit-tested.
 *
 * A Web-Mercator projection carries the map from world scale to city scale. The
 * world is the unit square [0,1]×[0,1] in "world units": lon -180..180 maps to
 * x 0..1, lat +85..-85 to y 0..1 (Mercator). A Viewport is a centre in world
 * units and a scale `k` (screen pixels across one world unit, i.e. the whole
 * globe). Everything downstream — land rings, track, pins, hex bins — projects
 * through the same two functions, so one wheel tick reframes the entire scene.
 */

export interface Viewport {
  /** world-unit centre, each in [0,1] */
  cx: number;
  cy: number;
  /** pixels spanning the whole world (one world unit) */
  k: number;
  /** viewport pixel size */
  width: number;
  height: number;
}

export interface LonLat {
  lon: number;
  lat: number;
}

export const MAX_LAT = 85.05112878; // Web-Mercator clamp

/** lon/lat → world unit square [0,1]². */
export function lonLatToWorld(lon: number, lat: number): [number, number] {
  const x = (lon + 180) / 360;
  const clamped = Math.max(-MAX_LAT, Math.min(MAX_LAT, lat));
  const s = Math.sin((clamped * Math.PI) / 180);
  const y = 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
  return [x, y];
}

/** world unit square → lon/lat (inverse of lonLatToWorld). */
export function worldToLonLat(x: number, y: number): LonLat {
  const lon = x * 360 - 180;
  const n = Math.PI * (1 - 2 * y);
  const lat = (Math.atan(Math.sinh(n)) * 180) / Math.PI;
  return { lon, lat };
}

/** world units → screen pixels, for the given viewport. */
export function worldToScreen(v: Viewport, x: number, y: number): [number, number] {
  return [(x - v.cx) * v.k + v.width / 2, (y - v.cy) * v.k + v.height / 2];
}

/** screen pixels → world units (inverse). */
export function screenToWorld(v: Viewport, sx: number, sy: number): [number, number] {
  return [(sx - v.width / 2) / v.k + v.cx, (sy - v.height / 2) / v.k + v.cy];
}

/** Convenience: lon/lat straight to screen. */
export function project(v: Viewport, lon: number, lat: number): [number, number] {
  const [x, y] = lonLatToWorld(lon, lat);
  return worldToScreen(v, x, y);
}

/** screen → lon/lat (for click-to-locate). */
export function unproject(v: Viewport, sx: number, sy: number): LonLat {
  const [x, y] = screenToWorld(v, sx, sy);
  return worldToLonLat(x, y);
}

/** A robust percentile of a sorted-or-unsorted number list (linear interp). */
export function percentile(values: number[], p: number): number {
  if (values.length === 0) return NaN;
  const s = [...values].sort((a, b) => a - b);
  const idx = (s.length - 1) * Math.max(0, Math.min(1, p));
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return s[lo]!;
  return s[lo]! + (s[hi]! - s[lo]!) * (idx - lo);
}

export interface Bounds {
  west: number;
  east: number;
  south: number;
  north: number;
}

/**
 * Bounds of a set of positions, trimmed to the central mass so a handful of
 * far-away outliers (a default (0,0), a trip abroad) do not shrink the home
 * city to a dot. With `trim` 0 it is the exact extent.
 */
export function robustBounds(pts: readonly LonLat[], trim = 0.05): Bounds | null {
  const lons = pts.map((p) => p.lon).filter(Number.isFinite);
  const lats = pts.map((p) => p.lat).filter(Number.isFinite);
  if (lons.length === 0 || lats.length === 0) return null;
  return {
    west: percentile(lons, trim),
    east: percentile(lons, 1 - trim),
    south: percentile(lats, trim),
    north: percentile(lats, 1 - trim),
  };
}

/**
 * The bounds of the single densest cluster of positions — the metro where most
 * time is spent — so the home city opens legible rather than a dot inside a
 * continent. Points are binned on a coarse degree grid; the heaviest cell and
 * its immediate neighbours define the frame. Far-flung places (another city, a
 * trip abroad) stay reachable by zooming out, not forced into the first view.
 */
export function densestBounds(pts: readonly LonLat[], cell = 0.35): Bounds | null {
  const valid = pts.filter((p) => Number.isFinite(p.lon) && Number.isFinite(p.lat));
  if (valid.length === 0) return null;
  if (valid.length < 5) return robustBounds(valid, 0);
  const cellOf = (p: LonLat): [number, number] => [Math.floor(p.lon / cell), Math.floor(p.lat / cell)];
  const counts = new Map<string, number>();
  for (const p of valid) {
    const [cx, cy] = cellOf(p);
    const k = `${cx}:${cy}`;
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  let bestKey = "";
  let best = -1;
  for (const [k, n] of counts) if (n > best) { best = n; bestKey = k; }
  const [bx, by] = bestKey.split(":").map(Number) as [number, number];
  const near = valid.filter((p) => {
    const [cx, cy] = cellOf(p);
    return Math.abs(cx - bx) <= 1 && Math.abs(cy - by) <= 1;
  });
  // A light trim centres the dense core rather than letting one edge point
  // (an airport on the city's rim) push it into a corner.
  return robustBounds(near.length ? near : valid, near.length >= 20 ? 0.05 : 0);
}

const EARTH_CIRCUMFERENCE_M = 40_075_016.686;

/** Metres covered by one screen pixel at the viewport's centre latitude. */
export function metersPerPixel(v: Viewport): number {
  const { lat } = worldToLonLat(v.cx, Math.max(0, Math.min(1, v.cy)));
  const cos = Math.max(0.05, Math.cos((lat * Math.PI) / 180));
  return (EARTH_CIRCUMFERENCE_M * cos) / Math.max(1, v.k);
}

/** A round "1·2·5" number not larger than `x` (for scale bars and ticks). */
export function niceRound(x: number): number {
  if (!(x > 0)) return 0;
  const pow = Math.pow(10, Math.floor(Math.log10(x)));
  const f = x / pow;
  const n = f >= 5 ? 5 : f >= 2 ? 2 : 1;
  return n * pow;
}

/** A viewport that frames `bounds` inside width×height with pixel padding. */
export function fitBounds(bounds: Bounds, width: number, height: number, pad = 40): Viewport {
  const [wx0, wy0] = lonLatToWorld(bounds.west, bounds.north); // top-left
  const [wx1, wy1] = lonLatToWorld(bounds.east, bounds.south); // bottom-right
  const spanX = Math.max(Math.abs(wx1 - wx0), 1e-9);
  const spanY = Math.max(Math.abs(wy1 - wy0), 1e-9);
  const usableW = Math.max(1, width - pad * 2);
  const usableH = Math.max(1, height - pad * 2);
  // Clamp so a single-point bound does not zoom to infinity (city-block cap).
  const k = Math.min(usableW / spanX, usableH / spanY, 1 << 22);
  return { cx: (wx0 + wx1) / 2, cy: (wy0 + wy1) / 2, k: Math.max(k, width), width, height };
}

/** Clamp a viewport's centre and scale to sane limits for width×height. */
export function clampViewport(v: Viewport): Viewport {
  const k = Math.max(v.width, Math.min(v.k, 1 << 24));
  // Keep the world centre on-globe vertically; wrap is not supported, so clamp.
  const cy = Math.max(0, Math.min(1, v.cy));
  const cx = Math.max(0, Math.min(1, v.cx));
  return { ...v, k, cx, cy };
}

/** Multiply scale by `factor`, keeping the world point under (sx,sy) fixed. */
export function zoomAt(v: Viewport, sx: number, sy: number, factor: number): Viewport {
  const [wx, wy] = screenToWorld(v, sx, sy);
  const k = v.k * factor;
  // Solve for a centre that leaves (wx,wy) at (sx,sy) after the scale change.
  const cx = wx - (sx - v.width / 2) / k;
  const cy = wy - (sy - v.height / 2) / k;
  return clampViewport({ ...v, k, cx, cy });
}

/** Pan by a screen-pixel delta. */
export function panBy(v: Viewport, dxPx: number, dyPx: number): Viewport {
  return clampViewport({ ...v, cx: v.cx - dxPx / v.k, cy: v.cy - dyPx / v.k });
}

/**
 * The equivalent zoom level, 0 (whole world across the viewport) upward. Used to
 * decide when land tiles give way to the track being its own map.
 */
export function zoomLevel(v: Viewport): number {
  return Math.log2(v.k / Math.max(1, v.width));
}

export interface TrackPoint extends LonLat {
  t: number; // epoch ms
}

export interface DaySegment {
  day: string; // YYYY-MM-DD in the chosen zone
  points: TrackPoint[];
  /** 0 (oldest day) .. 1 (newest day), for recency colour */
  recency: number;
}

/** The local-day key (YYYY-MM-DD) of an instant in a zone. */
export function dayKey(tMs: number, tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date(tMs));
    const y = parts.find((p) => p.type === "year")?.value ?? "0000";
    const m = parts.find((p) => p.type === "month")?.value ?? "01";
    const d = parts.find((p) => p.type === "day")?.value ?? "01";
    return `${y}-${m}-${d}`;
  } catch {
    return new Date(tMs).toISOString().slice(0, 10);
  }
}

/**
 * Split an ordered track into one polyline per local day, each tagged with a
 * recency in [0,1] (newest day = 1) so the renderer can fade older days back.
 */
export function segmentByDay(points: readonly TrackPoint[], tz: string): DaySegment[] {
  const byDay = new Map<string, TrackPoint[]>();
  for (const p of points) {
    if (!Number.isFinite(p.lon) || !Number.isFinite(p.lat)) continue;
    const key = dayKey(p.t, tz);
    const list = byDay.get(key);
    if (list) list.push(p);
    else byDay.set(key, [p]);
  }
  const days = [...byDay.keys()].sort();
  const n = days.length;
  return days.map((day, i) => ({
    day,
    points: byDay.get(day)!,
    recency: n <= 1 ? 1 : i / (n - 1),
  }));
}

export interface HexBin {
  /** bin centre in screen pixels */
  x: number;
  y: number;
  count: number;
}

/**
 * Pointy-top hex binning in screen space. `radius` is the hex circumradius in
 * pixels; points are the already-projected screen coordinates. Returns one bin
 * per occupied cell with the count, which the renderer shades by density.
 */
export function hexbin(points: readonly [number, number][], radius: number): HexBin[] {
  if (radius <= 0) return [];
  const dx = radius * Math.sqrt(3);
  const dy = radius * 1.5;
  const bins = new Map<string, { x: number; y: number; count: number }>();
  for (const [px, py] of points) {
    if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
    let row = Math.round(py / dy);
    const offset = (row & 1) * (dx / 2);
    let col = Math.round((px - offset) / dx);
    const key = `${col}:${row}`;
    const cx = col * dx + offset;
    const cy = row * dy;
    const cur = bins.get(key);
    if (cur) cur.count += 1;
    else bins.set(key, { x: cx, y: cy, count: 1 });
  }
  return [...bins.values()];
}
