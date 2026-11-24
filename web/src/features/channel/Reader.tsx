/*
 * The archetype-native reader. One keyset-paginated, virtualised record stream,
 * scoped by the global time range and searchable through the server's keyword
 * route, drawn in the form its archetype reads best: conversation bubbles,
 * transcript turns, a document list + reading pane, feed cards, a role-aware
 * table, or an interval agenda. Every record opens in the drawer; all record
 * text is wrapped in Sensitive and rendered as text, never HTML.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Search, CalendarDays, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import type { ChannelRow } from "@/api/types";
import { EmptyState, ErrorState, Input, Panel, Sensitive, Skeleton, StaticChip } from "@/ui";
import { usePrefs } from "@/store/prefs";
import { useScopes } from "@/store/scopes";
import { num } from "@/api";
import type { Archetypes, ChannelMeta, LensCandidate, RoleId, RoleMap } from "@/lens";
import { useOpenRecord } from "@/features/record";
import { ENVELOPE, useRecords } from "./useRecords";
import { bindingColumns, normalizeRecord, readerBinding, type ReaderRecord } from "./records";
import { clip, clockTime, dayKey, dayLabel, dur, offset } from "./readerFormat";
import styles from "./Reader.module.css";

type ReaderKind = "conversation" | "transcript" | "document" | "feed" | "table" | "agenda";

function readerKind(a: Archetypes): ReaderKind {
  switch (a.primary) {
    case "conversation":
      return "conversation";
    case "transcript":
      return "transcript";
    case "document":
      return "document";
    case "feed_item":
      return "feed";
    case "interval":
      return "agenda";
    default:
      return "table";
  }
}

export interface ReaderProps {
  meta: ChannelRow;
  cmeta: ChannelMeta;
  archetypes: Archetypes;
  roles: RoleMap;
  readerCandidate: LensCandidate | undefined;
  channelIndex: number;
}

export function Reader(props: ReaderProps): ReactNode {
  const { meta, cmeta, archetypes, roles } = props;
  const kind = readerKind(archetypes);
  const { from, to } = useScopes();
  const [rawSearch, setRawSearch] = useState("");
  const [search, setSearch] = useState("");
  const [jump, setJump] = useState<string | null>(null);

  // Debounce the search box so keystrokes do not fire a route per character.
  useEffect(() => {
    const id = window.setTimeout(() => setSearch(rawSearch.trim()), 300);
    return () => window.clearTimeout(id);
  }, [rawSearch]);

  const { binding, columns, order } = useMemo(() => {
    // Only real columns may be selected — the reader lens can bind a channel to a
    // computed alias (e.g. a coalesced "author") that exists in its own SQL, not
    // the view, so keep declared fields and the envelope and drop anything else.
    const declaredNames = (cmeta.fields ?? []).map((f) => f.name);
    const valid = new Set<string>([...ENVELOPE, ...declaredNames]);
    if (kind === "table" || kind === "agenda") {
      const declared = declaredNames.filter((name) => !roles[name]?.plumbing && !roles[name]?.hidden);
      const cols = ["happened_at", "ended_at", ...declared].filter((c) => valid.has(c)).slice(0, 12);
      return { binding: readerBinding(props.readerCandidate, roles), columns: cols, order: "desc" as "desc" | "asc" };
    }
    const b = readerBinding(props.readerCandidate, roles);
    return { binding: b, columns: bindingColumns(b).filter((c) => valid.has(c)), order: "desc" as const };
  }, [kind, cmeta.fields, roles, props.readerCandidate]);

  const records = useRecords({
    channel: meta.name,
    view: meta.view,
    columns,
    from,
    to,
    order,
    search,
    startBefore: jump,
    enabled: meta.rows > 0,
  });

  const recs = useMemo(() => records.rows.map((r) => normalizeRecord(r, binding)), [records.rows, binding]);

  if (meta.rows === 0) {
    return (
      <Panel title="Records" as="section">
        <EmptyState title="Awaiting first record" description="When this channel ingests its first record it will appear here, read in its archetype's native form." />
      </Panel>
    );
  }

  const toolbar = (
    <div className={styles.toolbar}>
      <div className={styles.searchBox}>
        <Search size={14} className={styles.searchIcon} aria-hidden />
        <Input
          type="search"
          value={rawSearch}
          placeholder={`Search ${meta.name}…`}
          onChange={(e) => setRawSearch(e.currentTarget.value)}
          aria-label="Search records"
        />
      </div>
      <label className={styles.jump}>
        <CalendarDays size={14} aria-hidden />
        <input
          type="date"
          className={styles.dateInput}
          value={jump ? jump.slice(0, 10) : ""}
          onChange={(e) => setJump(e.currentTarget.value ? `${e.currentTarget.value}T23:59:59Z` : null)}
          aria-label="Jump to date"
        />
        {jump ? (
          <button type="button" className={styles.clearJump} onClick={() => setJump(null)} aria-label="Clear jump">
            <X size={12} />
          </button>
        ) : null}
      </label>
    </div>
  );

  return (
    <Panel
      as="section"
      title="Records"
      subtitle={records.ranked ? "Ranked by relevance" : "Newest first"}
      actions={toolbar}
      flush
      footer={
        <div className={styles.footer}>
          <span>
            {recs.length.toLocaleString("en-US")} shown
            {records.hasNext && !records.ranked ? " · scroll for more" : ""}
            {records.ranked ? " · top matches" : ""}
          </span>
        </div>
      }
    >
      {records.isError ? (
        <div className={styles.pad}>
          <ErrorState error={records.error} onRetry={() => void records.refetch()} />
        </div>
      ) : records.isLoading ? (
        <div className={styles.pad}>
          <Skeleton height={64} lines={5} />
        </div>
      ) : recs.length === 0 ? (
        <div className={styles.pad}>
          <EmptyState title={search ? "No matches" : "Nothing in range"} description={search ? "No records matched that search in the current time range." : "No records fall inside the current time scope."} compact />
        </div>
      ) : kind === "document" ? (
        <DocumentReader recs={recs} records={records} />
      ) : (
        <StreamReader kind={kind} recs={recs} records={records} roles={roles} fields={cmeta.fields} />
      )}
    </Panel>
  );
}

/* ---- the infinite virtualised stream ------------------------------------- */

