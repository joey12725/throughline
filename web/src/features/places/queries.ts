/*
 * Bounded, read-only SQL for Places. Every statement is a single SELECT against
 * schema `api`, scoped to the global time window and capped. The time window is
 * inlined as an ISO literal (the /v1/sql endpoint has no bind parameters); only
 * values that match a strict ISO shape are ever interpolated, so nothing the
 * server did not already hand us can reach the string.
 */

const ISO = /^\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?$/;

/** A safe SQL predicate for the current scope on a timestamp column, or "". */
export function scopeClause(col: string, from: string | null, to: string | null): string {
  const parts: string[] = [];
  if (from && ISO.test(from)) parts.push(`${col} >= '${from}'::timestamptz`);
  if (to && ISO.test(to)) parts.push(`${col} < '${to}'::timestamptz`);
  return parts.length ? ` AND ${parts.join(" AND ")}` : "";
}

/**
 * Track points for the map, sampled EVENLY across the whole scope so the line
 * traces every era, not just the oldest rows the server would keep under its
 * default cap. A window numbers the points in time order and keeps every
 * `tot/target`-th, so a track of any length renders in roughly `target` points
 * while preserving its shape. Ordered by time for day-segmenting downstream.
 */
export function trackSql(from: string | null, to: string | null, target = 4000): string {
  const t = Math.max(500, Math.trunc(target));
  return (
    "SELECT at, lat, lon FROM (" +
    " SELECT at, lat, lon," +
    " row_number() OVER (ORDER BY at) AS rn," +
    " count(*) OVER () AS tot" +
    " FROM api.location_track" +
    " WHERE lat IS NOT NULL AND lon IS NOT NULL" +
    scopeClause("at", from, to) +
    ") s" +
    ` WHERE rn % GREATEST(1, (tot / ${t})) = 0` +
    " ORDER BY at"
  );
}

/** Detected visits for the dwell layer (circles sized by time spent). */
export function visitsSql(from: string | null, to: string | null, cap = 2000): string {
  return (
    "SELECT happened_at, ended_at, lat, lon," +
    " extract(epoch FROM (ended_at - happened_at))::float8 AS dwell_s" +
    " FROM api.visits WHERE lat IS NOT NULL AND ended_at IS NOT NULL" +
    scopeClause("happened_at", from, to) +
    ` ORDER BY happened_at DESC LIMIT ${cap}`
  );
}

/** Named places with their coordinates and how many records happened there. */
export function placesSql(cap = 300): string {
  return (
    "SELECT e.id, e.name," +
    " (e.metadata->>'lat')::float8 AS lat, (e.metadata->>'lon')::float8 AS lon," +
    " e.metadata->>'address' AS address, count(l.id) AS records" +
    " FROM api.places e" +
    " LEFT JOIN api.links l ON l.to_kind='entity' AND l.to_id=e.id AND l.type='at_place'" +
    " WHERE e.metadata ? 'lat' AND e.metadata ? 'lon'" +
    " GROUP BY 1,2,3,4,5 ORDER BY records DESC, e.name" +
    ` LIMIT ${cap}`
  );
}

/** Top places by total dwell, each visit snapped to its nearest named place. */
export function topPlacesSql(from: string | null, to: string | null, cap = 24): string {
  return (
    "WITH pl AS (" +
    " SELECT name, (metadata->>'lat')::float8 la, (metadata->>'lon')::float8 lo" +
    " FROM api.places WHERE metadata ? 'lat' AND metadata ? 'lon')," +
    " snap AS (" +
    " SELECT v.happened_at, v.ended_at," +
    " (SELECT name FROM pl ORDER BY (pl.la - v.lat)^2 + (pl.lo - v.lon)^2 LIMIT 1) place," +
    " (SELECT la FROM pl ORDER BY (pl.la - v.lat)^2 + (pl.lo - v.lon)^2 LIMIT 1) lat," +
    " (SELECT lo FROM pl ORDER BY (pl.la - v.lat)^2 + (pl.lo - v.lon)^2 LIMIT 1) lon" +
    " FROM api.visits v WHERE v.lat IS NOT NULL AND v.ended_at IS NOT NULL" +
    scopeClause("v.happened_at", from, to) +
    ")" +
    " SELECT place, lat, lon, count(*) visits," +
    " sum(extract(epoch FROM (ended_at - happened_at)))::float8 dwell_s," +
    " min(happened_at) first_at, max(ended_at) last_at" +
    " FROM snap WHERE place IS NOT NULL" +
    " GROUP BY place, lat, lon ORDER BY dwell_s DESC NULLS LAST" +
    ` LIMIT ${cap}`
  );
}

/** Records linked to a named place, newest first. */
export function recordsAtPlaceSql(placeId: number, cap = 60): string {
  const id = Math.trunc(placeId);
  return (
    "SELECT m.id, m.channel, m.happened_at, m.content, m.person_id, m.display_name" +
    " FROM api.links l JOIN api.messages m ON m.id = l.from_id AND l.from_kind='message'" +
    ` WHERE l.to_kind='entity' AND l.to_id=${id} AND l.type='at_place'` +
    ` ORDER BY m.happened_at DESC LIMIT ${cap}`
  );
}

/** Visit history near a point (for a place's calendar heat + weekday×hour). */
export function visitHistorySql(lat: number, lon: number, radius = 0.004, cap = 1000): string {
  const la = Number(lat.toFixed(6));
  const lo = Number(lon.toFixed(6));
  const r2 = Number((radius * radius).toFixed(10));
  return (
    "SELECT happened_at, ended_at," +
    " extract(epoch FROM (ended_at - happened_at))::float8 dwell_s" +
    " FROM api.visits" +
    ` WHERE lat IS NOT NULL AND ended_at IS NOT NULL AND (lat - ${la})^2 + (lon - ${lo})^2 < ${r2}` +
    ` ORDER BY happened_at LIMIT ${cap}`
  );
}

/** Which channels carry geography, for the side-panel legend (names from data). */
export function geoChannelsSql(from: string | null, to: string | null, cap = 40): string {
  return (
    "SELECT channel, count(*) n FROM api.messages WHERE lat IS NOT NULL" +
    scopeClause("happened_at", from, to) +
    ` GROUP BY 1 ORDER BY 2 DESC LIMIT ${cap}`
  );
}

/**
 * Weekly record density over the whole archive, for the timeline strip. Weekly
 * (not daily) so each bucket is a few pixels wide — a readable histogram of
 * activity rather than a sub-pixel smear — and gaps in coverage stay gaps
 * (a week with no geo records simply has no bar).
 */
export function densitySql(cap = 4000): string {
  return (
    "SELECT date_trunc('week', happened_at) AS day, count(*) n" +
    " FROM api.messages WHERE lat IS NOT NULL" +
    " GROUP BY 1 ORDER BY 1" +
    ` LIMIT ${cap}`
  );
}
