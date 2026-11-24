/*
 * Ask — the helper box. Free text goes to the Query helper (/query?ask=…); the
 * starter questions come straight from the lens engine's startersForChannel over
 * a diverse set of channels, and open Query pre-filled and run.
 */
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";
import { Panel, SourceDot, useSourceColor } from "@/ui";
import { startersForChannel, declaredProfile } from "@/lens";
import { channelMeta } from "@/features/channel/meta";
import type { ChannelIndex } from "./useChannelIndex";
import styles from "./Ask.module.css";

interface StarterItem {
  question: string;
  statement: string;
  plugin: string;
  channelIndex: number;
}

export function Ask({ index }: { index: ChannelIndex }): ReactNode {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  useSourceColor();

  const starters = useMemo<StarterItem[]>(() => {
    const out: StarterItem[] = [];
    const seen = new Set<string>();
    // One strong starter from the heaviest channel of each source, for diversity.
    for (const g of index.groups) {
      const info = g.channels.find((c) => c.row.rows > 0) ?? g.channels[0];
      if (!info) continue;
      try {
        const meta = channelMeta(info.row);
        const [first] = startersForChannel(meta, declaredProfile(meta), { limit: 1 });
        if (first && !seen.has(first.question)) {
          seen.add(first.question);
          out.push({
            question: first.question,
            statement: first.statement,
            plugin: info.plugin,
            channelIndex: info.channelIndex,
          });
        }
      } catch {
        /* skip a channel whose starter will not compile */
      }
      if (out.length >= 6) break;
    }
    return out;
  }, [index]);

  const submit = (e: FormEvent): void => {
    e.preventDefault();
    const q = text.trim();
    if (!q) return;
    navigate(`/query?ask=${encodeURIComponent(q)}`);
  };

  const runStatement = (sql: string): void => {
    navigate(`/query?sql=${encodeURIComponent(sql)}&run=1`);
  };

  return (
    <Panel title="Ask">
      <form className={styles.box} onSubmit={submit}>
        <Sparkles className={styles.spark} size={16} aria-hidden />
        <input
          className={styles.input}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ask anything about your archive…"
          aria-label="Ask a question about your archive"
          spellCheck={false}
        />
        <button className={styles.go} type="submit" aria-label="Ask" disabled={!text.trim()}>
          <ArrowRight size={16} />
        </button>
      </form>

      {starters.length > 0 && (
        <ul className={styles.starters}>
          {starters.map((s) => (
            <li key={s.question}>
              <button type="button" className={styles.starter} onClick={() => runStatement(s.statement)}>
                <SourceDot plugin={s.plugin} channelIndex={s.channelIndex} size={7} />
                <span>{s.question}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
