/*
 * The record drawer: one record rendered by its channel's archetype, with its
 * fields and roles, thread context, links and entities, provenance, versions,
 * effective annotations, classification, a coverage note, the raw payload tree,
 * and "open in Query". Read-only; all record text is text, never HTML, wrapped in
 * Sensitive, and any URL is inert until clicked.
 */
import { useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronRight, ExternalLink, SquareArrowOutUpRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { useBootstrap } from "@/api";
import type { ChannelRow } from "@/api/types";
import { Badge, Button, EmptyState, KeyValue, Sensitive, Skeleton, SourceDot, StaticChip } from "@/ui";
import { usePrefs } from "@/store/prefs";
import { formatInTz } from "@/store/scopes";
import { analyzeChannel, type RoleMap } from "@/lens";
import { channelMeta } from "@/features/channel/meta";
import { recordId, useRecord, useThreadContext, useProvenance, useLinks, useAnnotations, useVersions, type AnyRow } from "./recordData";
import styles from "./RecordDrawer.module.css";

export interface RecordDrawerProps {
  params: Record<string, string>;
  close: () => void;
}

export function RecordDrawer({ params }: RecordDrawerProps): ReactNode {
  const id = recordId(params.id ?? "");
  const channelName = params.channel ?? "";
  const navigate = useNavigate();
  const tz = usePrefs((s) => s.tz);
  const { data: boot } = useBootstrap();
  const channel = boot?.channels.find((c) => c.name === channelName);

  const { roles } = useMemo(() => {
    if (!channel) return { roles: {} as RoleMap };
    const a = analyzeChannel(channelMeta(channel), null);
    return { roles: a.roles };
  }, [channel]);

  const recQ = useRecord(id, channel);
  const rec = recQ.data?.rows[0];
  const ctxQ = useThreadContext(id);
  const { sources, supports } = useProvenance(id);
  const linksQ = useLinks(id);
  const annoQ = useAnnotations(id);
  const versionsQ = useVersions((rec?.external_id as string) ?? null, (rec?.channel_id as number) ?? null);

  if (id === null) {
    return <EmptyState title="No record" description="This drawer needs a record id." />;
  }

  const str = (v: unknown): string | null => (v === null || v === undefined || v === "" ? null : String(v));
  const time = str(rec?.happened_at);
  const ended = str(rec?.ended_at);
  const author = str(rec?.author_handle) ?? str(rec?.display_name) ?? str(rec?.person_handle);
  const content = str(rec?.content);

  // Title / url by role (declared field that carries each).
  const titleField = Object.values(roles).find((r) => r.role === "title")?.field;
  const urlField = Object.values(roles).find((r) => r.role === "url")?.field;
  const title = titleField ? str(rec?.[titleField]) : null;
  const url = urlField ? str(rec?.[urlField]) : null;

  const openInQuery = (): void => {
    const view = channel?.view ?? "api.messages";
    const q = `SELECT * FROM ${view} WHERE id = ${id}`;
    navigate(`/query?q=${encodeURIComponent(q)}`);
  };

  return (
    <div className={styles.drawer}>
      <div className={styles.topline}>
        {channel ? <SourceDot plugin={channel.plugin} /> : null}
        <span className={styles.channel}>{channelName || "record"}</span>
        <span className={styles.recId}>#{id}</span>
        <Button size="sm" variant="ghost" onClick={openInQuery}>
          <SquareArrowOutUpRight size={13} /> Query
        </Button>
      </div>

      {recQ.isLoading ? (
        <Skeleton height={60} lines={4} />
      ) : !rec ? (
        <EmptyState title="Record not found" description="It may be hidden from your token, superseded, or purged." />
      ) : (
        <>
          <section className={styles.content}>
            <div className={styles.contentHead}>
              {author ? (
                <Sensitive>
                  <span className={styles.author}>{author}</span>
                </Sensitive>
              ) : null}
              {time ? <span className={styles.time}>{formatInTz(time, tz)}</span> : null}
              {ended ? <span className={styles.timeEnd}>→ {formatInTz(ended, tz, { timeStyle: "short" })}</span> : null}
            </div>
            {title ? (
              <Sensitive block>
                <h3 className={styles.title}>{title}</h3>
              </Sensitive>
            ) : null}
            {content ? (
              <Sensitive block>
                <div className={styles.body}>
                  <ReactMarkdown allowedElements={MD_ALLOWED} unwrapDisallowed components={MD_COMPONENTS}>
                    {content}
                  </ReactMarkdown>
                </div>
              </Sensitive>
            ) : (
              <p className={styles.muted}>No text content.</p>
            )}
            {url ? <InertLink url={url} /> : null}
          </section>

          <Section title="Fields">
            <FieldList rec={rec} channel={channel} roles={roles} tz={tz} />
          </Section>

          <Section title="Classification">
            <KeyValue
              rows={[
                { key: "record kind", value: str(rec.is_reference) === "true" ? "reference" : channel?.record_kind ?? "record" },
                { key: "access", value: str(rec.access_label) ?? "default" },
                { key: "version", value: str(rec.version) ?? "1" },
                ...(channel?.coverage ? [{ key: "coverage", value: <Badge tone={channel.coverage.status === "complete" ? "ok" : channel.coverage.status === "partial" ? "warn" : "neutral"}>{String(channel.coverage.status).replace(/_/g, " ")}</Badge> }] : []),
              ]}
            />
          </Section>

          <CollapsibleSection title="Thread context" count={ctxQ.data?.rows.length}>
            <ThreadContext rows={ctxQ.data?.rows ?? []} currentId={id} tz={tz} loading={ctxQ.isLoading} />
          </CollapsibleSection>

          <Section title="Links & entities" count={linksQ.data?.rows.length}>
            <LinkList rows={linksQ.data?.rows ?? []} loading={linksQ.isLoading} />
          </Section>

          <Section title="Provenance" count={(sources.data?.rows.length ?? 0) + (supports.data?.rows.length ?? 0)}>
            <Provenance sources={sources.data?.rows ?? []} supports={supports.data?.rows ?? []} loading={sources.isLoading || supports.isLoading} />
          </Section>

          {(annoQ.data?.rows.length ?? 0) > 0 ? (
            <Section title="Annotations" count={annoQ.data?.rows.length}>
              <AnnotationList rows={annoQ.data?.rows ?? []} />
            </Section>
          ) : null}

          <Section title="Versions" count={versionsQ.data?.rows.length}>
            <VersionList rows={versionsQ.data?.rows ?? []} currentId={id} tz={tz} />
          </Section>

          <CollapsibleSection title="Raw payload">
            <JsonTree value={rec.fields} />
          </CollapsibleSection>
        </>
      )}
    </div>
  );
}

/* ---- sections ------------------------------------------------------------- */

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }): ReactNode {
  return (
    <section className={styles.section}>
      <h4 className={styles.sectionTitle}>
        {title}
        {count !== undefined ? <span className={styles.sectionCount}>{count}</span> : null}
      </h4>
      {children}
    </section>
  );
}

