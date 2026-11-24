/*
 * Role-aware cell rendering, shared by the table and the reader's field rows.
 * Personal text is always wrapped in Sensitive (the privacy veil); URLs found in
 * data are inert until clicked and carry rel="noreferrer noopener"; record text
 * is text, never HTML.
 */
import type { ReactNode } from "react";
import { ExternalLink } from "lucide-react";
import { RelativeTime, Sensitive } from "@/ui";
import type { RoleId, SqlColumn } from "@/lens";
import { clip, exactNumber, humanDuration, humanPercent } from "../runtime/format";
import { statusTone } from "../runtime/tokens";
import styles from "./reading.module.css";

const STATUS_GLYPH = { ok: "●", warn: "▲", bad: "■", neutral: "○" } as const;

export type CellKind = "time" | "duration" | "percent" | "number" | "url" | "json" | "bool" | "person" | "status" | "text" | "id";

const TIME_TYPES = new Set(["timestamptz", "timestamp", "date", "timestamp with time zone", "timestamp without time zone"]);
const NUM_TYPES = new Set(["int2", "int4", "int8", "bigint", "integer", "smallint", "numeric", "float4", "float8", "double precision", "real"]);

export function cellKind(col: SqlColumn, role: RoleId | undefined): CellKind {
  if (role === "url") return "url";
  if (role === "duration") return "duration";
  if (role === "percent" || role === "probability") return "percent";
  if (role === "category.status") return "status";
  if (role === "actor.label" || role === "category.speaker") return "person";
  if (role === "identifier" || role === "group_key" || role === "bookkeeping") return "id";
  if (role && (role.startsWith("time.") || role === "date.text")) return "time";
  if (role && (role.startsWith("json."))) return "json";
  const t = col.type.toLowerCase();
  if (TIME_TYPES.has(t)) return "time";
  if (t.startsWith("_") || t === "jsonb" || t === "json") return "json";
  if (t === "bool" || t === "boolean") return "bool";
  if (NUM_TYPES.has(t)) return "number";
  return "text";
}

export interface CellProps {
  value: unknown;
  kind: CellKind;
  unit?: string;
  /** Short cells for tables; long text gets room in readers. */
  compact?: boolean;
}

export function Cell({ value, kind, unit, compact = true }: CellProps): ReactNode {
  if (value === null || value === undefined || value === "") return <span className={styles.null}>—</span>;
  switch (kind) {
    case "time":
      return <RelativeTime value={String(value)} exact />;
    case "duration": {
      const s = typeof value === "number" ? value : Number(value);
      return <span className={styles.mono}>{Number.isFinite(s) ? humanDuration(unit === "ms" ? s / 1000 : s) : String(value)}</span>;
    }
    case "percent": {
      const n = typeof value === "number" ? value : Number(value);
      if (!Number.isFinite(n)) return <span>{String(value)}</span>;
      const frac = unit === "pct" || n > 1.0001 ? n / 100 : n;
      return (
        <span className={styles.percent}>
          <span className={styles.percentBar} aria-hidden>
            <span className={styles.percentFill} style={{ width: `${Math.max(0, Math.min(1, frac)) * 100}%` }} />
          </span>
          <span className={styles.mono}>{humanPercent(n, unit)}</span>
        </span>
      );
    }
    case "number": {
      const n = typeof value === "number" ? value : Number(value);
      return <span className={styles.mono}>{Number.isFinite(n) ? exactNumber(n) : String(value)}</span>;
    }
    case "id":
      return <span className={styles.id}>{String(value)}</span>;
    case "bool":
      return <span className={styles.mono}>{value === true || value === "t" || value === "true" ? "✓" : "·"}</span>;
    case "url": {
      const url = String(value);
      const safe = /^https?:\/\//i.test(url);
      return safe ? (
        <a className={styles.url} href={url} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()}>
          <ExternalLink size={12} aria-hidden /> {clip(url.replace(/^https?:\/\//, ""), compact ? 32 : 80)}
        </a>
      ) : (
        <Sensitive>{clip(url, compact ? 40 : 120)}</Sensitive>
      );
    }
    case "status": {
      const tone = statusTone(String(value));
      return (
        <span className={`${styles.status} ${styles[`status_${tone}`]}`}>
          <span aria-hidden>{STATUS_GLYPH[tone]}</span>
          {clip(String(value), 24)}
        </span>
      );
    }
    case "person":
      return (
        <Sensitive>
          <span className={styles.person}>{clip(String(value), compact ? 24 : 48)}</span>
        </Sensitive>
      );
    case "json": {
      const txt = typeof value === "string" ? value : JSON.stringify(value);
      return <span className={styles.jsonInline}>{clip(txt, compact ? 40 : 120)}</span>;
    }
    default: {
      const txt = String(value);
      return (
        <Sensitive>
          <span className={compact ? styles.textCompact : styles.text}>{compact ? clip(txt, 80) : txt}</span>
        </Sensitive>
      );
    }
  }
}
