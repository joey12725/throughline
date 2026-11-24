import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Play, Save, ArrowUpRight, Loader2, Terminal, Search, CalendarRange, Database } from "lucide-react";
import { Skeleton, Button, CodeBlock, Badge, ErrorState, toast } from "@/ui";
import { request, putState } from "@/api";
import { useStarterQueries, setupProblem, type StarterQuery } from "@/api/setup";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

const KIND_ICON = { sql: Terminal, find: Search, timeline: CalendarRange } as const;

interface SqlResp { rows: Array<Record<string, unknown>>; row_count: number; truncated: boolean }
interface FindResp { episodes: Array<Record<string, unknown>> }
interface TimelineResp { total_episodes: number; episodes_by_year: Record<string, number>; episodes: Array<Record<string, unknown>> }

export function QueriesStep(): ReactNode {
  const starters = useStarterQueries();

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Ask your first questions"
        title="Everything you've gathered, one question away"
        lede="Your archive is now a database you can ask in plain SQL, by meaning, or across time. Here are a few to try — run them, tweak them, save the ones you like. Your agent can run any of these too."
      />

      <div className={styles.body}>
        {starters.isLoading ? (
          <div className={styles.loadingStack}><Skeleton height={120} /><Skeleton height={120} /></div>
        ) : starters.error ? (
          <ErrorState error={starters.error} title="Could not load starter questions" onRetry={() => void starters.refetch()} />
        ) : starters.data ? (
          <div className={styles.setupList}>
            {starters.data.queries.map((q) => (
              <QueryCard key={q.id} query={q} />
            ))}
          </div>
        ) : null}
      </div>

      <WizardFooter />
    </div>
  );
}

function QueryCard({ query }: { query: StarterQuery }): ReactNode {
  const Icon = KIND_ICON[query.kind] ?? Database;
  const initialSql = query.kind === "sql" ? String(query.request.statement ?? "") : "";
  const [sqlText, setSqlText] = useState(initialSql);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ReactNode | null>(null);
  const [problem, setProblem] = useState<ReturnType<typeof setupProblem> | null>(null);

  const run = async (): Promise<void> => {
    setRunning(true);
    setProblem(null);
    setResult(null);
    try {
      if (query.kind === "sql") {
        const r = await request<SqlResp>("/sql", { method: "POST", body: { ...query.request, statement: sqlText } });
        setResult(<SqlResult resp={r} />);
      } else if (query.kind === "find") {
        const r = await request<FindResp>("/find", { method: "POST", body: query.request });
        setResult(<FindResult resp={r} />);
      } else {
        const r = await request<TimelineResp>("/timeline", { method: "POST", body: query.request });
        setResult(<TimelineResult resp={r} />);
      }
    } catch (e) {
      setProblem(setupProblem(e));
    } finally {
      setRunning(false);
    }
  };

  const save = (): void => {
    void putState("saved_query", `q_${Date.now()}`, { title: query.title, statement: sqlText, created_at: new Date().toISOString() })
      .then(() => toast.ok("Saved to Query"))
      .catch(() => toast.error("Could not save"));
  };

  return (
    <div className={styles.setupCard} style={{ padding: "var(--s-5)" }}>
      <div className={styles.jobRowTop}>
        <span className={styles.setupCardIcon} style={{ width: 32, height: 32 }}><Icon size={16} /></span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className={styles.setupCardName}>{query.title}</div>
          <div className={styles.setupCardSummary}>{query.question}</div>
        </div>
        <Badge tone="neutral">{query.kind}</Badge>
      </div>

      <p className={styles.queryExplain}>{query.explain}</p>

      {query.kind === "sql" ? (
        <textarea
          className={styles.queryEditor}
          spellCheck={false}
          rows={Math.min(8, Math.max(2, sqlText.split("\n").length))}
          value={sqlText}
          onChange={(e) => setSqlText(e.target.value)}
          aria-label={`SQL for ${query.title}`}
        />
      ) : (
        <CodeBlock code={JSON.stringify(query.request, null, 2)} lang="json" wrap />
      )}

      <div className={styles.cardActions} style={{ marginTop: "var(--s-3)" }}>
        {query.kind === "sql" && (
          <>
            <Button size="sm" variant="ghost" icon={<Save size={14} />} onClick={save}>Save to Query</Button>
            <Link to={`/query?q=${encodeURIComponent(sqlText)}&run=1`} className={styles.openInQuery}>
              Open in Query <ArrowUpRight size={13} aria-hidden />
            </Link>
          </>
        )}
        <span className={styles.footerSpacer} />
        <Button size="sm" variant="primary" icon={running ? <Loader2 size={14} className={styles.spin} /> : <Play size={14} />} onClick={() => void run()} disabled={running}>
          {running ? "Running…" : "Run"}
        </Button>
      </div>

      {problem && <div style={{ marginTop: "var(--s-3)" }}><ProblemView problem={problem} /></div>}
      {result && <div style={{ marginTop: "var(--s-3)" }}>{result}</div>}
    </div>
  );
}

