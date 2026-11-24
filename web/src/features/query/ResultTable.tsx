/*
 * The Table tab: a virtualised grid with a sticky header. Cells render by inferred
 * role and Postgres type — times in the scope timezone, numbers right-aligned with
 * tabular figures, JSON collapsible, long text clipped behind the privacy veil, and
 * record ids as links that open the record drawer. Only the visible rows mount, so
 * a 10k-row result scrolls smoothly. A CSS grid (not a <table>) keeps header and
 * rows in column lock-step while rows are absolutely positioned for virtualisation.
 */
import { useMemo, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, ExternalLink } from "lucide-react";
import { Sensitive } from "@/ui";
import { usePrefs } from "@/store/prefs";
import { formatInTz } from "@/store/scopes";
import { inferRoles, profileResult, type RoleMap } from "@/lens";
import { useOpenRecord } from "@/features/record";
import type { Row, SqlColumn, SqlResult } from "@/api/types";
import styles from "./query.module.css";

const NUM_TYPES = /^(int2|int4|int8|float4|float8|numeric|money|smallint|integer|bigint|double)/i;
const TIME_TYPES = /^(timestamptz|timestamp|date|time|timetz)/i;
const JSON_TYPES = /^(jsonb|json|_)/i;
const BOOL_TYPES = /^(bool|boolean)/i;

function isUrl(v: string): boolean {
  return /^https?:\/\//i.test(v);
}

function numFmt(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v ?? "");
  return n.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

function JsonCell({ value }: { value: unknown }): ReactNode {
  const [open, setOpen] = useState(false);
  const text = JSON.stringify(value);
  const preview = text.length > 60 ? `${text.slice(0, 60)}…` : text;
  return (
    <button type="button" className={styles.jsonCell} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
      <ChevronRight size={12} className={open ? styles.jsonOpen : undefined} aria-hidden />
      {open ? <code className={styles.jsonFull}>{JSON.stringify(value, null, 2)}</code> : <code>{preview}</code>}
    </button>
  );
}

export interface ResultTableProps {
  result: SqlResult;
  /** The channel whose records this result's `id` belongs to, if unambiguous. */
  recordChannel?: string | null;
}

export function ResultTable({ result, recordChannel }: ResultTableProps): ReactNode {
  const tz = usePrefs((s) => s.tz);
  const openRecord = useOpenRecord();
  const parentRef = useRef<HTMLDivElement | null>(null);
  const rows = result.rows as readonly Row[];
  const columns = result.columns;

  const roles: RoleMap = useMemo(() => {
    if (rows.length === 0 || columns.length === 0) return {};
    try {
      return inferRoles(profileResult(rows.slice(0, 1500) as never, columns as never, result.truncated));
    } catch {
      return {};
    }
  }, [rows, columns, result.truncated]);

  const virt = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 34,
    overscan: 16,
  });

  const template = useMemo(
    () => `52px ${columns.map((c) => (NUM_TYPES.test(c.type) ? "minmax(90px, 0.5fr)" : "minmax(150px, 1fr)")).join(" ")}`,
    [columns],
  );

  if (columns.length === 0) {
    return <div className={styles.resultEmpty}>No columns.</div>;
  }

  const idIsRecord = Boolean(recordChannel) && columns.some((c) => c.name === "id" && NUM_TYPES.test(c.type));

  function renderCell(col: SqlColumn, value: unknown): ReactNode {
    if (value === null || value === undefined) return <span className={styles.nullCell}>null</span>;
    if (idIsRecord && col.name === "id") {
      return (
        <button type="button" className={styles.idLink} onClick={() => openRecord(String(value), recordChannel as string)} title="Open record">
          {String(value)}
        </button>
      );
    }
    if (TIME_TYPES.test(col.type)) return <span className="tnum">{formatInTz(String(value), tz)}</span>;
    if (NUM_TYPES.test(col.type)) return <span className={`tnum ${styles.numCell}`}>{numFmt(value)}</span>;
    if (BOOL_TYPES.test(col.type)) return <span className={styles.boolCell}>{value ? "yes" : "no"}</span>;
    if (JSON_TYPES.test(col.type) || typeof value === "object") return <JsonCell value={value} />;
    const text = String(value);
    if (isUrl(text)) {
      return (
        <a className={styles.urlCell} href={text} target="_blank" rel="noreferrer noopener" title={text}>
          {text} <ExternalLink size={11} aria-hidden />
        </a>
      );
    }
    const role = roles[col.name]?.role;
    const longText = role === "body" || role === "title" || text.length > 48;
    return (
      <Sensitive className={styles.textCell}>
        {longText && text.length > 240 ? `${text.slice(0, 240)}…` : text}
      </Sensitive>
    );
  }

  return (
    <div className={styles.gridScroll} ref={parentRef}>
      <div className={styles.grid} style={{ minWidth: "fit-content" }}>
        <div className={styles.gridHead} style={{ gridTemplateColumns: template }}>
          <div className={styles.rowNumHead} aria-hidden />
          {columns.map((c) => (
            <div key={c.name} className={`${styles.headCell} ${NUM_TYPES.test(c.type) ? styles.numHead : ""}`} title={`${c.name} · ${c.type}`}>
              <span className={styles.colName}>{c.name}</span>
              <span className={styles.colType}>{c.type}</span>
            </div>
          ))}
        </div>
        <div className={styles.gridBody} style={{ height: virt.getTotalSize() }}>
          {virt.getVirtualItems().map((vi) => {
            const row = rows[vi.index] as Row;
            return (
              <div
                key={vi.key}
                className={styles.gridRow}
                style={{ gridTemplateColumns: template, transform: `translateY(${vi.start}px)` }}
              >
                <div className={styles.rowNum}>{vi.index + 1}</div>
                {columns.map((c) => (
                  <div key={c.name} className={`${styles.cell} ${NUM_TYPES.test(c.type) ? styles.numTd : ""}`}>
                    {renderCell(c, (row as Record<string, unknown>)[c.name])}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
