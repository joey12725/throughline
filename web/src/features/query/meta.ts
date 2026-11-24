/*
 * The lens engine's ChannelMeta from a bootstrap channel row. (The channel feature
 * has the same mapping; this standalone copy keeps the query screen from importing
 * another feature's internals.) Booleans become presence counts, exactly as the
 * engine reads with_geo/with_ended_at/with_images.
 */
import type { ChannelRow } from "@/api/types";
import type { ChannelMeta } from "@/lens";

export function metaFromRow(c: ChannelRow): ChannelMeta {
  const rows = Number(c.rows) || 0;
  const present = (flag: unknown): number => (flag ? Math.max(1, rows || 1) : 0);
  return {
    name: c.name,
    plugin: c.plugin,
    description: c.description,
    record_kind: c.record_kind ?? "life_event",
    is_reference: !!c.is_reference,
    is_outside: !!c.is_outside,
    whole_thread: !!c.whole_thread,
    embed: !!c.embed,
    fields: Array.isArray(c.fields) ? (c.fields as ChannelMeta["fields"]) : [],
    view: c.view,
    rows,
    threads: Number(c.threads) || 0,
    first_at: c.first_at ?? null,
    last_at: c.last_at ?? null,
    last_ingest_at: c.last_ingest_at ?? null,
    with_geo: present(c.with_geo),
    with_ended_at: present(c.with_ended_at),
    with_images: present(c.with_images),
    coverage: c.coverage ? { ...c.coverage, reasons: [...c.coverage.reasons] } : null,
  };
}
