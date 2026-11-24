import { describe, expect, it } from "vitest";
import {
  densestBounds,
  fitBounds,
  hexbin,
  lonLatToWorld,
  metersPerPixel,
  niceRound,
  panBy,
  percentile,
  project,
  robustBounds,
  screenToWorld,
  segmentByDay,
  unproject,
  worldToLonLat,
  zoomAt,
  zoomLevel,
  type Viewport,
} from "./projection";

const vp = (over: Partial<Viewport> = {}): Viewport => ({ cx: 0.5, cy: 0.5, k: 1000, width: 800, height: 600, ...over });

describe("mercator", () => {
  it("maps lon/lat into the unit square", () => {
    const [x, y] = lonLatToWorld(0, 0);
    expect(x).toBeCloseTo(0.5, 6);
    expect(y).toBeCloseTo(0.5, 6);
    expect(lonLatToWorld(-180, 0)[0]).toBeCloseTo(0, 6);
    expect(lonLatToWorld(180, 0)[0]).toBeCloseTo(1, 6);
  });
  it("round-trips lon/lat ↔ world", () => {
    for (const [lon, lat] of [[-9.14, 38.72], [34, 12], [120, -40], [0, 60]]) {
      const [x, y] = lonLatToWorld(lon!, lat!);
      const back = worldToLonLat(x, y);
      expect(back.lon).toBeCloseTo(lon!, 4);
      expect(back.lat).toBeCloseTo(lat!, 4);
    }
  });
  it("clamps latitude beyond the mercator limit", () => {
    expect(Number.isFinite(lonLatToWorld(0, 89.9)[1])).toBe(true);
  });
});

describe("viewport", () => {
  it("project then unproject is identity", () => {
    const v = vp();
    const [sx, sy] = project(v, -9.14, 38.72);
    const ll = unproject(v, sx, sy);
    expect(ll.lon).toBeCloseTo(-9.14, 3);
    expect(ll.lat).toBeCloseTo(38.72, 3);
  });
  it("screenToWorld inverts worldToScreen", () => {
    const v = vp();
    const back = screenToWorld(v, 400, 300);
    expect(back[0]).toBeCloseTo(0.5, 6);
    expect(back[1]).toBeCloseTo(0.5, 6);
  });
  it("zoomAt keeps the world point under the cursor fixed", () => {
    const v = vp();
    const before = screenToWorld(v, 600, 200);
    const z = zoomAt(v, 600, 200, 2.5);
    const after = screenToWorld(z, 600, 200);
    expect(after[0]).toBeCloseTo(before[0], 6);
    expect(after[1]).toBeCloseTo(before[1], 6);
    expect(z.k).toBeGreaterThan(v.k);
  });
  it("panBy shifts the centre by the pixel delta / scale", () => {
    const v = vp();
    const p = panBy(v, 100, 0);
    expect(p.cx).toBeCloseTo(0.5 - 100 / v.k, 6);
  });
  it("zoomLevel grows by one per doubling", () => {
    const a = zoomLevel(vp({ k: 800 }));
    const b = zoomLevel(vp({ k: 1600 }));
    expect(b - a).toBeCloseTo(1, 6);
  });
});

describe("bounds", () => {
  it("percentile interpolates", () => {
    expect(percentile([0, 10], 0.5)).toBeCloseTo(5, 6);
    expect(percentile([1, 2, 3, 4], 0)).toBe(1);
    expect(percentile([1, 2, 3, 4], 1)).toBe(4);
  });
  it("robustBounds trims outliers", () => {
    const pts = [...Array(100)].map((_, i) => ({ lon: -9 + i * 0.001, lat: 38 + i * 0.001 }));
    pts.push({ lon: 150, lat: -80 }); // far outlier
    const b = robustBounds(pts, 0.02)!;
    expect(b.east).toBeLessThan(10);
    expect(b.south).toBeGreaterThan(0);
  });
  it("fitBounds frames the box and never zooms below the viewport width", () => {
    const b = { west: -10, east: -8, south: 38, north: 39 };
    const v = fitBounds(b, 800, 600, 40);
    const c = project(v, -9, 38.5);
    expect(c[0]).toBeGreaterThan(0);
    expect(c[0]).toBeLessThan(800);
    expect(v.k).toBeGreaterThanOrEqual(800);
  });
  it("densestBounds frames the heaviest cluster, not the outliers", () => {
    // 40 points in a Lisbon-tight cluster, 6 scattered across other cities.
    const pts = [
      ...[...Array(40)].map((_, i) => ({ lon: -9.14 + (i % 7) * 0.004, lat: 38.71 + (i % 5) * 0.004 })),
      { lon: -3.7, lat: 40.41 }, // Madrid
      { lon: -3.69, lat: 40.42 },
      { lon: -8.61, lat: 41.15 }, // Porto
      { lon: -8.6, lat: 41.14 },
      { lon: 34, lat: 12 }, // far outlier
      { lon: 2.35, lat: 48.85 }, // Paris
    ];
    const b = densestBounds(pts)!;
    expect(b.west).toBeGreaterThan(-9.2);
    expect(b.east).toBeLessThan(-9.0);
    expect(b.south).toBeGreaterThan(38.6);
    expect(b.north).toBeLessThan(38.8);
  });
});

describe("scale", () => {
  it("metersPerPixel shrinks with latitude and grows as you zoom out", () => {
    const equator = metersPerPixel({ cx: 0.5, cy: 0.5, k: 1e6, width: 800, height: 600 });
    const north = metersPerPixel({ cx: 0.5, cy: 0.34, k: 1e6, width: 800, height: 600 }); // ~45N
    expect(north).toBeLessThan(equator);
    const zoomedOut = metersPerPixel({ cx: 0.5, cy: 0.5, k: 5e5, width: 800, height: 600 });
    expect(zoomedOut).toBeGreaterThan(equator);
  });
  it("niceRound snaps down to a 1·2·5 number", () => {
    expect(niceRound(3700)).toBe(2000);
    expect(niceRound(820)).toBe(500);
    expect(niceRound(96)).toBe(50);
    expect(niceRound(0.37)).toBeCloseTo(0.2, 9);
    expect(niceRound(0)).toBe(0);
  });
});

describe("track", () => {
  it("segments a track into days with a recency ramp", () => {
    const d = Date.UTC(2026, 0, 1, 12);
    const pts = [
      { lon: 0, lat: 0, t: d },
      { lon: 1, lat: 1, t: d + 3600_000 },
      { lon: 2, lat: 2, t: d + 86400_000 }, // next day
    ];
    const segs = segmentByDay(pts, "UTC");
    expect(segs).toHaveLength(2);
    expect(segs[0]!.recency).toBe(0);
    expect(segs[1]!.recency).toBe(1);
    expect(segs[0]!.points).toHaveLength(2);
  });
});

describe("hexbin", () => {
  it("collapses nearby points into one cell and counts them", () => {
    const pts: [number, number][] = [
      [100, 100],
      [101, 100],
      [500, 500],
    ];
    const bins = hexbin(pts, 20);
    expect(bins.length).toBe(2);
    expect(Math.max(...bins.map((b) => b.count))).toBe(2);
  });
  it("returns nothing for a non-positive radius", () => {
    expect(hexbin([[0, 0]], 0)).toEqual([]);
  });
});
