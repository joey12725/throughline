/*
 * Recipes: the tested recipe library over HTTP (GET /v1/recipes for the list,
 * GET /v1/recipes/{name} for the Markdown body with adaptation notes). Each shows
 * its name, summary and what it needs; "Insert" drops its statement into the editor,
 * and expanding fetches the full notes. Recipe text is rendered as Markdown with no
 * raw HTML.
 */
import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronRight, CornerDownLeft, Search } from "lucide-react";
import { Input, Skeleton, StaticChip } from "@/ui";
import { request } from "@/api";
import styles from "../query.module.css";

interface RecipeRow {
  name: string;
  version?: number;
  status?: string;
  summary?: string;
  needs?: string[];
  statements?: string[];
}

interface RecipeBody {
  name: string;
  body?: string;
  statements?: string[];
}

export interface RecipesTabProps {
  onInsert: (text: string) => void;
}

function RecipeDetail({ name, onInsert }: { name: string; onInsert: (t: string) => void }): ReactNode {
  const q = useQuery({
    queryKey: ["recipe", name],
    queryFn: () => request<RecipeBody>(`/recipes/${encodeURIComponent(name)}`),
    staleTime: Infinity,
  });
  if (q.isLoading) return <Skeleton height={60} radius={6} />;
  if (q.isError || !q.data) return <p className={styles.fieldDesc}>Could not load this recipe.</p>;
  const body = q.data.body ?? "";
  return (
    <div className={styles.recipeBody}>
      {body ? (
        <div className={styles.markdown}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
        </div>
      ) : (
        <p className={styles.fieldDesc}>No adaptation notes.</p>
      )}
      {(q.data.statements ?? []).map((s, i) => (
        <div key={i} className={styles.recipeStmt}>
          <code>{s}</code>
          <button type="button" className={styles.recipeInsert} onClick={() => onInsert(s)} title="Insert this statement">
            <CornerDownLeft size={12} /> insert
          </button>
        </div>
      ))}
    </div>
  );
}

export function RecipesTab({ onInsert }: RecipesTabProps): ReactNode {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["recipes"],
    queryFn: () => request<RecipeRow[]>("/recipes"),
    staleTime: 5 * 60_000,
  });

  const needle = q.trim().toLowerCase();
  const rows = (list.data ?? []).filter(
    (r) => !needle || r.name.toLowerCase().includes(needle) || (r.summary ?? "").toLowerCase().includes(needle),
  );

  return (
    <div className={styles.railPane}>
      <div className={styles.railSearch}>
        <Input lead={<Search size={14} />} placeholder="Search recipes" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search recipes" />
      </div>
      <div className={styles.railScroll}>
        {list.isLoading ? (
          <div className={styles.railPadded}><Skeleton height={48} radius={6} /><Skeleton height={48} radius={6} /></div>
        ) : rows.length === 0 ? (
          <div className={styles.railEmpty}>No recipe matches.</div>
        ) : (
          rows.map((r) => {
            const expanded = open === r.name;
            const first = r.statements?.[0];
            return (
              <div key={r.name} className={styles.recipeItem}>
                <div className={styles.recipeHead}>
                  <button type="button" className={styles.channelToggle} onClick={() => setOpen(expanded ? null : r.name)} aria-expanded={expanded} aria-label={`Details of ${r.name}`}>
                    <ChevronRight size={13} className={expanded ? styles.rotOpen : undefined} aria-hidden />
                  </button>
                  <div className={styles.recipeMain}>
                    <div className={styles.recipeTitleRow}>
                      <span className={styles.recipeName}>{r.name}</span>
                      {first && (
                        <button type="button" className={styles.recipeInsert} onClick={() => onInsert(first)} title="Insert statement">
                          <CornerDownLeft size={12} /> insert
                        </button>
                      )}
                    </div>
                    {r.summary ? <p className={styles.recipeSummary}>{r.summary}</p> : null}
                    {(r.needs ?? []).length > 0 && (
                      <div className={styles.recipeNeeds}>
                        {(r.needs ?? []).slice(0, 4).map((n) => (
                          <StaticChip key={n}>{n.replace(/^function:/, "")}</StaticChip>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
                {expanded && <RecipeDetail name={r.name} onInsert={onInsert} />}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