interface StreamItem {
  kind: "divider" | "record";
  key: string;
  label?: string;
  rec?: ReaderRecord;
}

function buildItems(recs: ReaderRecord[], tz: string, grouped: boolean): StreamItem[] {
  if (!grouped) return recs.map((r) => ({ kind: "record", key: r.id || r.time || Math.random().toString(), rec: r }));
  const out: StreamItem[] = [];
  let last = "";
  for (const r of recs) {
    const k = dayKey(r.time, tz);
    if (k !== last) {
      out.push({ kind: "divider", key: `d-${k}`, label: dayLabel(r.time, tz) });
      last = k;
    }
    out.push({ kind: "record", key: r.id || `${k}-${out.length}`, rec: r });
  }
  return out;
}

function StreamReader({
  kind,
  recs,
  records,
  roles,
  fields,
}: {
  kind: Exclude<ReaderKind, "document">;
  recs: ReaderRecord[];
  records: ReturnType<typeof useRecords>;
  roles: RoleMap;
  fields: ChannelMeta["fields"];
}): ReactNode {
  const tz = usePrefs((s) => s.tz);
  const openRecord = useOpenRecord();
  const scrollRef = useRef<HTMLDivElement>(null);
  const grouped = kind === "conversation" || kind === "agenda";
  const items = useMemo(() => buildItems(recs, tz, grouped), [recs, tz, grouped]);
  const estimate = kind === "conversation" ? 76 : kind === "feed" ? 96 : kind === "table" ? 34 : 60;

  const v = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => estimate,
    overscan: 10,
    getItemKey: (i) => items[i]?.key ?? i,
  });

  // Fetch the next keyset page as the tail scrolls into view.
  const vItems = v.getVirtualItems();
  useEffect(() => {
    const last = vItems[vItems.length - 1];
    if (!last) return;
    if (last.index >= items.length - 6 && records.hasNext && !records.isFetchingNext) {
      void records.fetchNext();
    }
  }, [vItems, items.length, records]);

  const tableCols = kind === "table" ? tableColumns(fields, roles) : [];

  return (
    <div ref={scrollRef} className={styles.scroll}>
      {kind === "table" ? <TableHead cols={tableCols} /> : null}
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {vItems.map((vi) => {
          const item = items[vi.index]!;
          return (
            <div
              key={vi.key}
              ref={v.measureElement}
              data-index={vi.index}
              className={styles.vrow}
              style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}
            >
              {item.kind === "divider" ? (
                <div className={styles.divider}>{item.label}</div>
              ) : kind === "conversation" ? (
                <Bubble rec={item.rec!} tz={tz} onOpen={openRecord} />
              ) : kind === "transcript" ? (
                <Turn rec={item.rec!} tz={tz} onOpen={openRecord} />
              ) : kind === "feed" ? (
                <FeedCard rec={item.rec!} tz={tz} onOpen={openRecord} />
              ) : kind === "agenda" ? (
                <AgendaRow rec={item.rec!} tz={tz} onOpen={openRecord} />
              ) : (
                <TableRow rec={item.rec!} cols={tableCols} tz={tz} onOpen={openRecord} />
              )}
            </div>
          );
        })}
      </div>
      {records.isFetchingNext ? <div className={styles.loadMore}>Loading…</div> : null}
    </div>
  );
}

