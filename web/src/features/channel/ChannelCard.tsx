/*
 * <ChannelCard meta /> — a compact, linkable card for one channel: source colour,
 * a sparkline of its record spine, counts, and coverage status. Exported for the
 * Sources index and the home screen. Derives everything from the bootstrap row;
 * no channel or plugin name is special-cased.
 */
import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { num, useSql } from "@/api";
import type { ChannelRow, PluginRow } from "@/api/types";
import { Badge, SourceDot, StatusGlyph, useSourceColor } from "@/ui";
import { pluginState, type Tone } from "@/ui/status";
import { relative } from "@/store/scopes";
import styles from "./ChannelCard.module.css";

const COVERAGE_TONE: Record<string, Tone> = { complete: "ok", partial: "warn", best_effort: "neutral" };

export interface ChannelCardProps {
  meta: ChannelRow;
  /** Index of this channel within its plugin, for the colour lightness step. */
  channelIndex?: number;
  /** The owning plugin's health row, when the host has it (adds a status glyph). */
  plugin?: PluginRow | null;
  /** Fetch and draw the spine sparkline (default true). */
  spark?: boolean;
  className?: string;
}

export function ChannelCard({ meta, channelIndex = 0, plugin, spark = true, className }: ChannelCardProps): ReactNode {
  const { cssFor } = useSourceColor();
  const hasRows = meta.rows > 0;
  // A coarse weekly spine — one small query, bounded, cached by react-query.
  const spine = useSql<{ bucket: string; n: string }>(
    spark && hasRows
      ? `SELECT date_trunc('week', happened_at) AS bucket, count(*)::text AS n FROM ${meta.view} GROUP BY 1 ORDER BY 1`
      : null,
    { rowCap: 600, priority: "background", keyParts: [meta.name] },
  );

  const counts = spine.data?.rows ?? [];
  const path = useMemo(() => sparkPath(counts.map((r) => num(r.n))), [counts]);

  const covTone = COVERAGE_TONE[meta.coverage?.status ?? ""] ?? "neutral";

  return (
    <Link
      to={`/c/${encodeURIComponent(meta.name)}`}
      className={`${styles.card} ${className ?? ""}`}
      style={{ ["--src" as string]: cssFor(meta.plugin, channelIndex) }}
    >
      <div className={styles.head}>
        <SourceDot plugin={meta.plugin} channelIndex={channelIndex} size={10} />
        <span className={styles.name}>{meta.name}</span>
        {plugin ? <StatusGlyph state={pluginState(plugin)} label={false} size={13} /> : null}
      </div>

      <div className={styles.sparkRow}>
        {spark && hasRows && path ? (
          <svg className={styles.spark} viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden>
            <path d={path} fill="none" stroke="var(--src, var(--brass))" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
          </svg>
        ) : (
          <div className={styles.sparkEmpty} aria-hidden />
        )}
      </div>

      <div className={styles.foot}>
        <span className={styles.count}>
          {hasRows ? meta.rows.toLocaleString("en-US") : "—"}
          <span className={styles.countUnit}>{meta.rows === 1 ? "record" : "records"}</span>
        </span>
        {meta.coverage ? (
          <Badge tone={covTone}>{String(meta.coverage.status).replace(/_/g, " ")}</Badge>
        ) : (
          <span className={styles.when}>{hasRows ? relative(meta.last_at) : "awaiting data"}</span>
        )}
      </div>
    </Link>
  );
}

/** A normalised sparkline path over a 100×24 box, baseline at the bottom. */
function sparkPath(values: number[]): string | null {
  if (values.length < 2) return null;
  const max = Math.max(1, ...values);
  const stepX = 100 / (values.length - 1);
  return values
    .map((v, i) => {
      const x = i * stepX;
      const y = 23 - (Math.log1p(v) / Math.log1p(max)) * 21;
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}
