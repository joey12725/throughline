// Test fixtures: the installation's 47 channel shapes (fixtures/zoo_channels.json at the
// repository root) and synthetic profiles built from rounded aggregate statistics.

import zoo from "../../../../fixtures/zoo_channels.json";
import { declaredProfile } from "../profile";
import { channelViewName } from "../sql";
import type { ChannelMeta, ChannelProfile, FieldDecl } from "../types";
import { SHAPE_STATS } from "./stats";

interface ZooChannel {
  name: string;
  plugin: string;
  description: string;
  record_kind: string;
  is_reference: boolean;
  is_outside: boolean;
  conversation_whole_thread: boolean;
  fields: FieldDecl[];
}

export const ZOO: ZooChannel[] = zoo as ZooChannel[];
export const ZOO_NAMES: string[] = ZOO.map((c) => c.name);

const END = Date.parse("2026-11-18T12:00:00Z");

/** Bootstrap-shaped metadata for one fixture channel; `empty` drops the counts. */
export function metaOf(name: string, empty = false): ChannelMeta {
  const c = ZOO.find((z) => z.name === name);
  if (!c) throw new Error(`no fixture channel ${name}`);
  const s = SHAPE_STATS[name] ?? [0, 0, 0, 0, 0, 0, 0];
  const rows = empty ? 0 : s[0];
  return {
    name: c.name,
    plugin: c.plugin,
    description: c.description,
    record_kind: c.record_kind,
    is_reference: c.is_reference,
    is_outside: c.is_outside,
    whole_thread: c.conversation_whole_thread,
    fields: c.fields,
    view: channelViewName(c.name),
    rows,
    threads: empty ? 0 : s[1],
    first_at: rows > 0 ? new Date(END - s[6] * 86400000).toISOString() : null,
    last_at: rows > 0 ? new Date(END).toISOString() : null,
    last_ingest_at: rows > 0 ? new Date(END).toISOString() : null,
    with_geo: empty ? 0 : s[3],
    with_ended_at: empty ? 0 : s[4],
    with_images: 0,
    coverage: null,
  };
}

/** A profile carrying only what aggregate statistics give: envelope counts, no field values. */
export function syntheticProfile(name: string): ChannelProfile {
  const meta = metaOf(name);
  const p = declaredProfile(meta);
  const s = SHAPE_STATS[name] ?? [0, 0, 0, 0, 0, 0, 0];
  if (meta.rows > 0) {
    p.envelope.authors = s[2];
    p.envelope.people = s[2];
    p.envelope.avg_content_chars = s[5];
    p.envelope.with_geo_source = s[3];
  }
  return p;
}

export function allMetas(empty = false): ChannelMeta[] {
  return ZOO_NAMES.map((n) => metaOf(n, empty));
}