function CollapsibleSection({ title, count, children }: { title: string; count?: number; children: ReactNode }): ReactNode {
  const [open, setOpen] = useState(false);
  return (
    <section className={styles.section}>
      <button type="button" className={styles.collapseHead} onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <ChevronRight size={14} className={`${styles.chevron} ${open ? styles.chevronOpen : ""}`} />
        {title}
        {count !== undefined ? <span className={styles.sectionCount}>{count}</span> : null}
      </button>
      {open ? children : null}
    </section>
  );
}

function FieldList({ rec, channel, roles, tz }: { rec: AnyRow; channel: ChannelRow | undefined; roles: RoleMap; tz: string }): ReactNode {
  const fields = channel?.fields ?? [];
  if (fields.length === 0) return <p className={styles.muted}>This channel declares no fields.</p>;
  const rows = fields
    .filter((f) => rec[f.name] !== null && rec[f.name] !== undefined && rec[f.name] !== "")
    .map((f) => {
      const role = roles[f.name];
      return {
        key: (
          <span className={styles.fieldKey}>
            {f.name}
            {role ? <span className={styles.fieldRole}>{role.role.replace(/\./g, "·")}</span> : null}
          </span>
        ),
        value: <FieldValue value={rec[f.name]} role={role?.role} tz={tz} />,
      };
    });
  if (rows.length === 0) return <p className={styles.muted}>No field values on this record.</p>;
  return <KeyValue rows={rows} />;
}

