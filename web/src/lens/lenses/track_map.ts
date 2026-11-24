import { sampleModulo } from "../sql";
import type { ChannelProfile, Role, RoleMap } from "../types";
import { type LensDef, SCOPE, channelSpec, col, costOf, int, resultSpec, resultTime, rolesOf } from "./common";

export interface GeoSource {
  lat: string;
  lon: string;
  /** Share of records with a position, 0..1 (1 when unknown but declared). */
  share: number;
  /** Positions come from field values rather than the envelope. */
  fields: Role[];
  /** Positions the envelope has were inferred from a nearby track point. */
  inferred: boolean;
}

/** Where a channel's coordinates live: the envelope when records carry positions, else a lat/lon field pair. */
export function geoSource(profile: ChannelProfile, roles: RoleMap): GeoSource | null {
  const env = profile.envelope;
  const own = env.with_geo_source ?? env.with_geo;
  const ownShare = env.rows > 0 ? own / env.rows : 0;
  const lat = rolesOf(roles, "geo.lat")[0];
  const lon = lat?.pair ? roles[lat.pair] : rolesOf(roles, "geo.lon")[0];
  if (ownShare >= 0.3) return { lat: "lat", lon: "lon", share: ownShare, fields: [], inferred: false };
  if (lat && lon && !lon.hidden) {
    const fill = profile.fields.find((f) => f.name === lat.field)?.fill;
    return { lat: col(lat.field), lon: col(lon.field), share: fill ?? 1, fields: [lat, lon], inferred: false };
  }
  const anyShare = env.rows > 0 ? env.with_geo / env.rows : 0;
  if (anyShare >= 0.05) return { lat: "lat", lon: "lon", share: anyShare, fields: [], inferred: env.with_geo_source !== null && own < env.with_geo };
  return null;
}

export const trackMap: LensDef = {
  id: "track_map",
  group: "space",
  needs: ["a position on most records, in time order"],
  summary: "The positions joined into a line in time order, coloured by time of day.",
  channels: { t: "time", lat: "latitude", lon: "longitude", accuracy: "optional accuracy in metres" },
  affinity: { geo_track: 1 },
  fit(profile, roles) {
    const g = geoSource(profile, roles);
    if (!g || g.inferred || g.share < 0.5) return { fitness: 0, why: "Records do not carry their own positions" };
    if (profile.envelope.rows > 0 && profile.envelope.rows < 20) return { fitness: 0.2, why: "Too few positions for a line" };
    return { fitness: g.share, why: `${Math.round(g.share * 100)}% of records carry a position` };
  },
  build(ctx) {
    const g = geoSource(ctx.profile, ctx.roles);
    if (!g) return [];
    const k = sampleModulo(ctx.rows, 5000);
    const acc = g.fields.length === 0 ? ", loc_accuracy_m AS accuracy" : "";
    const reference = ctx.meta.is_reference || ctx.meta.record_kind === "reference";
    return [
      {
        spec: channelSpec(ctx, "track_map", {
          title: "Track",
          why: reference
            ? "Records carry coordinates; joined in time order they make a line"
            : `${Math.round(g.share * 100)}% of records carry a position; joined in time order they are the path taken`,
          sql:
            `SELECT happened_at AS t, (${g.lat})::float8 AS lat, (${g.lon})::float8 AS lon${acc} FROM ${ctx.view} ` +
            `WHERE ${SCOPE} AND ${g.lat} IS NOT NULL AND ${g.lon} IS NOT NULL AND id % :k = 0 ORDER BY happened_at, id`,
          row_cap: 6000,
          encode: { t: "t", lat: "lat", lon: "lon", ...(acc ? { accuracy: "accuracy" } : {}) },
          params: { k: int(k) },
          sampled: k > 1,
          fields: g.fields.map((f) => f.field),
        }),
        information: reference ? 0.3 : ctx.profile.cadence.class === "sparse" ? 0.6 : 0.95,
        cost: costOf(ctx.rows, Math.min(5000, ctx.rows)),
      },
    ];
  },
  buildResult(ctx) {
    const lat = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "geo.lat");
    const lon = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "geo.lon");
    const t = resultTime(ctx);
    if (!lat || !lon || !t || ctx.rows < 5) return [];
    return [
      {
        spec: resultSpec("track_map", "as_is", {
          title: "Track",
          why: "The result has a time and coordinates: a path in time order",
          sql: ctx.statement,
          row_cap: 10000,
          encode: { t: t.name, lat: lat.name, lon: lon.name },
          fields: [t.name, lat.name, lon.name],
        }),
        information: 0.85,
        cost: 0,
      },
    ];
  },
};
