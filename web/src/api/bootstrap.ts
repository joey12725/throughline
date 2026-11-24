/*
 * Bootstrap. On a current server this is one call to GET /v1/web/bootstrap. The
 * backend route is being built in parallel, so until it lands the dev server
 * answers 404; `composeBootstrap` then builds the SAME shape from the older
 * routes (GET /v1/status, GET /v1/channels) and a couple of SQL statements, so
 * the shell works against an older server too. The result is flagged `degraded`.
 */
import { ApiError } from "./errors";
import { request } from "./client";
import { sql } from "./sql";
import type {
  Bootstrap,
  ChannelRow,
  Features,
  PluginRow,
  Totals,
} from "./types";
import { auth } from "@/store/auth";

export async function loadBootstrap(signal?: AbortSignal): Promise<Bootstrap> {
  try {
    return await request<Bootstrap>("/web/bootstrap", { signal });
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      return composeBootstrap(signal);
    }
    throw err;
  }
}

/* ---- Degraded composition ------------------------------------------------ */

interface StatusResp {
  embeddings: { available: boolean; models: Array<{ model: string }> };
  plugins: Array<{
    name: string;
    manifest?: { version?: string; run_mode?: string; default_interval_s?: number };
    interval_s?: number;
    last_success_at: string | null;
    last_error: string | null;
    messages_ingested: number;
    updated_at: string | null;
    stale: boolean;
  }>;
  channels: Array<{ name: string; count: number; last_ingest: string | null }>;
  staleness?: {
    newest_ingest_at?: string | null;
    coverage?: { partial?: string[]; best_effort?: string[]; open_gaps?: number };
  };
}

interface ChannelCatalogRow {
  name: string;
  plugin: string;
  description: string | null;
  fields: Array<{ name: string; type: string; description?: string | null }>;
  is_reference: boolean;
  is_outside: boolean;
  conversation_whole_thread: boolean;
  record_kind: string | null;
  embed: boolean | null;
}

interface ChannelStat {
  channel: string;
  rows: string;
  threads: string;
  first_at: string | null;
  last_at: string | null;
  with_geo: boolean;
  with_ended_at: boolean;
  with_images: boolean;
}

async function composeBootstrap(signal?: AbortSignal): Promise<Bootstrap> {
  const [status, channels] = await Promise.all([
    request<StatusResp>("/status", { signal }),
    request<ChannelCatalogRow[]>("/channels", { signal }),
  ]);

  const totals = await loadTotals(signal).catch(() => zeroTotals());
  const stats = await loadChannelStats(signal).catch(() => new Map<string, ChannelStat>());

  const coverage = status.staleness?.coverage ?? {};
  const partial = new Set(coverage.partial ?? []);
  const bestEffort = new Set(coverage.best_effort ?? []);

  const countByChannel = new Map(status.channels.map((c) => [c.name, c]));

  const pluginSet = new Set(channels.map((c) => c.plugin));
  const plugins: PluginRow[] = status.plugins.map((p) => ({
    name: p.name,
    version: p.manifest?.version ?? null,
    mode: p.manifest?.run_mode ?? null,
    interval_s: p.interval_s ?? p.manifest?.default_interval_s ?? null,
    last_success_at: p.last_success_at,
    last_error: p.last_error,
    messages_ingested: p.messages_ingested,
    stale: p.stale,
    updated_at: p.updated_at,
    open_outage: null,
  }));
  for (const p of status.plugins) pluginSet.add(p.name);

  const channelRows: ChannelRow[] = channels.map((c) => {
    const st = stats.get(c.name);
    const cnt = countByChannel.get(c.name);
    const status0 = partial.has(c.name)
      ? "partial"
      : bestEffort.has(c.name)
        ? "best_effort"
        : "complete";
    return {
      name: c.name,
      plugin: c.plugin,
      description: c.description,
      record_kind: c.record_kind,
      is_reference: c.is_reference,
      is_outside: c.is_outside,
      whole_thread: c.conversation_whole_thread,
      embed: Boolean(c.embed),
      fields: c.fields ?? [],
      view: `ch_${c.name.replace(/\./g, "_")}`,
      rows: st ? Number(st.rows) : (cnt?.count ?? 0),
      threads: st ? Number(st.threads) : 0,
      first_at: st?.first_at ?? null,
      last_at: st?.last_at ?? null,
      last_ingest_at: cnt?.last_ingest ?? null,
      with_geo: st?.with_geo ?? false,
      with_ended_at: st?.with_ended_at ?? false,
      with_images: st?.with_images ?? false,
      coverage: {
        status: status0,
        reasons: status0 === "best_effort" ? ["not_reported"] : [],
        covered_from: null,
        covered_to: null,
        open_gaps: 0,
      },
    };
  });

  const model = status.embeddings.models[0]?.model ?? null;
  const features: Features = {
    web_state: false, // this route set is absent, so is tl.web_state
    source_health: false,
    embeddings: { available: status.embeddings.available, model },
    assistant: { provider: null, model: null, loopback: true, default_tier: "schema" },
    egress: {},
  };

  return {
    version: "degraded",
    now: status.staleness?.newest_ingest_at ?? new Date().toISOString(),
    token: {
      id: "current",
      kind: "owner",
      name: "this token",
      asker: auth.asker(),
      unrestricted: totals.people > 0,
      can_write: false,
    },
    features,
    owner_person_id: null,
    totals: { ...totals, channels: channelRows.length, plugins: pluginSet.size },
    plugins,
    channels: channelRows,
    degraded: true,
  };
}

async function loadTotals(signal?: AbortSignal): Promise<Totals> {
  const r = await sql<{
    records: string;
    people: string;
    entities: string;
    links: string;
    threads: string;
  }>(
    `SELECT
       (SELECT count(*) FROM api.messages) AS records,
       (SELECT count(*) FROM api.people) AS people,
       (SELECT count(*) FROM api.entities) AS entities,
       (SELECT count(*) FROM api.links) AS links,
       (SELECT count(*) FROM api.threads) AS threads`,
    { priority: "visible", signal, rowCap: 1 },
  );
  const row = r.rows[0];
  return {
    records: row ? Number(row.records) : 0,
    people: row ? Number(row.people) : 0,
    entities: row ? Number(row.entities) : 0,
    links: row ? Number(row.links) : 0,
    threads: row ? Number(row.threads) : 0,
    channels: 0,
    plugins: 0,
    vectors: 0,
  };
}

async function loadChannelStats(signal?: AbortSignal): Promise<Map<string, ChannelStat>> {
  const r = await sql<ChannelStat>(
    `SELECT channel,
            count(*)::text AS rows,
            count(DISTINCT thread_id)::text AS threads,
            min(happened_at)::text AS first_at,
            max(happened_at)::text AS last_at,
            bool_or(lat IS NOT NULL) AS with_geo,
            bool_or(ended_at IS NOT NULL) AS with_ended_at,
            bool_or(images IS NOT NULL AND jsonb_array_length(images) > 0) AS with_images
       FROM api.messages
      GROUP BY channel`,
    { priority: "visible", signal, rowCap: 1000 },
  );
  return new Map(r.rows.map((s) => [s.channel, s]));
}

function zeroTotals(): Totals {
  return {
    records: 0,
    people: 0,
    entities: 0,
    links: 0,
    threads: 0,
    channels: 0,
    plugins: 0,
    vectors: 0,
  };
}
