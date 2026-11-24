import { type LensDef, SCOPE, channelSpec, col, costOf, resultSpec, rolesOf } from "./common";
import { geoSource } from "./track_map";

export const pinMap: LensDef = {
  id: "pin_map",
  group: "space",
  needs: ["positions on some records (the envelope's, or a latitude/longitude field pair)"],
  summary: "Places as circles sized by how many records happened there.",
  channels: { lat: "latitude", lon: "longitude", size: "records at the place", label: "optional place name" },
  affinity: { geo_track: 0.9, registry: 0.5, interval: 0.4, feed_item: 0.4, conversation: 0.3 },
  fit(profile, roles) {
    const g = geoSource(profile, roles);
    if (!g) return { fitness: 0, why: "No positions" };
    return { fitness: Math.min(1, 0.4 + g.share), why: g.fields.length ? "A latitude/longitude field pair" : `${Math.round(g.share * 100)}% of records have a position` };
  },
  build(ctx) {
    const g = geoSource(ctx.profile, ctx.roles);
    if (!g) return [];
    const place = rolesOf(ctx.roles, "geo.label")[0];
    const labelSql = place ? `, max(${col(place.field)}) AS label` : "";
    return [
      {
        spec: channelSpec(ctx, "pin_map", {
          title: g.inferred ? "Where these records happened" : "Places",
          why: g.inferred
            ? `${Math.round(g.share * 100)}% of records were placed from the location track; circles show where they happened`
            : g.fields.length
              ? `${g.fields.map((f) => f.field).join(" and ")} are coordinates; circles mark the places${place ? `, named by ${place.field}` : ""}`
              : `Records carry positions; circles show where they cluster${place ? `, named by ${place.field}` : ""}`,
          sql:
            `SELECT round((${g.lat})::numeric, 3)::float8 AS lat, round((${g.lon})::numeric, 3)::float8 AS lon, count(*) AS n${labelSql} ` +
            `FROM ${ctx.view} WHERE ${SCOPE} AND ${g.lat} IS NOT NULL AND ${g.lon} IS NOT NULL GROUP BY 1, 2 ORDER BY n DESC LIMIT 2000`,
          row_cap: 2000,
          encode: { lat: "lat", lon: "lon", size: "n", ...(place ? { label: "label" } : {}) },
          fields: [...g.fields.map((f) => f.field), ...(place ? [place.field] : [])],
        }),
        information: g.inferred ? 0.4 * Math.min(1, g.share * 4) : 0.8,
        cost: costOf(ctx.rows, 800),
      },
    ];
  },
  buildResult(ctx) {
    const lat = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "geo.lat");
    const lon = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "geo.lon");
    if (!lat || !lon || ctx.rows < 1) return [];
    const size = ctx.profile.columns.find((c) => ctx.roles[c.name]?.role === "count");
    const name = ctx.profile.columns.find((c) => c.kind === "text");
    return [
      {
        spec: resultSpec("pin_map", "as_is", {
          title: "Places",
          why: "The result has coordinates: one circle per row",
          sql: ctx.statement,
          row_cap: 5000,
          encode: { lat: lat.name, lon: lon.name, ...(size ? { size: size.name } : {}), ...(name ? { label: name.name } : {}) },
          fields: [lat.name, lon.name],
        }),
        information: 0.8,
        cost: 0,
      },
    ];
  },
};
