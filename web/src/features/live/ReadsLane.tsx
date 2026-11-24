/*
 * The Reads lane (owner only) — what tokens are asking of the archive right now:
 * time, who asked, the statement (monospace, clipped, click to open in Query),
 * the row count and the status. Restricted tokens and agents receive no reads,
 * so this degrades to an empty state.
 */
import { type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { EmptyState } from "@/ui";
import type { ReadRow } from "@/api/types";
import { relative } from "@/store/scopes";
import styles from "./ReadsLane.module.css";

export function ReadsLane({ reads, now }: { reads: readonly ReadRow[]; now: number }): ReactNode {
  const navigate = useNavigate();
  if (reads.length === 0) {
    return <EmptyState compact title="No reads yet" description="Queries from any token show up here as they run." />;
  }
  return (
    <ul className={styles.list}>
      {reads.map((r, i) => {
        const ok = String(r.status).startsWith("2");
        return (
          <li key={`${r.at}:${i}`} className={styles.row}>
            <div className={styles.meta}>
              <span className={styles.asker}>{r.asker || String(r.token)}</span>
              <span className={styles.spacer} />
              <span className={`${styles.status} ${ok ? styles.ok : styles.bad}`}>{String(r.status)}</span>
              <time className={styles.at} dateTime={r.at}>{relative(r.at, now)}</time>
            </div>
            <button
              type="button"
              className={styles.stmt}
              title={r.statement}
              onClick={() => navigate(`/query?sql=${encodeURIComponent(r.statement)}`)}
            >
              {r.statement}
            </button>
            <div className={styles.foot}>
              {r.row_count === null ? "—" : `${r.row_count.toLocaleString("en-US")} ${r.row_count === 1 ? "row" : "rows"}`}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
