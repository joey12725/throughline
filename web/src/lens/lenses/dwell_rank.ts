import { type LensDef, SCOPE, channelSpec, col, costOf, int, rolesOf } from "./common";
import { geoSource } from "./track_map";

export const dwellRank: LensDef = {
  id: "dwell_rank",
  group: "space",
  needs: ["positions", "a duration per record (an end time, or a duration field)"],
  summary: "Places ranked by the time spent there.",
  channels: { label: "place", value: "total time in seconds", visits: "stays", lat: "latitude", lon: "longitude" },
  affinity: { geo_track: 0.85, interval: 0.5 },
  fit(profile, roles) {
    const g = geoSource(profile, roles);
    if (!g || g.inferred) return { fitness: 0, why: "No positions of its own" };
    const ended = profile.rows > 0 ? profile.envelope.with_ended_at / profile.rows : 0;
    if (ended >= 0.02) return { fitness: 0.9, why: "Positions with an end time: stays" };
    return rolesOf(roles, "duration").length > 0 ? { fitness: 0.7, why: "Positions with a duration field" } : { fitness: 0, why: "No time spent to rank by" };
  },
  build(ctx) {
    const g = geoSource(ctx.profile, ctx.roles);
    if (!g) return [];
    const ended = ctx.rows > 0 ? ctx.profile.envelope.with_ended_at / ctx.rows : 0;
    const duration = rolesOf(ctx.roles, "duration").find((d) => d.unit === "s" || d.unit === undefined);
    if (ended < 0.02 && !duration) return [];
    const dur = ended >= 0.02 ? "extract(epoch FROM ended_at - happened_at)" : col((duration as { field: string }).field);
    const cond = ended >= 0.02 ? "ended_at IS NOT NULL" : `${dur} IS NOT NULL`;
    const place = rolesOf(ctx.roles, "geo.label")[0];
    const coords = `round((${g.lat})::numeric, 3)::text || ', ' || round((${g.lon})::numeric, 3)::text`;
    const name = place ? `coalesce(nullif(${col(place.field)}, ''), ${coords})` : coords;
    return [
      {
        spec: channelSpec(ctx, "dwell_rank", {
          title: "Where the time goes",
          why: `${ended >= 0.02 ? "Some records are stays with an end time" : `${(duration as { field: string }).field} gives the time spent`}; summing it per ${place ? place.field : "place"} ranks the places`,
          sql:
            `SELECT ${name} AS place, sum(${dur})::float8 AS dwell_s, count(*) AS visits, avg((${g.lat})::float8)::float8 AS lat, avg((${g.lon})::float8)::float8 AS lon ` +
            `FROM ${ctx.view} WHERE ${SCOPE} AND ${cond} AND ${g.lat} IS NOT NULL GROUP BY 1 ORDER BY dwell_s DESC NULLS LAST LIMIT :top`,
          row_cap: 100,
          encode: { label: "place", value: "dwell_s", visits: "visits", lat: "lat", lon: "lon" },
          params: { top: int(30) },
          options: { units: { dwell_s: "s" } },
          fields: [...g.fields.map((f) => f.field), ...(place ? [place.field] : []), ...(ended < 0.02 && duration ? [duration.field] : [])],
        }),
        information: place ? 0.85 : 0.6,
        cost: costOf(ctx.rows, 30),
      },
    ];
  },
};