/* ---- conversation --------------------------------------------------------- */

function Bubble({ rec, tz, onOpen }: { rec: ReaderRecord; tz: string; onOpen: (id: string, channel?: string) => void }): ReactNode {
  const images = num(rec.raw.images);
  const imgCount = Array.isArray(rec.raw.images) ? rec.raw.images.length : Number.isFinite(images) ? images : 0;
  return (
    <div className={`${styles.bubbleRow} ${rec.mine ? styles.mine : ""}`}>
      <div className={styles.bubble} role="button" tabIndex={0} onClick={() => onOpen(rec.id, rec.raw.channel as string)} onKeyDown={(e) => e.key === "Enter" && onOpen(rec.id, rec.raw.channel as string)}>
        <div className={styles.bubbleHead}>
          {rec.author ? (
            <Sensitive>
              <span className={styles.author}>{clip(rec.author, 32)}</span>
            </Sensitive>
          ) : null}
          <span className={styles.time}>{clockTime(rec.time, tz)}</span>
        </div>
        {rec.text ? (
          <Sensitive block>
            <span className={styles.bubbleText}>{rec.text}</span>
          </Sensitive>
        ) : (
          <span className={styles.muted}>no text</span>
        )}
        <div className={styles.attachments}>
          {imgCount > 0 ? <StaticChip>{imgCount} {imgCount === 1 ? "image" : "images"}</StaticChip> : null}
          {rec.status ? <StaticChip>{clip(rec.status, 20)}</StaticChip> : null}
        </div>
      </div>
    </div>
  );
}

/* ---- transcript ----------------------------------------------------------- */

function Turn({ rec, tz, onOpen }: { rec: ReaderRecord; tz: string; onOpen: (id: string, channel?: string) => void }): ReactNode {
  return (
    <div className={styles.turn} role="button" tabIndex={0} onClick={() => onOpen(rec.id, rec.raw.channel as string)} onKeyDown={(e) => e.key === "Enter" && onOpen(rec.id, rec.raw.channel as string)}>
      <div className={styles.turnMeta}>
        {rec.startOffset !== null ? <span className={styles.offset}>{offset(rec.startOffset)}</span> : <span className={styles.offset}>{clockTime(rec.time, tz)}</span>}
        {rec.speaker ? (
          <Sensitive>
            <span className={styles.speaker}>{clip(rec.speaker, 28)}</span>
          </Sensitive>
        ) : null}
        {rec.place ? (
          <Sensitive>
            <span className={styles.place}>{clip(rec.place, 24)}</span>
          </Sensitive>
        ) : null}
      </div>
      <Sensitive block>
        <span className={styles.turnText}>{rec.text ?? ""}</span>
      </Sensitive>
    </div>
  );
}

