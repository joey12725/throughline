/*
 * /ui/people/:id — the person read model. A header (name, the identities and the
 * channels they are reached through, first/last contact, totals), then a grid of
 * bounded panels: the relationship timeline (brush sets the global scope), the
 * weekly rhythm, reply latency both ways, channel mix, the threads they are in,
 * recent records (opening in the drawer) and related people. Every number is a
 * query; nothing names a real channel or plugin.
 */
import { useEffect, useMemo, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, MessageCircleQuestion } from "lucide-react";
import { useBootstrap, useSql, num } from "@/api";
import { Panel, Stat, Skeleton, EmptyState, ErrorState, Sensitive, SourceDot, useSourceColor } from "@/ui";
import { ChartPanel } from "@/charts";
import type { ViewSpec } from "@/lens";
import { usePrefs } from "@/store/prefs";
import { formatInTz, relative } from "@/store/scopes";
import { useOpenRecord } from "@/features/record";
import { channelPluginMap, channelIndexMap } from "./data";
import { RelationshipTimeline } from "./RelationshipTimeline";
import styles from "./Person.module.css";

export function PersonPage({ id }: { id: number }): ReactNode {
  const navigate = useNavigate();
  const { data: boot } = useBootstrap();
  const tz = usePrefs((s) => s.tz);
  const openRecord = useOpenRecord();

  const pluginOf = useMemo(() => channelPluginMap(boot?.channels ?? []), [boot?.channels]);
  const chIndex = useMemo(() => channelIndexMap(boot?.channels ?? []), [boot?.channels]);
  const scope = useMemo(() => ({ person: id, tz }), [id, tz]);
  const now = boot?.now ? Date.parse(boot.now) : Date.now();

  const brief = useSql<BriefRow>(
    `SELECT display_name AS name, handle, message_count::int AS records, messages_to::int AS messages_to, ` +
      `exchange_count::int AS exchanges, first_contact_at, last_contact_at, circle_rank::int AS circle_rank, top_channels ` +
      `FROM api.person_brief(${id | 0})`,
    { priority: "visible", keyParts: ["brief", id] },
  );
  const idsQ = useSql<IdRow>(
    `SELECT system, kind, value, status FROM api.identifiers WHERE person_id = ${id | 0} ORDER BY status DESC, system`,
    { priority: "visible", keyParts: ["ids", id], rowCap: 50 },
  );

  const b = brief.data?.rows[0];
  const name = b?.name ?? b?.handle ?? `Person ${id}`;
  const firstAt = b?.first_contact_at ?? null;
  const lastAt = b?.last_contact_at ?? null;

  if (brief.isError) {
    return (
      <div className={styles.page}>
        <BackLink onClick={() => navigate("/people")} />
        <ErrorState error={brief.error} onRetry={() => void brief.refetch()} />
      </div>
    );
  }
  if (brief.isLoading && !b) {
    return (
      <div className={styles.page}>
        <BackLink onClick={() => navigate("/people")} />
        <Skeleton height={90} />
        <Skeleton height={64} />
        <Skeleton height={320} />
      </div>
    );
  }
  if (!b || b.records === 0) {
    return (
      <div className={styles.page}>
        <BackLink onClick={() => navigate("/people")} />
        <EmptyState
          title="Nothing to show for this person"
          description="No records are attributed to them, or your token cannot see identity data."
        />
      </div>
    );
  }

  // The dominant channel decides the person's accent colour, derived from shape.
  const topChannel = Array.isArray(b?.top_channels) && b.top_channels[0] ? b.top_channels[0].channel : "";
  const accentPlugin = pluginOf.get(topChannel) ?? "";
  const accentIndex = chIndex.get(topChannel) ?? 0;

  const specs = buildSpecs(id);

  const ask = (): void => {
    const params = new URLSearchParams();
    params.set("ask", `Tell me about ${name} — how often do we talk, through what, and about what?`);
    navigate(`/query?${params.toString()}`);
  };

  return (
    <div className={styles.page}>
      <BackLink onClick={() => navigate("/people")} />

      <PersonHeader
        name={name}
        handle={b.handle}
        accentPlugin={accentPlugin}
        accentIndex={accentIndex}
        ids={idsQ.data?.rows ?? []}
        pluginOf={pluginOf}
        onAsk={ask}
      />

      <div className={styles.stats}>
        <Stat label="Records" value={b.records.toLocaleString("en-US")} />
        <Stat label="From them" value={(b.records - b.messages_to).toLocaleString("en-US")} />
        <Stat label="To them" value={b.messages_to.toLocaleString("en-US")} />
        <Stat label="Exchanges" value={b.exchanges.toLocaleString("en-US")} />
        <Stat label="First contact" value={firstAt ? formatInTz(firstAt, tz, { dateStyle: "medium" }) : "—"} />
        <Stat label="Last contact" value={lastAt ? relative(lastAt, now) : "—"} hint={lastAt ? formatInTz(lastAt, tz, { dateStyle: "medium" }) : undefined} />
        {b.circle_rank != null && <Stat label="In your circle" value={`#${b.circle_rank}`} />}
      </div>

      <div className={styles.grid}>
        <div className={styles.wide}>
          <RelationshipTimeline id={id} spec={specs.timeline} scope={scope} pluginOf={pluginOf} chIndex={chIndex} height={232} />
        </div>
        <ChartPanel spec={specs.rhythm} scope={scope} board={`person-${id}`} height={240} />
        <ReplyLatency id={id} />
        <ChannelMix id={id} pluginOf={pluginOf} chIndex={chIndex} onOpen={(c) => navigate(`/c/${encodeURIComponent(c)}`)} />
        <RelatedPeople id={id} onOpen={(pid) => navigate(`/people/${pid}`)} />
        <ThreadsPanel id={id} pluginOf={pluginOf} chIndex={chIndex} now={now} onOpen={openRecord} />
        <div className={styles.wide}>
          <RecentRecords id={id} pluginOf={pluginOf} chIndex={chIndex} tz={tz} onOpen={openRecord} />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ header */

function PersonHeader({
  name,
  handle,
  accentPlugin,
  accentIndex,
  ids,
  pluginOf,
  onAsk,
}: {
  name: string;
  handle: string | null;
  accentPlugin: string;
  accentIndex: number;
  ids: readonly IdRow[];
  pluginOf: Map<string, string>;
  onAsk: () => void;
}): ReactNode {
  const { cssFor } = useSourceColor();
  const accent = accentPlugin ? cssFor(accentPlugin, accentIndex) : "var(--brass)";
  const initials =
    name
      .split(/\s+/)
      .slice(0, 2)
      .map((w) => w[0]?.toUpperCase() ?? "")
      .join("") || "?";
  // One chip per distinct identifier system, with its strongest value.
  const seen = new Set<string>();
  const shown = ids.filter((i) => (seen.has(i.system) ? false : (seen.add(i.system), true))).slice(0, 8);
  return (
    <header className={styles.header}>
      <div className={styles.identity}>
        <div className={styles.avatar} style={{ background: accent, color: accent }}>
          {initials}
        </div>
        <div style={{ minWidth: 0 }}>
          <h1 className={styles.name}>{name}</h1>
          {handle && <p className={styles.handle}>{handle}</p>}
          {shown.length > 0 && (
            <div className={styles.ids}>
              {shown.map((i, k) => {
                const plugin = pluginOf.get(i.system) ?? i.system;
                return (
                  <span className={styles.idChip} key={`${i.system}-${i.value}-${k}`}>
                    <SourceDot plugin={plugin} size={7} />
                    <span className={styles.idSys}>{i.system}</span>
                    <span className={styles.idVal}>{i.value}</span>
                  </span>
                );
              })}
            </div>
          )}
        </div>
      </div>
      <button type="button" className={styles.askBtn} onClick={onAsk}>
        <MessageCircleQuestion size={15} />
        Ask about this person
      </button>
    </header>
  );
}

/* -------------------------------------------------------------- reply latency */

function ReplyLatency({ id }: { id: number }): ReactNode {
  const { data: boot } = useBootstrap();
  const owner = boot?.owner_person_id ?? null;
  const statement =
    owner == null
      ? null
      : `WITH turns AS (SELECT thread_id, happened_at, CASE WHEN person_id = ${id | 0} THEN 'them' WHEN person_id = ${owner | 0} THEN 'me' END AS who ` +
        `FROM api.messages WHERE person_id IN (${id | 0}, ${owner | 0}) ` +
        `AND thread_id IN (SELECT DISTINCT thread_id FROM api.messages WHERE person_id = ${id | 0})), ` +
        `seq AS (SELECT who, lag(who) OVER w AS prev, extract(epoch FROM happened_at - lag(happened_at) OVER w) AS gap ` +
        `FROM turns WINDOW w AS (PARTITION BY thread_id ORDER BY happened_at)) ` +
        `SELECT CASE WHEN prev = 'them' AND who = 'me' THEN 'you' WHEN prev = 'me' AND who = 'them' THEN 'them' END AS dir, ` +
        `count(*)::int AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)::int AS median_s ` +
        `FROM seq WHERE gap > 0 AND gap < 86400 GROUP BY 1`;
  const q = useSql<LatRow>(statement, { priority: "normal", keyParts: ["lat", id], rowCap: 10 });
  const rows = (q.data?.rows ?? []).filter((r) => r.dir === "you" || r.dir === "them");
  const you = rows.find((r) => r.dir === "you");
  const them = rows.find((r) => r.dir === "them");
  const maxMed = Math.max(1, ...rows.map((r) => num(r.median_s)));

  return (
    <Panel title="Reply latency" subtitle="Median time to answer, both ways, within a day">
      {q.isLoading ? (
        <Skeleton height={90} />
      ) : rows.length === 0 ? (
        <EmptyState compact title="No back-and-forth" description="There isn’t enough two-sided conversation to measure." />
      ) : (
        <div className={styles.latency}>
          <LatencyRow label="You reply" row={you} maxMed={maxMed} color="var(--brass)" />
          <LatencyRow label="They reply" row={them} maxMed={maxMed} color="var(--patina)" />
        </div>
      )}
    </Panel>
  );
}

function LatencyRow({ label, row, maxMed, color }: { label: string; row?: LatRow; maxMed: number; color: string }): ReactNode {
  const med = row ? num(row.median_s) : 0;
  return (
    <div className={styles.latRow}>
      <span className={styles.latLabel}>{label}</span>
      <span className={styles.latVal}>{row ? humanDuration(med) : "—"}</span>
      <span className={styles.latBarTrack}>
        <span className={styles.latBar} style={{ width: `${Math.round((med / maxMed) * 100)}%`, background: color }} />
      </span>
      <span className={styles.latN}>{row ? `${num(row.n).toLocaleString("en-US")}×` : ""}</span>
    </div>
  );
}

/* --------------------------------------------------------------- channel mix */

function ChannelMix({
  id,
  pluginOf,
  chIndex,
  onOpen,
}: {
  id: number;
  pluginOf: Map<string, string>;
  chIndex: Map<string, number>;
  onOpen: (channel: string) => void;
}): ReactNode {
  const { cssFor } = useSourceColor();
  const q = useSql<{ channel: string; n: string }>(
    `SELECT channel, count(*)::int AS n FROM api.messages WHERE person_id = ${id | 0} GROUP BY 1 ORDER BY n DESC, 1 LIMIT 14`,
    { priority: "normal", keyParts: ["mix", id], rowCap: 50 },
  );
  const rows = q.data?.rows ?? [];
  const max = Math.max(1, ...rows.map((r) => num(r.n)));
  return (
    <Panel title="Channel mix" subtitle="Where this relationship lives">
      {q.isLoading ? (
        <Skeleton height={120} />
      ) : rows.length === 0 ? (
        <EmptyState compact title="No channels" />
      ) : (
        <div className={styles.mix}>
          {rows.map((r) => {
            const plugin = pluginOf.get(r.channel) ?? r.channel;
            const ci = chIndex.get(r.channel) ?? 0;
            return (
              <button key={r.channel} type="button" className={styles.mixRow} onClick={() => onOpen(r.channel)} title={`Open ${r.channel}`}>
                <span className={styles.mixName}>
                  <SourceDot plugin={plugin} channelIndex={ci} size={8} />
                  {r.channel}
                </span>
                <span className={styles.mixTrack}>
                  <span className={styles.mixFill} style={{ width: `${Math.round((num(r.n) / max) * 100)}%`, background: cssFor(plugin, ci) }} />
                </span>
                <span className={styles.mixN}>{num(r.n).toLocaleString("en-US")}</span>
              </button>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------------- related people */

function RelatedPeople({ id, onOpen }: { id: number; onOpen: (pid: number) => void }): ReactNode {
  const q = useSql<RelRow>(
    `SELECT c.other_person_id::int AS id, p.display_name AS name, c.exchanges::int AS exchanges, ` +
      `c.shared_conversations::int AS shared, c.rank::int AS rank ` +
      `FROM api.circle(${id | 0}, 'absolute', NULL, NULL, 12) c JOIN api.people p ON p.id = c.other_person_id ORDER BY c.rank`,
    { priority: "normal", keyParts: ["related", id], rowCap: 20 },
  );
  const rows = q.data?.rows ?? [];
  return (
    <Panel title="Often together" subtitle="People who co-occur in the same threads">
      {q.isLoading ? (
        <Skeleton height={120} />
      ) : rows.length === 0 ? (
        <EmptyState compact title="No shared threads" description="No one else appears in threads with them." />
      ) : (
        <div className={styles.list}>
          {rows.map((r) => (
            <button key={r.id} type="button" className={styles.item} onClick={() => onOpen(Number(r.id))}>
              <span className={styles.relRank}>#{r.rank}</span>
              <div className={styles.itemMain}>
                <div className={styles.relName}>{r.name ?? `Person ${r.id}`}</div>
                <div className={styles.relMeta}>
                  {num(r.exchanges).toLocaleString("en-US")} exchanges · {num(r.shared).toLocaleString("en-US")} shared
                </div>
              </div>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}

/* -------------------------------------------------------------------- threads */

function ThreadsPanel({
  id,
  pluginOf,
  chIndex,
  now,
  onOpen,
}: {
  id: number;
  pluginOf: Map<string, string>;
  chIndex: Map<string, number>;
  now: number;
  onOpen: (recordId: number, channel: string) => void;
}): ReactNode {
  const q = useSql<ThreadRow>(
    `SELECT thread_id::int AS thread_id, mode() WITHIN GROUP (ORDER BY channel) AS channel, ` +
      `max(thread_title) AS title, count(*)::int AS n, max(happened_at) AS last_at, ` +
      `(array_agg(id ORDER BY happened_at DESC))[1]::int AS last_id ` +
      `FROM api.messages WHERE person_id = ${id | 0} GROUP BY thread_id ORDER BY last_at DESC LIMIT 24`,
    { priority: "normal", keyParts: ["threads", id], rowCap: 50 },
  );
  const rows = q.data?.rows ?? [];
  return (
    <Panel title="Threads they’re in" subtitle={`${rows.length} most recent`}>
      {q.isLoading ? (
        <Skeleton height={140} />
      ) : rows.length === 0 ? (
        <EmptyState compact title="No threads" />
      ) : (
        <div className={styles.list}>
          {rows.map((r) => {
            const plugin = pluginOf.get(r.channel) ?? r.channel;
            return (
              <button key={r.thread_id} type="button" className={styles.item} onClick={() => onOpen(Number(r.last_id), r.channel)}>
                <SourceDot plugin={plugin} channelIndex={chIndex.get(r.channel) ?? 0} size={9} />
                <div className={styles.itemMain}>
                  <div className={styles.itemTop}>
                    <span className={styles.itemTitle}>{r.title || `Thread ${r.thread_id}`}</span>
                    <span className={styles.itemTime}>{relative(r.last_at, now)}</span>
                  </div>
                  <div className={styles.itemMeta}>
                    <span>{r.channel}</span>
                    <span>·</span>
                    <span>{num(r.n).toLocaleString("en-US")} records</span>
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------------- recent records */

function RecentRecords({
  id,
  pluginOf,
  chIndex,
  tz,
  onOpen,
}: {
  id: number;
  pluginOf: Map<string, string>;
  chIndex: Map<string, number>;
  tz: string;
  onOpen: (recordId: number, channel: string) => void;
}): ReactNode {
  const q = useSql<TimelineRow>(
    `SELECT message_id::int AS message_id, channel, content, record_time, side ` +
      `FROM api.person_timeline(${id | 0}, NULL, NULL, 40)`,
    { priority: "normal", keyParts: ["timeline", id], rowCap: 60 },
  );
  const rows = q.data?.rows ?? [];
  return (
    <Panel title="Recent records" subtitle="Newest first — from them and to them">
      {q.isLoading ? (
        <Skeleton height={160} />
      ) : rows.length === 0 ? (
        <EmptyState compact title="No records" />
      ) : (
        <div className={styles.list}>
          {rows.map((r, k) => {
            const plugin = pluginOf.get(r.channel) ?? r.channel;
            return (
              <button key={`${r.message_id}-${k}`} type="button" className={styles.item} onClick={() => onOpen(Number(r.message_id), r.channel)}>
                <SourceDot plugin={plugin} channelIndex={chIndex.get(r.channel) ?? 0} size={9} />
                <div className={styles.itemMain}>
                  <div className={styles.itemTop}>
                    <span className={styles.itemMeta}>
                      <span className={styles.sideNote}>{r.side === "from" ? "them" : "you"}</span>
                      {r.channel}
                    </span>
                    <span className={styles.itemTime} title={formatInTz(r.record_time, tz)}>
                      {formatInTz(r.record_time, tz, { dateStyle: "medium", timeStyle: "short" })}
                    </span>
                  </div>
                  <div className={styles.itemBody}>
                    <Sensitive>{r.content}</Sensitive>
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

/* ----------------------------------------------------------------- spec builders */

function buildSpecs(id: number): { timeline: ViewSpec; rhythm: ViewSpec } {
  const where = "person_id = :person AND happened_at >= :from AND happened_at < :to";
  const timeline: ViewSpec = {
    v: 1,
    title: "Relationship over time",
    why: "Weekly volume, coloured by the channel it came through — brush to set the scope",
    lens: "timeseries",
    source: {
      sql:
        `SELECT date_trunc(:grain, happened_at, :tz) AS bucket, channel AS series, count(*) AS n ` +
        `FROM api.messages WHERE ${where} GROUP BY 1, 2 ORDER BY 1`,
      row_cap: 4000,
    },
    encode: { x: "bucket", series: "series", y: "n" },
    options: { bucket_budget: 320, min_grain: "week" },
    provenance: { by: "user", sampled: false },
  };
  const rhythm: ViewSpec = {
    v: 1,
    title: "When they reach you",
    why: "Records by weekday and hour, in your timezone",
    lens: "weekday_hour",
    source: {
      sql:
        `SELECT extract(isodow FROM happened_at AT TIME ZONE :tz)::int AS weekday, ` +
        `extract(hour FROM happened_at AT TIME ZONE :tz)::int AS hour, count(*) AS n ` +
        `FROM api.messages WHERE ${where} GROUP BY 1, 2`,
      row_cap: 200,
    },
    encode: { weekday: "weekday", hour: "hour", value: "n" },
    provenance: { by: "user", sampled: false },
  };
  return { timeline, rhythm };
}

/* ------------------------------------------------------------------- helpers */

function BackLink({ onClick }: { onClick: () => void }): ReactNode {
  return (
    <button type="button" className={styles.back} onClick={onClick}>
      <ArrowLeft size={14} /> All people
    </button>
  );
}

function humanDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

/* ---------------------------------------------------------------------- types */

interface BriefRow {
  name: string | null;
  handle: string | null;
  records: number;
  messages_to: number;
  exchanges: number;
  first_contact_at: string | null;
  last_contact_at: string | null;
  circle_rank: number | null;
  top_channels: { channel: string; messages: number }[] | null;
}
interface IdRow {
  system: string;
  kind: string;
  value: string;
  status: string;
}
interface LatRow {
  dir: string | null;
  n: string;
  median_s: string;
}
interface RelRow {
  id: string;
  name: string | null;
  exchanges: string;
  shared: string;
  rank: string;
}
interface ThreadRow {
  thread_id: string;
  channel: string;
  title: string | null;
  n: string;
  last_at: string;
  last_id: string;
}
interface TimelineRow {
  message_id: string;
  channel: string;
  content: string;
  record_time: string;
  side: string;
}
