/*
 * Read-only data for the record drawer, each piece its own scheduled query so a
 * missing overlay (no provenance, no links) degrades to "none" rather than
 * failing the drawer. Everything is a SELECT over schema api through the same
 * validator and asker gate as the rest of the UI.
 */
import { useSql } from "@/api";
import { lit } from "@/lens";
import type { ChannelRow } from "@/api/types";

export interface AnyRow {
  [k: string]: unknown;
}

/** Strip a record-kind prefix ("m:1549") and keep the numeric id. */
export function recordId(raw: string): number | null {
  const m = /(\d+)\s*$/.exec(raw);
  return m ? Number(m[1]) : null;
}

const RECORD_COLS = [
  "id",
  "external_id",
  "channel_id",
  "version",
  "superseded_by",
  "thread_id",
  "thread_no",
  "thread_title",
  "thread_external_id",
  "happened_at",
  "happened_offset_min",
  "ended_at",
  "ingested_at",
  "author_handle",
  "person_id",
  "person_handle",
  "display_name",
  "content",
  "images",
  "lat",
  "lon",
  "access_label",
  "is_reference",
  "fields",
];

function quote(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

export function useRecord(id: number | null, channel: ChannelRow | undefined) {
  const view = channel?.view ?? "api.messages";
  const declared = (channel?.fields ?? []).map((f) => f.name).filter((n) => !RECORD_COLS.includes(n));
  const cols = [...RECORD_COLS, ...declared].map(quote).join(", ");
  return useSql<AnyRow>(id !== null ? `SELECT ${cols} FROM ${view} WHERE id = ${lit.int(id)} LIMIT 1` : null, {
    rowCap: 1,
    priority: "visible",
    keyParts: [channel?.name ?? ""],
  });
}

export function useThreadContext(id: number | null) {
  return useSql<AnyRow>(
    id !== null
      ? `SELECT id, thread_no, happened_at, author_handle, person_handle, display_name, left(content, 240) AS content FROM api.context(${lit.int(id)}, 5, 5) ORDER BY thread_no`
      : null,
    { rowCap: 40, priority: "normal" },
  );
}

export function useProvenance(id: number | null) {
  const sources = useSql<AnyRow>(
    id !== null
      ? `SELECT source_message_id, source_version, relation, origin, job, created_at FROM api.provenance WHERE derived_id = ${lit.int(id)} ORDER BY source_message_id LIMIT 50`
      : null,
    { rowCap: 50, priority: "background" },
  );
  const supports = useSql<AnyRow>(
    id !== null
      ? `SELECT derived_id, derived_kind, relation, origin, job, created_at FROM api.provenance WHERE source_message_id = ${lit.int(id)} ORDER BY derived_id LIMIT 50`
      : null,
    { rowCap: 50, priority: "background" },
  );
  return { sources, supports };
}

export function useLinks(id: number | null) {
  return useSql<AnyRow>(
    id !== null
      ? [
          "SELECT l.type, l.to_kind, l.to_id, l.confidence, l.status, e.name AS entity_name, e.kind AS entity_kind",
          "FROM api.links l",
          "LEFT JOIN api.entities e ON l.to_kind = 'entity' AND e.id = l.to_id",
          `WHERE l.from_kind = 'message' AND l.from_id = ${lit.int(id)}`,
          "ORDER BY l.confidence DESC NULLS LAST LIMIT 50",
        ].join("\n")
      : null,
    { rowCap: 50, priority: "background" },
  );
}

export function useAnnotations(id: number | null) {
  return useSql<AnyRow>(
    id !== null
      ? `SELECT attribute, value, confidence, method, origin, status, created_at FROM api.annotations WHERE message_id = ${lit.int(id)} AND status = 'active' ORDER BY created_at DESC LIMIT 50`
      : null,
    { rowCap: 50, priority: "background" },
  );
}

export function useVersions(externalId: string | null, channelId: number | null) {
  const ready = externalId !== null && channelId !== null;
  return useSql<AnyRow>(
    ready
      ? `SELECT id, version, ingested_at, superseded_by FROM api.message_versions WHERE external_id = ${lit.text(externalId)} AND channel_id = ${lit.int(channelId)} ORDER BY version`
      : null,
    { rowCap: 50, priority: "background" },
  );
}