function FieldValue({ value, role, tz }: { value: unknown; role?: string; tz: string }): ReactNode {
  if (value === null || value === undefined) return <span className={styles.muted}>—</span>;
  if (typeof value === "object") return <JsonTree value={value} inline />;
  const s = String(value);
  if (role === "url" && /^https?:\/\//i.test(s)) return <InertLink url={s} />;
  if ((role === "time.start" || role === "time.end" || role === "time.deadline") && /^\d{4}-\d{2}-\d{2}/.test(s)) {
    return <span className="tnum">{formatInTz(s, tz)}</span>;
  }
  const sensitive = role === "body" || role === "title" || role === "actor.label" || role === "category.speaker" || role === "geo.label";
  const node = <span className={styles.fieldVal}>{s.length > 200 ? s.slice(0, 200) + "…" : s}</span>;
  return sensitive ? <Sensitive>{node}</Sensitive> : node;
}

function ThreadContext({ rows, currentId, tz, loading }: { rows: readonly AnyRow[]; currentId: number; tz: string; loading: boolean }): ReactNode {
  if (loading) return <Skeleton height={40} lines={3} />;
  if (rows.length === 0) return <p className={styles.muted}>No surrounding thread.</p>;
  return (
    <ol className={styles.context}>
      {rows.map((r, i) => {
        const isCur = Number(r.id) === currentId;
        const who = (r.author_handle as string) ?? (r.display_name as string) ?? (r.person_handle as string) ?? "";
        return (
          <li key={i} className={`${styles.ctxRow} ${isCur ? styles.ctxCurrent : ""}`}>
            <div className={styles.ctxMeta}>
              {who ? (
                <Sensitive>
                  <span className={styles.ctxWho}>{who}</span>
                </Sensitive>
              ) : null}
              <span className={styles.time}>{r.happened_at ? formatInTz(String(r.happened_at), tz, { timeStyle: "short" }) : ""}</span>
            </div>
            <Sensitive block>
              <span className={styles.ctxText}>{(r.content as string) ?? ""}</span>
            </Sensitive>
          </li>
        );
      })}
    </ol>
  );
}

function LinkList({ rows, loading }: { rows: readonly AnyRow[]; loading: boolean }): ReactNode {
  if (loading) return <Skeleton height={28} lines={2} />;
  if (rows.length === 0) return <p className={styles.muted}>No links.</p>;
  return (
    <ul className={styles.links}>
      {rows.map((r, i) => (
        <li key={i} className={styles.linkRow}>
          <StaticChip>{String(r.type).replace(/_/g, " ")}</StaticChip>
          <Sensitive>
            <span className={styles.linkTarget}>{(r.entity_name as string) ?? `${r.to_kind} #${r.to_id}`}</span>
          </Sensitive>
          {r.entity_kind ? <span className={styles.linkKind}>{String(r.entity_kind)}</span> : null}
          {r.confidence != null ? <span className={styles.conf}>{Math.round(Number(r.confidence) * 100)}%</span> : null}
        </li>
      ))}
    </ul>
  );
}

function Provenance({ sources, supports, loading }: { sources: readonly AnyRow[]; supports: readonly AnyRow[]; loading: boolean }): ReactNode {
  if (loading) return <Skeleton height={28} lines={2} />;
  if (sources.length === 0 && supports.length === 0) return <p className={styles.muted}>No recorded lineage.</p>;
  return (
    <div className={styles.prov}>
      {sources.length > 0 ? (
        <div>
          <div className={styles.provLabel}>Derived from</div>
          <ul className={styles.links}>
            {sources.map((r, i) => (
              <li key={i} className={styles.linkRow}>
                <StaticChip>{String(r.relation ?? "source")}</StaticChip>
                <span className={styles.linkTarget}>message #{String(r.source_message_id)}</span>
                {r.origin ? <span className={styles.linkKind}>{String(r.origin)}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {supports.length > 0 ? (
        <div>
          <div className={styles.provLabel}>Supports</div>
          <ul className={styles.links}>
            {supports.map((r, i) => (
              <li key={i} className={styles.linkRow}>
                <StaticChip>{String(r.relation ?? "derived")}</StaticChip>
                <span className={styles.linkTarget}>{String(r.derived_kind ?? "record")} #{String(r.derived_id)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function AnnotationList({ rows }: { rows: readonly AnyRow[] }): ReactNode {
  return (
    <ul className={styles.links}>
      {rows.map((r, i) => (
        <li key={i} className={styles.linkRow}>
          <StaticChip>{String(r.attribute)}</StaticChip>
          <span className={styles.linkTarget}>
            <JsonTree value={r.value} inline />
          </span>
          {r.confidence != null ? <span className={styles.conf}>{Math.round(Number(r.confidence) * 100)}%</span> : null}
          {r.origin ? <span className={styles.linkKind}>{String(r.origin)}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function VersionList({ rows, currentId, tz }: { rows: readonly AnyRow[]; currentId: number; tz: string }): ReactNode {
  if (rows.length === 0) return <p className={styles.muted}>Single version.</p>;
  return (
    <ul className={styles.versions}>
      {rows.map((r, i) => {
        const isCur = Number(r.id) === currentId;
        const superseded = r.superseded_by != null;
        return (
          <li key={i} className={styles.versionRow}>
            <span className={styles.vNum}>v{String(r.version)}</span>
            <span className={styles.time}>{r.ingested_at ? formatInTz(String(r.ingested_at), tz, { dateStyle: "medium" }) : ""}</span>
            {isCur ? <Badge tone="ok">current</Badge> : superseded ? <Badge tone="neutral">superseded</Badge> : null}
          </li>
        );
      })}
    </ul>
  );
}

/* ---- JSON tree (inert, text only) ----------------------------------------- */

function JsonTree({ value, inline }: { value: unknown; inline?: boolean }): ReactNode {
  const parsed = useMemo(() => parse(value), [value]);
  if (parsed === undefined || parsed === null) return <span className={styles.muted}>—</span>;
  if (typeof parsed !== "object") {
    return <span className={styles.jsonScalar}>{String(parsed)}</span>;
  }
  return (
    <div className={inline ? styles.jsonInline : styles.jsonTree}>
      <JsonNode value={parsed} depth={0} />
    </div>
  );
}

function JsonNode({ value, depth }: { value: unknown; depth: number }): ReactNode {
  if (value === null || value === undefined) return <span className={styles.jsonNull}>null</span>;
  if (typeof value !== "object") {
    return (
      <Sensitive>
        <span className={styles.jsonScalar}>{typeof value === "string" ? value : String(value)}</span>
      </Sensitive>
    );
  }
  const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v] as const) : Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return <span className={styles.jsonNull}>{Array.isArray(value) ? "[]" : "{}"}</span>;
  return (
    <ul className={styles.jsonList}>
      {entries.slice(0, 200).map(([k, v]) => (
        <li key={k} className={styles.jsonRow} style={{ paddingLeft: depth ? 12 : 0 }}>
          <span className={styles.jsonKey}>{k}</span>
          {depth < 4 ? <JsonNode value={v} depth={depth + 1} /> : <span className={styles.muted}>…</span>}
        </li>
      ))}
    </ul>
  );
}

function parse(raw: unknown): unknown {
  if (typeof raw === "string") {
    const s = raw.trim();
    if (s.startsWith("{") || s.startsWith("[")) {
      try {
        return JSON.parse(s);
      } catch {
        return raw;
      }
    }
    return raw;
  }
  return raw;
}

/* ---- safe markdown + inert links ----------------------------------------- */

const MD_ALLOWED = ["p", "br", "strong", "em", "del", "a", "ul", "ol", "li", "blockquote", "code", "pre", "h1", "h2", "h3", "h4", "hr"];
const MD_COMPONENTS = {
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
  if (!/^https?:\/\//i.test(url)) return <span className={styles.url}>{url}</span>;
  return (
    <a className={styles.url} href={url} target="_blank" rel="noreferrer noopener">
      <ExternalLink size={12} />
      {url.replace(/^https?:\/\//, "").slice(0, 70)}
    </a>
  );
}
