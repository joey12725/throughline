/*
 * Mapping and data access shared by the channel page, the record drawer and the
 * exported ChannelCard. Nothing here names a channel or plugin: everything is
 * derived from the bootstrap row's declared shape, exactly as the lens engine
 * expects it.
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { sql } from "@/api";
import type { ChannelRow } from "@/api/types";
import {
  profileChannel,
  type ChannelMeta,
  type ChannelProfile,
  type Row,
  type RunSql,
  type SqlColumn,
} from "@/lens";

/** The engine's ChannelMeta from a bootstrap channel row. Booleans become counts
 *  (the engine reads with_geo/with_ended_at/with_images as a presence count). */
export function channelMeta(c: ChannelRow): ChannelMeta {
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

/** The engine's injected data-access, routed through the shared SQL scheduler. */
export function runSql(priority: "visible" | "normal" | "background" = "background"): RunSql {
  return (statement, opts) =>
    sql<Row>(statement, {
      rowCap: opts?.row_cap,
      deadlineMs: opts?.deadline_ms as number | undefined,
      priority,
    }).then((r) => ({ rows: r.rows as Row[], columns: r.columns as SqlColumn[], truncated: r.truncated }));
}

/**
 * Profile a channel with live SQL. Channels with no rows resolve immediately to a
 * declared profile inside the engine, so the hook stays enabled for them too and
 * the page still predicts lenses from declared fields alone.
 */
export function useChannelProfile(meta: ChannelMeta | null): UseQueryResult<ChannelProfile> {
  return useQuery({
    queryKey: ["channel-profile", meta?.name, meta?.rows, meta?.last_ingest_at],
    enabled: !!meta,
    staleTime: 10 * 60_000,
    queryFn: () => profileChannel(meta as ChannelMeta, runSql("normal")),
  });
}
