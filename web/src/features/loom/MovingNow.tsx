/*
 * Moving now — the latest arrivals as compact archetype cards. Reuses the live
 * arrivals feed (GET /v1/web/arrivals without `after`) and the shared
 * ArrivalCard; clicking a card opens the record drawer.
 */
import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useArrivals } from "@/api";
import { Panel, EmptyState, Skeleton } from "@/ui";
import { useOpenRecord } from "@/features/record";
import { ArrivalCard } from "@/features/live/ArrivalCard";
import type { ChannelIndex } from "./useChannelIndex";
import styles from "./MovingNow.module.css";

export function MovingNow({ index }: { index: ChannelIndex }): ReactNode {
  const { page, unsupported } = useArrivals({ enabled: true });
  const openRecord = useOpenRecord();
  const now = Date.now();

  const rows = useMemo(() => {
    const rs = page?.rows ?? [];
    // The feed is oldest-first; show the newest at the top.
    return [...rs].reverse().slice(0, 14);
  }, [page]);

  return (
    <Panel
      title="Moving now"
      actions={
        <Link className={styles.allLink} to="/live">
          Live →
        </Link>
      }
    >
      {unsupported ? (
        <EmptyState title="Arrivals need a current server" description="This build of the API has no arrivals route yet." />
      ) : !page ? (
        <Skeleton height={160} lines={6} />
      ) : rows.length === 0 ? (
        <EmptyState title="No recent arrivals" description="New records appear here as they land." />
      ) : (
        <div className={styles.list}>
          {rows.map((r) => (
            <ArrivalCard
              key={`${r.id}:${r.version}`}
              row={r}
              info={index.byName.get(r.channel)}
              onOpen={openRecord}
              now={now}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}
