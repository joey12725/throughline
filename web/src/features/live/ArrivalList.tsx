/*
 * The arrival list: newest at the top, each row an archetype card, virtualised.
 * Unsettled rows (consumers cannot see them yet) are lighter and sit above a
 * hairline labelled with the settle horizon. A burst of old-dated rows from one
 * channel folds into a single backfill card so a history import cannot flood the
 * view.
 */
import { useMemo, useRef, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { EmptyState } from "@/ui";
import type { ArrivalRow } from "@/api/types";
import { relative } from "@/store/scopes";
import { useOpenRecord } from "@/features/record";
import type { ChannelIndex } from "@/features/loom/useChannelIndex";
import { ArrivalCard } from "./ArrivalCard";
import styles from "./ArrivalList.module.css";

const BACKFILL_MIN_RUN = 6;
const BACKFILL_STALE_MS = 6 * 60 * 60 * 1000; // happened_at older than ingested_at by >6h

type Item =
  | { kind: "arrival"; key: string; row: ArrivalRow; settled: boolean }
  | { kind: "backfill"; key: string; channel: string; count: number; fromMs: number; toMs: number; settled: boolean }
  | { kind: "settle"; key: string; horizon: number | null; settleS: number | null };

export function ArrivalList({
  rows,
  index,
  activePlugins,
  settleHorizon,
  settleS,
  now,
}: {
  rows: ArrivalRow[];
  index: ChannelIndex;
  activePlugins: ReadonlySet<string>;
  settleHorizon: number | null;
  settleS: number | null;
  now: number;
}): ReactNode {
  const openRecord = useOpenRecord();
  const parentRef = useRef<HTMLDivElement>(null);

  const items = useMemo<Item[]>(() => {
    const filtered =
      activePlugins.size === 0
        ? rows
        : rows.filter((r) => activePlugins.has(index.byName.get(r.channel)?.plugin ?? r.channel.split(".")[0] ?? ""));

    // Fold contiguous backfill runs (same channel, old happened_at, long run).
    const folded: Item[] = [];
    let i = 0;
    while (i < filtered.length) {
      const r = filtered[i]!;
      const stale = Date.parse(r.ingested_at) - Date.parse(r.happened_at) > BACKFILL_STALE_MS;
      if (stale) {
        let j = i;
        let fromMs = Infinity;
        let toMs = -Infinity;
        while (
          j < filtered.length &&
          filtered[j]!.channel === r.channel &&
          Date.parse(filtered[j]!.ingested_at) - Date.parse(filtered[j]!.happened_at) > BACKFILL_STALE_MS
        ) {
          const hm = Date.parse(filtered[j]!.happened_at);
          if (hm < fromMs) fromMs = hm;
          if (hm > toMs) toMs = hm;
          j++;
        }
        const run = j - i;
        if (run >= BACKFILL_MIN_RUN) {
          folded.push({
            kind: "backfill",
            key: `bf:${r.channel}:${r.id}:${run}`,
            channel: r.channel,
            count: run,
            fromMs,
            toMs,
            settled: r.settled,
          });
          i = j;
          continue;
        }
      }
      folded.push({ kind: "arrival", key: `${r.id}:${r.version}`, row: r, settled: r.settled });
      i++;
    }

    // Insert the settle divider before the first settled item (unsettled sit above).
    const firstSettled = folded.findIndex((it) => it.kind !== "settle" && it.settled);
    if (firstSettled > 0) {
      folded.splice(firstSettled, 0, { kind: "settle", key: "settle", horizon: settleHorizon, settleS });
    }
    return folded;
  }, [rows, index, activePlugins, settleHorizon, settleS]);

  const virt = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: (i) => (items[i]?.kind === "settle" ? 30 : 64),
    overscan: 8,
    getItemKey: (i) => items[i]?.key ?? i,
  });

  if (items.length === 0) {
    return <EmptyState title="Nothing moving yet" description="Arrivals appear here the moment they land." />;
  }

  return (
    <div ref={parentRef} className={styles.scroll}>
      <div className={styles.inner} style={{ height: virt.getTotalSize() }}>
        {virt.getVirtualItems().map((v) => {
          const it = items[v.index]!;
          return (
            <div
              key={v.key}
              ref={virt.measureElement}
              data-index={v.index}
              className={styles.slot}
              style={{ transform: `translateY(${v.start}px)` }}
            >
              {it.kind === "settle" ? (
                <SettleDivider horizon={it.horizon} settleS={it.settleS} now={now} />
              ) : it.kind === "backfill" ? (
                <BackfillCard item={it} index={index} />
              ) : (
                <ArrivalCard
                  row={it.row}
                  info={index.byName.get(it.row.channel)}
                  onOpen={openRecord}
                  dim={!it.settled}
                  now={now}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SettleDivider({ horizon, settleS, now }: { horizon: number | null; settleS: number | null; now: number }): ReactNode {
  const label =
    horizon !== null
      ? `settle horizon · ${relative(horizon, now)}${settleS ? ` (${settleS}s)` : ""}`
      : "settle horizon";
  return (
    <div className={styles.settle}>
      <span className={styles.settleLine} />
      <span className={styles.settleLabel}>{label}</span>
      <span className={styles.settleLine} />
    </div>
  );
}

function BackfillCard({
  item,
  index,
}: {
  item: Extract<Item, { kind: "backfill" }>;
  index: ChannelIndex;
}): ReactNode {
  const info = index.byName.get(item.channel);
  const plugin = info?.plugin ?? item.channel.split(".")[0] ?? "";
  const span = spanLabel(item.fromMs, item.toMs);
  return (
    <div className={styles.backfill} style={{ ["--c" as string]: `var(--brass)` }} data-plugin={plugin}>
      <span className={styles.bfIcon} aria-hidden>⇲</span>
      <span className={styles.bfText}>
        <strong>{item.count.toLocaleString("en-US")}</strong> records backfilled into{" "}
        <span className={styles.bfChannel}>{item.channel}</span>
        <span className={styles.bfSpan}>{span}</span>
      </span>
    </div>
  );
}

function spanLabel(fromMs: number, toMs: number): string {
  if (!Number.isFinite(fromMs) || !Number.isFinite(toMs)) return "";
  const f = new Date(fromMs);
  const t = new Date(toMs);
  const opt: Intl.DateTimeFormatOptions = { year: "numeric", month: "short", timeZone: "UTC" };
  const a = f.toLocaleDateString("en-US", opt);
  const b = t.toLocaleDateString("en-US", opt);
  return a === b ? ` · ${a}` : ` · ${a} – ${b}`;
}
