/*
 * Data access for the People screens. Every statement is a bounded SELECT over
 * schema api; nothing here names a real channel or plugin — the plugin family of
 * a person is resolved from the bootstrap channel table. Numbers come back as
 * strings (Postgres numeric) and are coerced with Number() at the edge.
 */
import { useMemo } from "react";
import { useSql, useBootstrap, num } from "@/api";
import type { ChannelRow } from "@/api/types";
import { useScopes } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import type { PersonInput } from "./layout";

const UNKNOWN_FAMILY = "—";

/** channel name → plugin, from the bootstrap channel table. */
export function channelPluginMap(channels: readonly ChannelRow[]): Map<string, string> {
  const m = new Map<string, string>();
  for (const c of channels) m.set(c.name, c.plugin);
  return m;
}

/** The colour index of a channel within its plugin (stable, bootstrap order). */
export function channelIndexMap(channels: readonly ChannelRow[]): Map<string, number> {
  const per = new Map<string, number>();
  const idx = new Map<string, number>();
  for (const c of channels) {
    const i = per.get(c.plugin) ?? 0;
    idx.set(c.name, i);
    per.set(c.plugin, i + 1);
  }
  return idx;
}

export interface PeopleRow {
  id: string;
  name: string | null;
  handle: string | null;
  records: string;
  channels: string;
  first_at: string | null;
  last_at: string | null;
  top_channel: string | null;
}

/** All people (excluding the owner), aggregated for the constellation + list. */
export function usePeopleAggregate(ownerId: number | null) {
  const { from, to } = useScopes();
  const scoped = from || to ? `AND m.happened_at >= ${lit(from)} AND m.happened_at < ${lit(to, true)}` : "";
  const ownerFilter = ownerId != null ? `AND p.id <> ${ownerId | 0}` : "";
  const statement =
    ownerId == null
      ? null
      : `SELECT p.id::text AS id, p.display_name AS name, p.handle AS handle, ` +
        `count(*)::int AS records, count(DISTINCT m.channel)::int AS channels, ` +
        `min(m.happened_at) AS first_at, max(m.happened_at) AS last_at, ` +
        `mode() WITHIN GROUP (ORDER BY m.channel) AS top_channel ` +
        `FROM api.people p JOIN api.messages m ON m.person_id = p.id ` +
        `WHERE p.is_owner = false ${ownerFilter} ${scoped} ` +
        `GROUP BY 1, 2, 3 ORDER BY records DESC`;
  return useSql<PeopleRow>(statement, {
    rowCap: 5000,
    priority: "visible",
    keyParts: ["people-agg", from ?? "", to ?? ""],
  });
}

export interface SparkRow {
  person_id: string;
  bucket: string;
  n: string;
}

/** Coarse per-person volume buckets, for the side-list trend sparkline. */
export function usePeopleSparklines(ownerId: number | null) {
  const statement =
    ownerId == null
      ? null
      : `SELECT person_id::text AS person_id, date_trunc('quarter', happened_at) AS bucket, count(*)::int AS n ` +
        `FROM api.messages WHERE person_id IS NOT NULL AND person_id <> ${ownerId | 0} ` +
        `GROUP BY 1, 2 ORDER BY 1, 2`;
  const q = useSql<SparkRow>(statement, { rowCap: 10000, priority: "background", keyParts: ["people-spark"] });
  const byPerson = useMemo(() => {
    const m = new Map<string, number[]>();
    for (const r of q.data?.rows ?? []) {
      const arr = m.get(r.person_id) ?? [];
      arr.push(num(r.n));
      m.set(r.person_id, arr);
    }
    return m;
  }, [q.data]);
  return byPerson;
}

/** Join the aggregate rows to their plugin family and shape them for the layout. */
export function usePeopleInputs(ownerId: number | null): {
  people: PersonInput[];
  loading: boolean;
  error: unknown;
  isError: boolean;
  refetch: () => void;
} {
  const { data: boot } = useBootstrap();
  const agg = usePeopleAggregate(ownerId);
  const pluginOf = useMemo(() => channelPluginMap(boot?.channels ?? []), [boot?.channels]);
  const people = useMemo<PersonInput[]>(() => {
    return (agg.data?.rows ?? []).map((r) => {
      const top = r.top_channel ?? "";
      return {
        id: Number(r.id),
        name: r.name ?? r.handle ?? `Person ${r.id}`,
        handle: r.handle,
        records: num(r.records),
        channels: num(r.channels),
        firstAt: r.first_at,
        lastAt: r.last_at,
        topChannel: top,
        family: pluginOf.get(top) ?? UNKNOWN_FAMILY,
      };
    });
  }, [agg.data, pluginOf]);
  return {
    people,
    loading: agg.isLoading,
    error: agg.error,
    isError: agg.isError,
    refetch: () => void agg.refetch(),
  };
}

/** Timezone-bound scope the person charts read. */
export function usePersonScope(personId: number) {
  const tz = usePrefs((s) => s.tz);
  return useMemo(() => ({ person: personId, tz }), [personId, tz]);
}

/* A timestamp literal for inlined SQL. `from`/`to` arrive as ISO instants from
 * the global scope; everything else is validated by the server's reader role. */
function lit(v: string | null, upper = false): string {
  if (!v) return upper ? "'infinity'::timestamptz" : "'-infinity'::timestamptz";
  return `'${v.replace(/'/g, "''")}'::timestamptz`;
}
