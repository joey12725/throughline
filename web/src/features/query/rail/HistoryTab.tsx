/*
 * History: the last runs on this device (idb-keyval), each with its time, row count
 * and elapsed ms. On-device only — these raw statements never go to the server.
 * Click to reload a statement into the editor.
 */
import { useEffect, useState, type ReactNode } from "react";
import { Clock, Trash2, CircleAlert } from "lucide-react";
import { Button, EmptyState, RelativeTime } from "@/ui";
import { loadHistory, clearHistory, type HistoryEntry } from "../history";
import styles from "../query.module.css";

export interface HistoryTabProps {
  onLoad: (statement: string) => void;
  /** bumps when a new run is recorded, to refresh the list */
  version: number;
}

export function HistoryTab({ onLoad, version }: HistoryTabProps): ReactNode {
  const [rows, setRows] = useState<HistoryEntry[]>([]);

  useEffect(() => {
    let live = true;
    void loadHistory().then((h) => live && setRows(h));
    return () => {
      live = false;
    };
  }, [version]);

  const clear = async (): Promise<void> => {
    await clearHistory();
    setRows([]);
  };

  return (
    <div className={styles.railPane}>
      <div className={styles.railSearch}>
        <Button size="sm" variant="ghost" icon={<Trash2 size={14} />} onClick={() => void clear()} disabled={rows.length === 0} block>
          Clear history
        </Button>
      </div>
      <div className={styles.railScroll}>
        {rows.length === 0 ? (
          <EmptyState icon={<Clock size={20} />} title="No runs yet" description="Statements you run appear here, newest first." compact />
        ) : (
          rows.map((h) => (
            <button key={h.id} type="button" className={styles.histItem} onClick={() => onLoad(h.statement)}>
              <code className={styles.histStmt}>{h.statement.replace(/\s+/g, " ").slice(0, 90)}</code>
              <div className={styles.histMeta}>
                <RelativeTime value={h.at} />
                {h.ok ? (
                  <>
                    <span className="tnum">{h.rowCount?.toLocaleString("en-US") ?? "—"} rows</span>
                    {h.ms !== null && <span className="tnum">{Math.round(h.ms)} ms</span>}
                  </>
                ) : (
                  <span className={styles.histErr}><CircleAlert size={11} /> error</span>
                )}
              </div>
            </button>
          ))
        )}
      </div>
    </div>
  );
}