/* ---- feed ----------------------------------------------------------------- */

function FeedCard({ rec, tz, onOpen }: { rec: ReaderRecord; tz: string; onOpen: (id: string, channel?: string) => void }): ReactNode {
  const seconds = num(rec.raw.duration_s);
  return (
    <div className={styles.card} role="button" tabIndex={0} onClick={() => onOpen(rec.id, rec.raw.channel as string)} onKeyDown={(e) => e.key === "Enter" && onOpen(rec.id, rec.raw.channel as string)}>
      <div className={styles.cardHead}>
        {rec.actor || rec.author ? (
          <Sensitive>
            <span className={styles.cardActor}>{clip(rec.actor || rec.author || "", 32)}</span>
          </Sensitive>
        ) : null}
        <span className={styles.time}>{clockTime(rec.time, tz)}</span>
        {Number.isFinite(seconds) ? <span className={styles.dura}>{dur(seconds)}</span> : null}
        {rec.status ? <StaticChip>{clip(rec.status, 18)}</StaticChip> : null}
      </div>
      {rec.title ? (
        <Sensitive block>
          <span className={styles.cardTitle}>{clip(rec.title, 160)}</span>
        </Sensitive>
      ) : rec.text ? (
        <Sensitive block>
          <span className={styles.cardTitle}>{clip(rec.text, 160)}</span>
        </Sensitive>
      ) : null}
      {rec.url ? <InertLink url={rec.url} /> : null}
    </div>
  );
}

/* ---- agenda (interval) ---------------------------------------------------- */

