/*
 * The day as a story: records in time order, grouped into episodes by gaps. Each
 * record is rendered compactly by what it is — who, when, and its text under the
 * privacy veil — and opens in the drawer. Long-text channels read in Newsreader.
 */
import { useMemo, type ReactNode } from "react";
import { Panel, EmptyState, Sensitive, SourceDot } from "@/ui";
import { groupEpisodes, type DayRecord } from "./episodes";
import styles from "./Day.module.css";

export function Narrative({
  records,
  ownerPersonId,
  longText,
  pluginOf,
  chIndex,
  onOpen,
}: {
  records: DayRecord[];
  ownerPersonId: number | null;
  /** Channels whose body reads as long text (document/transcript/feed). */
  longText: Set<string>;
  pluginOf: Map<string, string>;
  chIndex: Map<string, number>;
  onOpen: (id: number, channel: string) => void;
}): ReactNode {
  const episodes = useMemo(() => groupEpisodes(records, 40), [records]);

  return (
    <Panel title="The day, in order" subtitle={`${episodes.length} episodes`}>
      {episodes.length === 0 ? (
        <EmptyState compact title="A quiet day" description="No records landed in this day." />
      ) : (
        <div>
          {episodes.map((ep) => (
            <div key={ep.key} className={styles.episode}>
              <div className={styles.epHead}>
                <span className={styles.epTime}>{fmtTime(ep.records[0]?.localTime)}</span>
                <span className={styles.epSpan}>
                  {ep.records.length} record{ep.records.length === 1 ? "" : "s"}
                  {ep.endMs > ep.startMs ? ` · ${fmtSpan(ep.startMs, ep.endMs)}` : ""}
                </span>
              </div>
              <div className={styles.epRecords}>
                {ep.records.map((r, k) => {
                  const plugin = pluginOf.get(r.channel) ?? r.channel;
                  const who = r.displayName || (r.personId === ownerPersonId && r.personId != null ? "You" : channelShort(r.channel));
                  const read = longText.has(r.channel);
                  return (
                    <button
                      key={`${r.id}-${k}`}
                      type="button"
                      className={styles.rec}
                      onClick={() => onOpen(r.id, r.channel)}
                    >
                      <SourceDot plugin={plugin} channelIndex={chIndex.get(r.channel) ?? 0} size={9} className={styles.recDot} />
                      <div className={styles.recMain}>
                        <div className={styles.recTop}>
                          <span className={styles.recWho}>
                            {who} <span className={styles.recChan}>{r.channel}</span>
                          </span>
                          <span className={styles.recTime}>{fmtTime(r.localTime)}</span>
                        </div>
                        <div className={read ? `${styles.recBody} ${styles.read}` : styles.recBody}>
                          <Sensitive>{r.content}</Sensitive>
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function fmtTime(local?: string): string {
  if (!local) return "";
  return local.slice(11, 16);
}
function fmtSpan(a: number, b: number): string {
  const mins = Math.round((b - a) / 60000);
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}
function channelShort(ch: string): string {
  const p = ch.split(".");
  return p[p.length - 1] ?? ch;
}
