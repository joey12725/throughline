/*
 * Natural Earth land as flat rings of [lon,lat] pairs, bundled (world-atlas) and
 * computed once. Same source the charts' map lens uses, so Vite shares the one
 * JSON module; no remote tiles, no egress. The Places map draws these at low
 * zoom for context and lets the track itself be the map as you zoom in.
 */
import { feature } from "topojson-client";
import type { Feature, FeatureCollection, MultiPolygon, Polygon } from "geojson";
// eslint-disable-next-line @typescript-eslint/no-explicit-any
import landTopo from "world-atlas/land-110m.json";

let RINGS: number[][][] | null = null;

export function landRings(): number[][][] {
  if (RINGS) return RINGS;
  const out = feature(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    landTopo as any,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (landTopo as any).objects.land,
  ) as unknown as FeatureCollection<Polygon | MultiPolygon> | Feature<Polygon | MultiPolygon>;
  const feats = "features" in out ? out.features : [out];
  const rings: number[][][] = [];
  for (const f of feats) {
    const geom = f.geometry;
    if (!geom) continue;
    const polys = geom.type === "MultiPolygon" ? geom.coordinates : [geom.coordinates];
    for (const poly of polys) for (const ring of poly) rings.push(ring as number[][]);
  }
  RINGS = rings;
  return rings;
}