function AgendaRow({ rec, tz, onOpen }: { rec: ReaderRecord; tz: string; onOpen: (id: string, channel?: string) => void }): ReactNode {
  const label = rec.title ?? rec.status ?? rec.text ?? rec.speaker ?? "record";
  const endS = rec.endedAt ? clockTime(rec.endedAt, tz) : "";
  const span = rec.endedAt && rec.time ? (new Date(rec.endedAt).getTime() - new Date(rec.time).getTime()) / 1000 : null;
  return (
    <div className={styles.agenda} role="button" tabIndex={0} onClick={() => onOpen(rec.id, rec.raw.channel as string)} onKeyDown={(e) => e.key === "Enter" && onOpen(rec.id, rec.raw.channel as string)}>
      <div className={styles.agendaTime}>
        <span className={styles.agendaStart}>{clockTime(rec.time, tz)}</span>
        {endS ? <span className={styles.agendaEnd}>{endS}</span> : null}
      </div>
      <div className={styles.agendaBody}>
        <Sensitive>
          <span className={styles.agendaLabel}>{clip(label, 80)}</span>
        </Sensitive>
        <div className={styles.agendaMeta}>
          {span !== null && span > 0 ? <span>{dur(span)}</span> : null}
          {rec.status && rec.status !== label ? <StaticChip>{clip(rec.status, 18)}</StaticChip> : null}
          {rec.place ? (
            <Sensitive>
              <span className={styles.place}>{clip(rec.place, 24)}</span>
            </Sensitive>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/* ---- role-aware table ----------------------------------------------------- */

interface TableCol {
  name: string;
  role: RoleId | undefined;
  unit: string | undefined;
  align: "left" | "right";
}

function tableColumns(fields: ChannelMeta["fields"], roles: RoleMap): TableCol[] {
  const cols: TableCol[] = [{ name: "happened_at", role: "time.start", unit: undefined, align: "left" }];
  for (const f of fields ?? []) {
    const r = roles[f.name];
    if (r?.plumbing || r?.hidden) continue;
    const measure = r && /percent|probability|score|count|currency|duration|distance|mass|measure/.test(r.role);
    cols.push({ name: f.name, role: r?.role, unit: r?.unit, align: measure ? "right" : "left" });
    if (cols.length >= 8) break;
  }
  return cols;
}

function TableHead({ cols }: { cols: TableCol[] }): ReactNode {
  const grid = cols.map((c) => (c.align === "right" ? "minmax(80px,1fr)" : "minmax(120px,1.6fr)")).join(" ");
  return (
    <div className={styles.thead} style={{ gridTemplateColumns: grid }}>
      {cols.map((c) => (
        <div key={c.name} className={`${styles.th} ${c.align === "right" ? styles.right : ""}`} title={c.name}>
          {humanLabel(c.name)}
        </div>
      ))}
    </div>
  );
}

function TableRow({ rec, cols, tz, onOpen }: { rec: ReaderRecord; cols: TableCol[]; tz: string; onOpen: (id: string, channel?: string) => void }): ReactNode {
  const grid = cols.map((c) => (c.align === "right" ? "minmax(80px,1fr)" : "minmax(120px,1.6fr)")).join(" ");
  return (
    <div
      className={styles.trow}
      style={{ gridTemplateColumns: grid }}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(rec.id, rec.raw.channel as string)}
      onKeyDown={(e) => e.key === "Enter" && onOpen(rec.id, rec.raw.channel as string)}
    >
      {cols.map((c) => (
        <div key={c.name} className={`${styles.td} ${c.align === "right" ? styles.right : ""}`}>
          {renderCell(rec.raw[c.name], c, tz)}
        </div>
      ))}
    </div>
  );
}

function renderCell(value: unknown, col: TableCol, tz: string): ReactNode {
  if (value === null || value === undefined || value === "") return <span className={styles.muted}>—</span>;
  const role = col.role;
  if (col.name === "happened_at" || role === "time.start" || role === "time.end" || role === "time.deadline") {
    return <span className="tnum">{clockTimeOrDay(String(value), tz)}</span>;
  }
  if (typeof value === "object") {
    return <span className={styles.json}>{Array.isArray(value) ? `[${value.length}]` : "{…}"}</span>;
  }
  if (role === "url" && typeof value === "string" && /^https?:\/\//i.test(value)) return <InertLink url={value} />;
  const n = num(value);
  const numeric = role && /percent|probability|score|count|currency|duration|distance|mass|measure/.test(role);
  if (numeric && Number.isFinite(n)) {
    return <span className="tnum">{formatMeasure(n, col.unit, role)}</span>;
  }
  const text = String(value);
  const sensitive = role === "body" || role === "title" || role === "category.speaker" || role === "actor.label" || role === "geo.label";
  const node = <span className={styles.cellText}>{clip(text, 80)}</span>;
  return sensitive ? <Sensitive>{node}</Sensitive> : node;
}

function clockTimeOrDay(iso: string, tz: string): string {
  const t = clockTime(iso, tz);
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    const day = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "2-digit", timeZone: tz }).format(d);
    return `${day} ${t}`;
  } catch {
    return t;
  }
}

function formatMeasure(n: number, unit: string | undefined, role: RoleId | undefined): string {
  if (role === "percent" || role === "probability" || unit === "pct") {
    const pct = n > 1.0001 ? n : n * 100;
    return `${Math.abs(pct) >= 10 ? Math.round(pct) : Math.round(pct * 10) / 10}%`;
  }
  if (role === "duration" || unit === "s") return dur(n);
  const u = unit && /^[a-z%]{1,4}$/i.test(unit) ? ` ${unit}` : "";
  const fixed = Number.isInteger(n) ? n.toLocaleString("en-US") : Math.abs(n) >= 100 ? Math.round(n).toLocaleString("en-US") : n.toFixed(2);
  return `${fixed}${u}`;
}

function humanLabel(name: string): string {
  const s = name.replace(/[_\s]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : name;
}

/* ---- document (list + reading pane) --------------------------------------- */

function DocumentReader({ recs, records }: { recs: ReaderRecord[]; records: ReturnType<typeof useRecords> }): ReactNode {
  const tz = usePrefs((s) => s.tz);
  const openRecord = useOpenRecord();
  const [selected, setSelected] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const current = recs.find((r) => r.id === selected) ?? recs[0];

  const v = useVirtualizer({
    count: recs.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 68,
    overscan: 10,
    getItemKey: (i) => recs[i]?.id ?? i,
  });
  const vItems = v.getVirtualItems();
  useEffect(() => {
    const last = vItems[vItems.length - 1];
    if (last && last.index >= recs.length - 6 && records.hasNext && !records.isFetchingNext) void records.fetchNext();
  }, [vItems, recs.length, records]);

  return (
    <div className={styles.docSplit}>
      <div ref={scrollRef} className={styles.docList}>
        <div style={{ height: v.getTotalSize(), position: "relative" }}>
          {vItems.map((vi) => {
            const r = recs[vi.index]!;
            return (
              <button
                type="button"
                key={vi.key}
                className={`${styles.docItem} ${current?.id === r.id ? styles.docItemActive : ""}`}
                style={{ position: "absolute", top: 0, left: 0, width: "100%", height: vi.size, transform: `translateY(${vi.start}px)` }}
                onClick={() => setSelected(r.id)}
              >
                <Sensitive>
                  <span className={styles.docItemTitle}>{clip(r.title ?? r.text ?? "Untitled", 70)}</span>
                </Sensitive>
                <span className={styles.docItemMeta}>
                  {r.author ? (
                    <Sensitive>
                      <span>{clip(r.author, 24)}</span>
                    </Sensitive>
                  ) : null}
                  <span className="tnum">{clockTimeOrDay(r.time ?? "", tz)}</span>
                </span>
              </button>
            );
          })}
        </div>
        {records.isFetchingNext ? <div className={styles.loadMore}>Loading…</div> : null}
      </div>
      <div className={styles.docPane}>
        {current ? (
          <article className={styles.doc}>
            <div className={styles.docPaneHead}>
              {current.title ? (
                <Sensitive block>
                  <h3 className={styles.docTitle}>{current.title}</h3>
                </Sensitive>
              ) : null}
              <div className={styles.docPaneMeta}>
                {current.author ? (
                  <Sensitive>
                    <span>{current.author}</span>
                  </Sensitive>
                ) : null}
                <span className="tnum">{clockTimeOrDay(current.time ?? "", tz)}</span>
                <button type="button" className={styles.openFull} onClick={() => openRecord(current.id, current.raw.channel as string)}>
                  Open record
                </button>
              </div>
              {current.url ? <InertLink url={current.url} /> : null}
            </div>
            <Sensitive block>
              <div className={styles.docBody}>
                <ReactMarkdown allowedElements={MD_ALLOWED} unwrapDisallowed components={MD_COMPONENTS}>
                  {current.text ?? "_No content._"}
                </ReactMarkdown>
              </div>
            </Sensitive>
          </article>
        ) : (
          <EmptyState title="Select a document" compact />
        )}
      </div>
    </div>
  );
}

/* ---- safe markdown + inert links ----------------------------------------- */

const MD_ALLOWED = ["p", "br", "strong", "em", "del", "a", "ul", "ol", "li", "blockquote", "code", "pre", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td"];

const MD_COMPONENTS = {
  // URLs in content are inert until clicked, and never auto-fetched.
  a: ({ href, children }: { href?: string; children?: ReactNode }) =>
    href && /^https?:\/\//i.test(href) ? (
      <a href={href} target="_blank" rel="noreferrer noopener">
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  img: () => null,
} as const;

function InertLink({ url }: { url: string }): ReactNode {
  if (!/^https?:\/\//i.test(url)) return <span className={styles.url}>{clip(url, 60)}</span>;
  return (
    <a className={styles.url} href={url} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()}>
      {clip(url.replace(/^https?:\/\//, ""), 60)}
    </a>
  );
}
