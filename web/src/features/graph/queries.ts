/*
 * Bounded, read-only SQL for the Graph. Nodes are entities and people; edges are
 * co-occurrence through shared messages (the link graph is message-centric) plus
 * direct entity↔entity links. Every statement is one SELECT in schema `api`,
 * depth ≤ 2 and capped at a node budget, so no query can walk the whole graph.
 */

/** Double single quotes so free text can never break out of a literal. */
function lit(s: string): string {
  return s.replace(/'/g, "''");
}

export function entityKindsSql(): string {
  return "SELECT kind, count(*) n FROM api.entities GROUP BY 1 ORDER BY 2 DESC";
}

export function linkKindsSql(cap = 24): string {
  return `SELECT type, count(*) n FROM api.links GROUP BY 1 ORDER BY 2 DESC LIMIT ${cap}`;
}

/**
 * Overview nodes: the most-connected entities and people across the whole
 * installation, as a single ranked set. Degree is every link that touches the
 * node. Bounded by `cap`; derived purely from shape.
 */
export function overviewNodesSql(cap = 120): string {
  return (
    "SELECT node_kind, id, label, kind, deg FROM (" +
    " SELECT 'entity'::text node_kind, e.id, e.name label, e.kind kind, count(l.id)::int deg" +
    " FROM api.entities e" +
    " LEFT JOIN api.links l ON (l.from_kind='entity' AND l.from_id=e.id) OR (l.to_kind='entity' AND l.to_id=e.id)" +
    " WHERE e.merged_into IS NULL" +
    " GROUP BY e.id, e.name, e.kind" +
    " UNION ALL" +
    " SELECT 'person'::text, p.id, coalesce(p.display_name, p.handle), 'person'::text," +
    " (SELECT count(*)::int FROM api.links l WHERE l.to_kind='person' AND l.to_id=p.id)" +
    " FROM api.people p WHERE p.merged_into IS NULL AND p.is_owner = false" +
    ") q WHERE deg > 0 ORDER BY deg DESC, label" +
    ` LIMIT ${Math.trunc(cap)}`
  );
}

/**
 * Overview edges among a given node set: message co-occurrence (two nodes named
 * in the same message) unioned with direct typed entity/person links, so hubs,
 * documents and organisations all wire up. The id lists come from
 * `overviewNodesSql` and are integers only, so nothing but data we already
 * fetched reaches the string. `rel` is "shared" for co-occurrence or the link
 * type for a direct link; `w` is the combined strength. Bounded by `cap`.
 */
export function overviewEdgesSql(entityIds: readonly number[], personIds: readonly number[], cap = 700): string {
  const list = (xs: readonly number[]): string => (xs.length ? xs.map((x) => Math.trunc(x)).join(",") : "-1");
  const el = list(entityIds);
  const pl = list(personIds);
  const inSet = (col: string) => `((${col}_kind='entity' AND ${col}_id IN (${el})) OR (${col}_kind='person' AND ${col}_id IN (${pl})))`;
  return (
    "WITH ml AS (" +
    " SELECT l.from_id mid, l.to_kind, l.to_id FROM api.links l" +
    " WHERE l.from_kind='message' AND (" +
    `  (l.to_kind='entity' AND l.to_id IN (${el})) OR (l.to_kind='person' AND l.to_id IN (${pl}))` +
    " ))," +
    " cooc AS (" +
    "  SELECT a.to_kind ak, a.to_id aid, b.to_kind bk, b.to_id bid, count(DISTINCT a.mid)::int w, 'shared'::text rel" +
    "  FROM ml a JOIN ml b ON a.mid=b.mid AND (a.to_kind > b.to_kind OR (a.to_kind=b.to_kind AND a.to_id > b.to_id))" +
    "  GROUP BY 1,2,3,4" +
    " )," +
    " direct AS (" +
    "  SELECT l.from_kind ak, l.from_id aid, l.to_kind bk, l.to_id bid, count(*)::int w, min(l.type) rel" +
    "  FROM api.links l WHERE l.from_kind IN ('entity','person') AND l.to_kind IN ('entity','person')" +
    `  AND ${inSet("l.from")} AND ${inSet("l.to")} AND NOT (l.from_kind=l.to_kind AND l.from_id=l.to_id)` +
    "  GROUP BY 1,2,3,4" +
    " )" +
    " SELECT ak, aid, bk, bid, sum(w)::int w, min(rel) rel" +
    " FROM (SELECT * FROM cooc UNION ALL SELECT * FROM direct) u" +
    " GROUP BY 1,2,3,4 ORDER BY w DESC" +
    ` LIMIT ${Math.trunc(cap)}`
  );
}

/** Most-connected entities (degree over all links touching them). */
export function mostConnectedSql(cap = 12): string {
  return (
    "SELECT e.kind, e.id, e.name, count(l.id) deg FROM api.entities e" +
    " JOIN api.links l ON (l.from_kind='entity' AND l.from_id=e.id) OR (l.to_kind='entity' AND l.to_id=e.id)" +
    ` GROUP BY 1,2,3 ORDER BY 4 DESC, e.name LIMIT ${cap}`
  );
}

/** Search entities and people by name for the focus box. */
export function searchSql(term: string, cap = 24): string {
  const t = lit(term);
  return (
    "SELECT 'entity' node_kind, e.id, e.name label, e.kind kind," +
    " (SELECT count(*) FROM api.links l WHERE (l.from_kind='entity' AND l.from_id=e.id) OR (l.to_kind='entity' AND l.to_id=e.id)) deg" +
    ` FROM api.entities e WHERE e.name ILIKE '%${t}%'` +
    " UNION ALL" +
    " SELECT 'person', p.id, coalesce(p.display_name, p.handle), 'person'," +
    " (SELECT count(*) FROM api.links l WHERE l.to_kind='person' AND l.to_id=p.id) deg" +
    ` FROM api.people p WHERE (p.display_name ILIKE '%${t}%' OR p.handle ILIKE '%${t}%') AND p.merged_into IS NULL` +
    ` ORDER BY deg DESC NULLS LAST LIMIT ${cap}`
  );
}

/**
 * Neighbours of a node (kind in 'entity'|'person', id), by how many messages
 * they share with it. Resolves each neighbour's label and type in one pass.
 */
export function neighboursSql(kind: "entity" | "person", id: number, cap = 40): string {
  const k = kind === "person" ? "person" : "entity";
  const i = Math.trunc(id);
  return (
    "SELECT l2.to_kind node_kind, l2.to_id id, count(DISTINCT l1.from_id) shared," +
    " coalesce(e.name, p.display_name, p.handle) label, coalesce(e.kind, 'person') kind" +
    " FROM api.links l1" +
    " JOIN api.links l2 ON l2.from_kind='message' AND l2.from_id=l1.from_id" +
    " LEFT JOIN api.entities e ON l2.to_kind='entity' AND e.id=l2.to_id" +
    " LEFT JOIN api.people p ON l2.to_kind='person' AND p.id=l2.to_id" +
    ` WHERE l1.from_kind='message' AND l1.to_kind='${k}' AND l1.to_id=${i}` +
    " AND l2.to_kind IN ('entity','person')" +
    ` AND NOT (l2.to_kind='${k}' AND l2.to_id=${i})` +
    " AND coalesce(e.merged_into, p.merged_into) IS NULL" +
    ` GROUP BY 1,2,4,5 HAVING count(DISTINCT l1.from_id) > 0 ORDER BY 3 DESC, 4 LIMIT ${cap}`
  );
}

export function entityDetailSql(id: number): string {
  return `SELECT id, kind, name, external_ref, is_placeholder, metadata FROM api.entities WHERE id=${Math.trunc(id)}`;
}
export function personDetailSql(id: number): string {
  return `SELECT id, handle, display_name, kind, status, note FROM api.people WHERE id=${Math.trunc(id)}`;
}

/** Records attached to a node, newest first (each opens the record drawer). */
export function nodeRecordsSql(kind: "entity" | "person", id: number, cap = 60): string {
  const k = kind === "person" ? "person" : "entity";
  const i = Math.trunc(id);
  return (
    "SELECT m.id, m.channel, m.is_reference, m.happened_at, m.content, l.type link" +
    " FROM api.links l JOIN api.messages m ON m.id=l.from_id AND l.from_kind='message'" +
    ` WHERE l.to_kind='${k}' AND l.to_id=${i}` +
    ` ORDER BY m.happened_at DESC LIMIT ${cap}`
  );
}

/** Direct, typed links involving a node (for the links list in the panel). */
export function nodeLinksSql(kind: "entity" | "person", id: number, cap = 80): string {
  const k = kind === "person" ? "person" : "entity";
  const i = Math.trunc(id);
  return (
    "SELECT l.type, count(*) n FROM api.links l" +
    ` WHERE (l.from_kind='${k}' AND l.from_id=${i}) OR (l.to_kind='${k}' AND l.to_id=${i})` +
    ` GROUP BY 1 ORDER BY 2 DESC LIMIT ${cap}`
  );
}
