/*
 * The Graph side panel: a focused node's properties, its typed links, and the
 * records attached to it (each opens the record drawer; a page's body is read
 * there, rendered safely). Degrades to plain states when a node has no links or
 * records.
 */
import { Link2, FileText } from "lucide-react";
import { num, useSql } from "@/api";
import { EmptyState, ErrorState, KeyValue, Panel, Sensitive, Skeleton, SourceDot, StaticChip } from "@/ui";
import { formatInTz } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import { useOpenRecord } from "@/features/record";
import type { GNode } from "./layout";
import { entityDetailSql, nodeLinksSql, nodeRecordsSql, personDetailSql } from "./queries";
import styles from "./graph.module.css";

interface Props {
  node: GNode;
  kindColor: (kind: string) => string;
  primaryLabel: string;
  primaryDisabled: boolean;
  onPrimary: () => void;
}

interface Detail {
  id: number;
  kind?: string;
  name?: string;
  display_name?: string;
  handle?: string;
  status?: string;
  note?: string | null;
  external_ref?: string | null;
  metadata?: Record<string, unknown> | null;
}
interface RecRow {
  id: number;
  channel: string;
  is_reference: boolean;
  happened_at: string;
  content: string | null;
  link: string;
}
interface LinkRow {
  type: string;
  n: number | string;
}

export default function NodePanel({ node, kindColor, primaryLabel, primaryDisabled, onPrimary }: Props) {
  const tz = usePrefs((s) => s.tz);
  const isPerson = node.nodeKind === "person";
  const detail = useSql<Detail>(isPerson ? personDetailSql(node.id) : entityDetailSql(node.id), { priority: "visible", keyParts: ["gdetail", node.key] });
  const links = useSql<LinkRow>(nodeLinksSql(node.nodeKind, node.id), { priority: "visible", keyParts: ["glinks", node.key] });
  const records = useSql<RecRow>(nodeRecordsSql(node.nodeKind, node.id), { priority: "background", keyParts: ["grecs", node.key] });
  const open = useOpenRecord();
  const d = detail.data?.rows[0];
  const { primary, other } = d ? splitProps(d, isPerson) : { primary: [], other: [] };

  return (
    <Panel
      title={
        <span className={styles.nodeTitle}>
          <span className={styles.nodeSwatch} style={{ background: kindColor(node.kind) }} aria-hidden /> {node.label || node.kind}
        </span>
      }
      subtitle={<span className={styles.nodeType}>{node.kind}</span>}
      className={styles.sidePanel}
      actions={
        <button type="button" className={styles.expandBtn} disabled={primaryDisabled} onClick={onPrimary}>
          {primaryLabel}
        </button>
      }
    >
      {detail.isLoading && <Skeleton height={80} />}
      {d && (
        <div className={styles.props}>
          <KeyValue rows={primary} />
          {other.length > 0 && (
            <details className={styles.otherProps}>
              <summary>Other properties <span className={styles.chipN}>{other.length}</span></summary>
              <div className={styles.otherBody}>
                <KeyValue rows={other} />
              </div>
            </details>
          )}
        </div>
      )}

      <h3 className={styles.sectionH}><Link2 size={12} /> Links</h3>
      {links.isLoading && <Skeleton height={60} />}
      {links.error && <ErrorState error={links.error} />}
      {!links.isLoading && (links.data?.rows.length ?? 0) === 0 && <p className={styles.muted}>No typed links.</p>}
      <div className={styles.linkChips}>
        {(links.data?.rows ?? []).map((l) => (
          <StaticChip key={l.type}>{l.type} <span className={styles.chipN}>{num(l.n)}</span></StaticChip>
        ))}
      </div>

      <h3 className={styles.sectionH}><FileText size={12} /> Records</h3>
      {records.isLoading && <Skeleton height={100} />}
      {records.error && <ErrorState error={records.error} />}
      {!records.isLoading && (records.data?.rows.length ?? 0) === 0 && (
        <EmptyState icon={<FileText />} title="No records" description="Nothing links to this node yet." compact />
      )}
      <ul className={styles.recList}>
        {(records.data?.rows ?? []).map((r) => (
          <li key={r.id}>
            <button type="button" className={styles.recRow} onClick={() => open(r.id, r.channel)}>
              <span className={styles.recMeta}>
                <SourceDot plugin={pluginOf(r.channel)} /> {r.channel} · {r.link} · {formatInTz(r.happened_at, tz, { dateStyle: "medium" })}
              </span>
              <Sensitive as="span" className={styles.recContent}>{r.content ?? "—"}</Sensitive>
            </button>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

type Row = { key: React.ReactNode; value: React.ReactNode };

/**
 * Split a node's properties into the meaningful ones (shown) and the rest
 * ("Other properties", collapsed). We never silently drop a field: booleans and
 * internal-looking flags, null/empty values, and overflow past the first few all
 * move into the collapsed group rather than being hidden. This keeps noise like
 * an internal `zoo: true` flag out of the way while staying honest.
 */
function splitProps(d: Detail, isPerson: boolean): { primary: Row[]; other: Row[] } {
  const primary: Row[] = [];
  const other: Row[] = [];
  if (isPerson) {
    if (d.handle) primary.push({ key: "handle", value: d.handle });
    if (d.status) primary.push({ key: "status", value: d.status });
    if (d.note) primary.push({ key: "note", value: <Sensitive as="span">{d.note}</Sensitive> });
  } else {
    if (d.external_ref) other.push({ key: "ref", value: d.external_ref });
    const meta = d.metadata ?? {};
    for (const [k, v] of Object.entries(meta)) {
      if (v == null || typeof v === "object") {
        if (v != null) other.push({ key: k, value: previewJson(v) });
        continue;
      }
      // Booleans read as internal flags here; keep them, but out of the way.
      if (typeof v === "boolean") other.push({ key: k, value: v ? "true" : "false" });
      else if (primary.length < 6) primary.push({ key: k, value: String(v) });
      else other.push({ key: k, value: String(v) });
    }
  }
  if (primary.length === 0 && other.length === 0) primary.push({ key: "id", value: String(d.id) });
  return { primary, other };
}

function previewJson(v: unknown): string {
  try {
    const s = JSON.stringify(v);
    return s.length > 80 ? s.slice(0, 79) + "…" : s;
  } catch {
    return String(v);
  }
}

/** Plugin name = the text before the first dot of a channel (shape, not a list). */
function pluginOf(channel: string): string {
  const i = channel.indexOf(".");
  return i > 0 ? channel.slice(0, i) : channel;
}
