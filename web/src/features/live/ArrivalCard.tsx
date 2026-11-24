/*
 * One arrival, rendered in the form its channel's archetype reads best. The
 * variant comes from the lens engine (bubbles / transcript / document / feed /
 * cards) so nothing here names a channel. Text is always wrapped in <Sensitive>
 * and any URL stays inert; the card is a button that opens the record drawer.
 */
import { memo, type ReactNode } from "react";
import { Sensitive, SourceDot, useSourceColor } from "@/ui";
import type { ArrivalRow } from "@/api/types";
import { relative } from "@/store/scopes";
import type { ChannelInfo } from "@/features/loom/useChannelIndex";
import styles from "./ArrivalCard.module.css";

export interface ArrivalCardProps {
  row: ArrivalRow;
  info?: ChannelInfo | null;
  onOpen?: (id: string | number, channel: string) => void;
  /** Rows the consumers cannot see yet are drawn lighter. */
  dim?: boolean;
  now?: number;
}

export const ArrivalCard = memo(function ArrivalCard({ row, info, onOpen, dim, now }: ArrivalCardProps): ReactNode {
  const { cssFor } = useSourceColor();
  const plugin = info?.plugin ?? row.channel.split(".")[0] ?? "";
  const variant = info?.variant ?? "cards";
  const accent = cssFor(plugin, info?.channelIndex ?? 0);
  const text = (row.excerpt ?? "").trim();
  const title = text || describeEmpty(variant);

  return (
    <button
      type="button"
      data-testid="arrival"
      className={`${styles.card} ${styles[variant]} ${dim ? styles.dim : ""}`}
      style={{ ["--accent" as string]: accent }}
      onClick={() => onOpen?.(row.id, row.channel)}
      title={`Open ${row.channel}`}
    >
      <span className={styles.rail} aria-hidden />
      <span className={styles.body}>
        <span className={styles.head}>
          <SourceDot plugin={plugin} channelIndex={info?.channelIndex ?? 0} size={8} />
          <span className={styles.channel}>{row.channel}</span>
          {row.author ? <span className={styles.author}>{row.author}</span> : null}
          <span className={styles.spacer} />
          {row.version > 1 ? <span className={styles.edited}>edited</span> : null}
          <time className={styles.when} dateTime={row.happened_at}>
            {relative(row.happened_at, now)}
          </time>
        </span>
        <span className={styles.excerpt}>
          <Sensitive>{title}</Sensitive>
        </span>
      </span>
    </button>
  );
});

function describeEmpty(variant: string): string {
  switch (variant) {
    case "bubbles": return "(no text)";
    case "transcript": return "(silent turn)";
    case "feed": return "(untitled item)";
    case "document": return "(empty document)";
    default: return "(record)";
  }
}