function SqlResult({ resp }: { resp: SqlResp }): ReactNode {
  if (!resp.rows.length) return <div className={styles.jobPhase}>No rows.</div>;
  const cols = Object.keys(resp.rows[0]!).slice(0, 6);
  return (
    <div>
      <div className={styles.miniTableWrap}>
        <table className={styles.miniTable}>
          <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>
            {resp.rows.slice(0, 8).map((r, i) => (
              <tr key={i}>{cols.map((c) => <td key={c}>{cell(r[c])}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className={styles.resultMeta}>{resp.row_count.toLocaleString()} row{resp.row_count === 1 ? "" : "s"}{resp.truncated ? " · truncated" : ""}</div>
    </div>
  );
}

function FindResult({ resp }: { resp: FindResp }): ReactNode {
  if (!resp.episodes.length) return <div className={styles.jobPhase}>No matches.</div>;
  return (
    <div className={styles.findList}>
      {resp.episodes.slice(0, 5).map((ep, i) => {
        const messages = ep.messages as Array<Record<string, unknown>> | undefined;
        const first = messages?.[0];
        const text = String((first?.text ?? ep.text ?? "") as string);
        const when = String((ep.time_from ?? ep.time ?? "") as string);
        return (
          <div key={i} className={styles.findItem}>
            <div className={styles.findHead}>
              <span className={styles.findChannel}>{String(ep.channel ?? "")}</span>
              {when && <span className={styles.findTime}>{when}</span>}
            </div>
            {text && <div className={styles.findSnippet}>{text.slice(0, 180)}</div>}
          </div>
        );
      })}
    </div>
  );
}

function TimelineResult({ resp }: { resp: TimelineResp }): ReactNode {
  const years = Object.entries(resp.episodes_by_year ?? {});
  const max = Math.max(1, ...years.map(([, n]) => n));
  return (
    <div>
      <div className={styles.resultMeta} style={{ marginBottom: "var(--s-2)" }}>{resp.total_episodes.toLocaleString()} episodes across {years.length} year{years.length === 1 ? "" : "s"}</div>
      <div className={styles.yearBars}>
        {years.map(([y, n]) => (
          <div key={y} className={styles.yearBar}>
            <span className={styles.yearLabel}>{y}</span>
            <span className={styles.yearTrack}><span className={styles.yearFill} style={{ width: `${Math.round((n / max) * 100)}%` }} /></span>
            <span className={styles.yearCount}>{n}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function cell(v: unknown): ReactNode {
  if (v == null) return <span style={{ color: "var(--ink-3)" }}>—</span>;
  if (typeof v === "object") return <span className="tnum">{JSON.stringify(v).slice(0, 40)}</span>;
  return String(v).slice(0, 60);
}
