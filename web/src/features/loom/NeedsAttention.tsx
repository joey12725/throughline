/*
 * Needs attention — what the owner should look at, derived from shape:
 *   - plugins that are late / stale / failing / paused / never ran
 *   - channels quiet for much longer than their own rhythm (from the weekly
 *     counts the Loom already fetched — no extra query)
 *   - channels with reported coverage gaps
 *   - pending approvals, when the token can see them
 * Restricted tokens get no plugin health; the list degrades to what they can see.
 */
import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { request, useBootstrap } from "@/api";
import { Panel, EmptyState, Skeleton, StatusGlyph, Badge } from "@/ui";
import { pluginState, STATE, type Tone } from "@/ui/status";
import { relative } from "@/store/scopes";
import type { ChannelIndex } from "./useChannelIndex";
import { useLoomData } from "./loomData";
import styles from "./NeedsAttention.module.css";

interface Item {
  key: string;
  tone: Tone;
  glyph?: ReactNode;
  title: string;
  detail: string;
  to: string;
  rank: number;
}

const TONE_RANK: Record<Tone, number> = { bad: 0, warn: 1, neutral: 2, ok: 3 };

export function NeedsAttention({ index }: { index: ChannelIndex }): ReactNode {
  const { data: boot } = useBootstrap();
  const now = Date.now();
  const grid = useLoomData(index, now);

  // Approvals: owner-visible app route; empty / 403 degrades silently.
  const approvals = useQuery({
    queryKey: ["approvals-count"],
    queryFn: async () => {
      try {
        const rows = await request<unknown[]>("/approvals");
        return Array.isArray(rows) ? rows.length : 0;
      } catch {
        return 0;
      }
    },
    enabled: !!boot && boot.token.unrestricted,
    staleTime: 60_000,
  });

  const items = useMemo<Item[]>(() => {
    if (!boot) return [];
    const out: Item[] = [];

    // 1) Plugin health
    for (const p of boot.plugins) {
      const st = pluginState(p, now);
      if (st === "ok" || st === "one_shot") continue;
      const d = STATE[st];
      out.push({
        key: `plugin:${p.name}`,
        tone: d.tone,
        glyph: <StatusGlyph state={st} label={false} size={14} />,
        title: p.name,
        detail:
          p.last_error?.slice(0, 80) ??
          (p.last_success_at ? `${d.word} · last ran ${relative(p.last_success_at, now)}` : d.word),
        to: `/sources/${encodeURIComponent(p.name)}`,
        rank: TONE_RANK[d.tone],
      });
    }

    // 2) Quiet channels (rhythm from the weekly grid)
    const nowWeek = grid.weekIndex(now);
    for (const info of index.byName.values()) {
      const arr = grid.counts.get(info.name);
      if (!arr || nowWeek < 0) continue;
      const active: number[] = [];
      for (let i = 0; i < arr.length; i++) if ((arr[i] ?? 0) > 0) active.push(i);
      if (active.length < 6) continue; // too sparse to have a rhythm
      const last = active[active.length - 1] ?? 0;
      const gaps: number[] = [];
      for (let i = 1; i < active.length; i++) gaps.push((active[i] ?? 0) - (active[i - 1] ?? 0));
      gaps.sort((a, b) => a - b);
      const median = gaps[Math.floor(gaps.length / 2)] || 1;
      const since = nowWeek - last;
      if (since > Math.max(3 * median, 3)) {
        out.push({
          key: `quiet:${info.name}`,
          tone: "warn",
          title: info.name,
          detail: `quiet ${since} weeks · usually every ~${median} ${median === 1 ? "week" : "weeks"}`,
          to: `/c/${encodeURIComponent(info.name)}`,
          rank: 1.5,
        });
      }
    }

    // 3) Reported coverage gaps
    for (const info of index.byName.values()) {
      const cov = info.row.coverage;
      if (cov && cov.open_gaps > 0) {
        out.push({
          key: `gap:${info.name}`,
          tone: "warn",
          title: info.name,
          detail: `${cov.open_gaps} coverage ${cov.open_gaps === 1 ? "gap" : "gaps"}`,
          to: `/c/${encodeURIComponent(info.name)}`,
          rank: 1.8,
        });
      }
    }

    // 4) Pending approvals
    if (approvals.data && approvals.data > 0) {
      out.push({
        key: "approvals",
        tone: "warn",
        title: `${approvals.data} pending ${approvals.data === 1 ? "approval" : "approvals"}`,
        detail: "decisions waiting in the ledger",
        to: "/ledger",
        rank: 0.5,
      });
    }

    out.sort((a, b) => a.rank - b.rank || a.title.localeCompare(b.title));
    return out.slice(0, 9);
  }, [boot, index, grid, approvals.data, now]);

  const restricted = boot && !boot.token.unrestricted;

  return (
    <Panel title="Needs attention" actions={<Link className={styles.allLink} to="/sources">Sources →</Link>}>
      {!boot || grid.loading ? (
        <Skeleton height={140} lines={5} />
      ) : items.length === 0 ? (
        <EmptyState
          title="All healthy"
          description={restricted ? "Nothing in view needs attention." : "Every source is current and covered."}
        />
      ) : (
        <ul className={styles.list}>
          {items.map((it) => (
            <li key={it.key}>
              <Link className={styles.row} to={it.to} data-tone={it.tone}>
                <span className={styles.glyph}>{it.glyph ?? <Badge tone={it.tone}>·</Badge>}</span>
                <span className={styles.text}>
                  <span className={styles.title}>{it.title}</span>
                  <span className={styles.detail}>{it.detail}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
