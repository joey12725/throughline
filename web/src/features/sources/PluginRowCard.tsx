/*
 * One plugin on the health board: a dense row that expands to its channels.
 * Status glyph, age against cadence, an arrivals sparkline on the source's own
 * band, and (expandable, monospace) its last error. Nothing names a plugin.
 */
import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, AlertTriangle } from "lucide-react";
import type { ChannelRow, PluginRow } from "@/api/types";
import { StatusGlyph, SourceDot, useSourceColor } from "@/ui";
import { relative } from "@/store/scopes";
import { ChannelCard } from "@/features/channel";
import { deriveSourceState, formatCadence } from "./status";
import { Sparkline } from "./Sparkline";
import styles from "./Sources.module.css";

export interface PluginRowCardProps {
  plugin: PluginRow;
  channels: ChannelRow[];
  channelIndex: Map<string, number>;
  history: number[];
  now: number;
  defaultOpen?: boolean;
}

export function PluginRowCard({
  plugin,
  channels,
  channelIndex,
  history,
  now,
  defaultOpen = false,
}: PluginRowCardProps): ReactNode {
  const [open, setOpen] = useState(defaultOpen);
  const [showError, setShowError] = useState(false);
  const { cssFor } = useSourceColor();
  const health = deriveSourceState(plugin, now);
  const panelId = useId();

  const records = channels.reduce((s, c) => s + (c.rows || 0), 0);

  return (
    <div className={styles.pluginCard} data-tone={stateTone(health.state)} style={{ ["--src" as string]: cssFor(plugin.name, 0) }}>
      <div className={styles.pluginHead}>
        <button
          className={styles.expandBtn}
          data-testid="plugin-expand"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((v) => !v)}
        >
          <ChevronRight className={styles.chevron} data-open={open} size={16} aria-hidden />
          <SourceDot plugin={plugin.name} channelIndex={0} size={12} />
          <span className={styles.pluginName}>{plugin.name}</span>
        </button>

        <StatusGlyph state={health.state} size={14} />

        <span className={styles.pluginMeta}>
          {plugin.last_success_at ? (
            <>
              <span title={plugin.last_success_at}>{relative(plugin.last_success_at, now)}</span>
              <span className={styles.dot}>·</span>
              <span>{formatCadence(plugin.interval_s)}</span>
            </>
          ) : (
            <span>{formatCadence(plugin.interval_s)}</span>
          )}
        </span>

        <span className={styles.pluginCounts}>
          <Link to={`/sources/${encodeURIComponent(plugin.name)}`} className={styles.detailLink}>
            {channels.length} channel{channels.length === 1 ? "" : "s"}
          </Link>
          <span className={styles.dot}>·</span>
          <span>{records.toLocaleString()} records</span>
        </span>

        <span className={styles.pluginSpark}>
          <Sparkline counts={history} color="var(--src, var(--brass))" ariaLabel={`${plugin.name} arrivals`} />
        </span>
      </div>

      {plugin.last_error ? (
        <div className={styles.errorRow}>
          <button className={styles.errorToggle} onClick={() => setShowError((v) => !v)}>
            <AlertTriangle size={13} aria-hidden /> {showError ? "hide error" : "last error"}
          </button>
          {showError ? <pre className={styles.errorPre}>{plugin.last_error}</pre> : null}
        </div>
      ) : null}

      {open ? (
        <div className={styles.channelGrid} id={panelId}>
          {channels.length === 0 ? (
            <p className={styles.noChannels}>No channels registered for this source yet.</p>
          ) : (
            channels.map((c) => (
              <ChannelCard key={c.name} meta={c} channelIndex={channelIndex.get(c.name) ?? 0} plugin={plugin} />
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

function stateTone(state: string): string {
  if (state === "failing" || state === "never_ran") return "bad";
  if (state === "stale" || state === "late") return "warn";
  if (state === "ok" || state === "one_shot") return "ok";
  return "neutral";
}
