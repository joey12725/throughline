/*
 * The empty-state starters: real questions the lens engine derives from the richest
 * channels, as clickable cards. Clicking fills the statement and runs it with a chart
 * already chosen — the "easy to answer questions" path. Diverse by channel and lens,
 * so the grid shows the breadth of the archive rather than six variations of one idea.
 */
import { useMemo, type ReactNode } from "react";
import { ArrowRight } from "lucide-react";
import { SourceDot } from "@/ui";
import { startersForChannel, type Starter, type ViewSpec } from "@/lens";
import type { ChannelRow } from "@/api/types";
import { metaFromRow } from "./meta";
import styles from "./query.module.css";

export interface StartersProps {
  channels: readonly ChannelRow[];
  tz: string;
  onPick: (statement: string, spec: ViewSpec, channel: string, plugin: string) => void;
}

interface Card extends Starter {
  channel: string;
  plugin: string;
  channelIndex: number;
}

export function Starters({ channels, tz, onPick }: StartersProps): ReactNode {
  const cards = useMemo<Card[]>(() => {
    const ranked = [...channels]
      .map((c, i) => ({ c, i }))
      .filter((x) => Number(x.c.rows) > 0)
      .sort((a, b) => Number(b.c.rows) - Number(a.c.rows))
      .slice(0, 10);

    const out: Card[] = [];
    const seenLens = new Map<string, number>();
    for (const { c, i } of ranked) {
      let starters: Starter[] = [];
      try {
        starters = startersForChannel(metaFromRow(c), null, { scope: { tz }, limit: 3 });
      } catch {
        starters = [];
      }
      for (const s of starters) {
        const lens = s.spec.lens;
        // Keep the grid diverse: one card per lens kind.
        const n = seenLens.get(lens) ?? 0;
        if (n >= 1) continue;
        seenLens.set(lens, n + 1);
        out.push({ ...s, channel: c.name, plugin: c.plugin, channelIndex: i });
        break; // one strong starter per channel
      }
      if (out.length >= 8) break;
    }
    return out;
  }, [channels, tz]);

  if (cards.length === 0) return null;

  return (
    <div className={styles.starters}>
      <div className={styles.startersHead}>
        <h2>Answer a question</h2>
        <p>Pick one to run it, or write your own below. Every answer comes with a chart.</p>
      </div>
      <div className={styles.startersGrid}>
        {cards.map((card, i) => (
          <button
            key={`${card.channel}-${i}`}
            type="button"
            className={styles.starterCard}
            onClick={() => onPick(card.statement, card.spec, card.channel, card.plugin)}
          >
            <div className={styles.starterTop}>
              <SourceDot plugin={card.plugin} channelIndex={card.channelIndex} />
              <span className={styles.starterChannel}>{card.channel}</span>
              <ArrowRight size={14} className={styles.starterArrow} aria-hidden />
            </div>
            <div className={styles.starterQ}>{card.question}</div>
            <div className={styles.starterLens}>{card.spec.lens.replace(/_/g, " ")}</div>
          </button>
        ))}
      </div>
    </div>
  );
}
