/*
 * Saved queries: server state of kind "saved_query" ({title, statement, spec?,
 * created_at}) via the UI-state API, with a localStorage fallback when the server
 * has no web_state. Click to load a saved query back into the editor; the trash
 * removes it. "Save current query" stores whatever is in the editor now.
 */
import { type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, Save, Bookmark } from "lucide-react";
import { Button, EmptyState, RelativeTime } from "@/ui";
import { listState, deleteState } from "@/api";
import type { ViewSpec } from "@/lens";
import styles from "../query.module.css";

export interface SavedQuery {
  title: string;
  statement: string;
  spec?: ViewSpec | null;
  created_at: string;
}

export interface SavedTabProps {
  onLoad: (statement: string, spec?: ViewSpec | null) => void;
  onSaveCurrent: () => void;
  canSave: boolean;
}

export function SavedTab({ onLoad, onSaveCurrent, canSave }: SavedTabProps): ReactNode {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["saved_query"],
    queryFn: () => listState<SavedQuery>("saved_query"),
    staleTime: 10_000,
  });

  const entries = [...(q.data ?? [])].sort((a, b) => (b.value.created_at ?? "").localeCompare(a.value.created_at ?? ""));

  const remove = async (key: string): Promise<void> => {
    await deleteState("saved_query", key).catch(() => undefined);
    void qc.invalidateQueries({ queryKey: ["saved_query"] });
  };

  return (
    <div className={styles.railPane}>
      <div className={styles.railSearch}>
        <Button size="sm" variant="default" icon={<Save size={14} />} onClick={onSaveCurrent} disabled={!canSave} block>
          Save current query
        </Button>
      </div>
      <div className={styles.railScroll}>
        {q.isLoading ? null : entries.length === 0 ? (
          <EmptyState icon={<Bookmark size={20} />} title="Nothing saved yet" description="Save a query to find it here on any device signed in to this box." compact />
        ) : (
          entries.map((e) => (
            <div key={e.key} className={styles.savedItem}>
              <button type="button" className={styles.savedMain} onClick={() => onLoad(e.value.statement, e.value.spec)}>
                <span className={styles.savedTitle}>{e.value.title || "Untitled query"}</span>
                <code className={styles.savedStmt}>{e.value.statement.slice(0, 80)}</code>
                <span className={styles.savedWhen}><RelativeTime value={e.value.created_at} /></span>
              </button>
              <button type="button" className={styles.savedDelete} onClick={() => void remove(e.key)} aria-label={`Delete ${e.value.title}`}>
                <Trash2 size={13} />
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
