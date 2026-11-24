import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Check, Play, Table2, Layers, Sparkles, Loader2 } from "lucide-react";
import { Skeleton, Button, Badge, ErrorState } from "@/ui";
import { request } from "@/api";
import {
  useLensesSetup,
  useApplyLenses,
  runLens,
  useJobs,
  setupProblem,
  type LensTemplate,
  type LensApplied,
  type PaceId,
  type Job,
} from "@/api/setup";
import { useWizard } from "./wizard";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { PluginIcon } from "./PluginIcon";
import { PaceControl } from "./PaceControl";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

function daysSince(iso?: string): number | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return Math.max(1, Math.round((Date.now() - d.getTime()) / 86_400_000));
}

function quoteLine(q: LensApplied["quote"]): string {
  const parts: string[] = [];
  const days = daysSince(q.since);
  if (days != null) parts.push(`~${days} days to read`);
  else if (q.items != null) parts.push(`~${q.items.toLocaleString()} to read`);
  if (q.cost_usd != null) parts.push(q.cost_usd === 0 ? "free" : `~$${q.cost_usd.toFixed(2)}`);
  return parts.join(" · ") || "ready";
}

export function LensesStep(): ReactNode {
  const wiz = useWizard();
  const lenses = useLensesSetup();
  const apply = useApplyLenses();
  const view = lenses.data;

  const [selected, setSelected] = useState<Set<string> | null>(null);
  const [applied, setApplied] = useState<LensApplied[] | null>(null);

  const pace = ((wiz.choices.pace as PaceId) ?? view?.pace ?? "balanced") as PaceId;
  const setPace = (id: PaceId): void => wiz.setChoices({ pace: id });

  // Preselect all available templates on first load.
  useEffect(() => {
    if (selected === null && view) {
      setSelected(new Set(view.templates.filter((t) => t.available).map((t) => t.id)));
    }
  }, [view, selected]);

  const sel = selected ?? new Set<string>();
  const toggle = (t: LensTemplate): void => {
    if (!t.available) return;
    const next = new Set(sel);
    if (next.has(t.id)) next.delete(t.id);
    else next.add(t.id);
    setSelected(next);
  };

  const onCreate = (): void => {
    apply.mutate(
      { templates: [...sel], pace },
      { onSuccess: (r) => setApplied(r.lenses) },
    );
  };

  const appliedTemplate = (name: string): LensTemplate | undefined => view?.templates.find((t) => t.lens === name);

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Your first lenses"
        title="Teach your hub to keep notes"
        lede="A lens is a table an AI keeps filled from your conversations — your plans, promises, the dates that matter — and each one is also a SQL view (api.lens_<name>) you and your agents can query."
      />

      <div className={styles.body}>
        {lenses.isLoading ? (
          <div className={styles.loadingStack}>
            <Skeleton height={100} />
            <Skeleton height={160} />
          </div>
        ) : lenses.error ? (
          <ErrorState error={lenses.error} title="Could not load lenses" onRetry={() => void lenses.refetch()} />
        ) : view ? (
          <>
            {!view.model.ready && (
              <Callout tone="warn">
                {view.model.hint ?? "Lenses need a model that can read your conversations."}
                <div style={{ marginTop: "var(--s-3)" }}>
                  <Button size="sm" variant="default" icon={<Sparkles size={14} />} onClick={() => wiz.goTo("models")}>
                    Set up a model
                  </Button>
                </div>
              </Callout>
            )}

            <section>
              <div className={styles.sectionLabel}>How often lenses update</div>
              <PaceControl paces={view.paces} value={pace} onChange={setPace} />
            </section>

            <section>
              <div className={styles.sectionLabel}><Layers size={13} /> Starter lenses</div>
              <div className={styles.catalogGrid}>
                {view.templates.map((t) => (
                  <LensTemplateCard key={t.id} template={t} selected={sel.has(t.id)} onToggle={() => toggle(t)} />
                ))}
              </div>
            </section>

            {applied ? (
              <section>
                <div className={styles.sectionLabel}><Check size={13} /> Created</div>
                <div className={styles.setupList}>
                  {applied.map((a) => (
                    <LensResult key={a.name} applied={a} template={appliedTemplate(a.name)} />
                  ))}
                </div>
              </section>
            ) : (
              <div className={styles.cardActions}>
                <span className={styles.footerSpacer} />
                <Button
                  variant="primary"
                  icon={apply.isPending ? <Loader2 size={15} className={styles.spin} /> : <Sparkles size={15} />}
                  onClick={onCreate}
                  disabled={apply.isPending || sel.size === 0}
                >
                  {apply.isPending ? "Creating…" : `Create ${sel.size} lens${sel.size === 1 ? "" : "es"}`}
                </Button>
              </div>
            )}
            {apply.error && <ProblemView problem={setupProblem(apply.error)} />}

            {view.others.length > 0 && (
              <section>
                <div className={styles.sectionLabel}>Already here</div>
                <div className={styles.detectList}>
                  {view.others.map((o) => (
                    <div key={o.name} className={styles.detectItem}>
                      <span className={styles.setupCardIcon} style={{ width: 28, height: 28 }}><Table2 size={15} /></span>
                      <span>{o.description || o.name}</span>
                      <span className={styles.footerSpacer} />
                      <span className={styles.detectRecords}>{o.rows.toLocaleString()} rows</span>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </>
        ) : null}
      </div>

      <WizardFooter />
    </div>
  );
}

function LensTemplateCard({ template, selected, onToggle }: { template: LensTemplate; selected: boolean; onToggle: () => void }): ReactNode {
  const on = selected || template.enabled;
  return (
    <button
      type="button"
      className={styles.srcCard}
      data-selected={on}
      data-locked={template.enabled}
      aria-pressed={on}
      onClick={onToggle}
      disabled={!template.available}
      title={template.why_not ?? undefined}
    >
      <div className={styles.srcTop}>
        <span className={styles.srcIcon}><PluginIcon name={template.icon} size={18} /></span>
        <span className={styles.srcName}>{template.title}</span>
        {on && <Check size={16} className={styles.srcCheck} style={{ color: template.enabled ? "var(--ok)" : "var(--brass)" }} aria-hidden />}
      </div>
      <div className={styles.srcSummary}>{template.pitch}</div>
      <div className={styles.srcBadges}>
        {template.columns.slice(0, 4).map((c) => (
          <Badge key={c} tone="neutral">{c}</Badge>
        ))}
      </div>
      {template.enabled ? (
        <div className={styles.srcConnected}><Check size={12} /> Already on · {template.rows.toLocaleString()} rows</div>
      ) : !template.available && template.why_not ? (
        <div className={styles.srcWhyNot}>{template.why_not}</div>
      ) : null}
    </button>
  );
}

function LensResult({ applied, template }: { applied: LensApplied; template?: LensTemplate }): ReactNode {
  const jobs = useJobs(true);
  const [jobId, setJobId] = useState<number | null>(null);
  const [rows, setRows] = useState<Array<Record<string, unknown>> | null>(null);
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ReturnType<typeof setupProblem> | null>(null);

  const job: Job | undefined = jobId != null ? jobs.data?.jobs.find((j) => j.id === jobId) : undefined;
  const running = job != null && (job.status === "running" || job.status === "queued");
  const done = job != null && job.status === "done";

  // When the run finishes, read a few rows from the lens view.
  useEffect(() => {
    if (done && rows === null && !busy) {
      setBusy(true);
      request<{ rows: Array<Record<string, unknown>> }>(`/lenses/${encodeURIComponent(applied.name)}/rows?limit=5`)
        .then((r) => { setRows(r.rows ?? []); void qc.invalidateQueries({ queryKey: ["setup", "lenses"] }); })
        .catch((e) => setProblem(setupProblem(e)))
        .finally(() => setBusy(false));
    }
  }, [done, rows, busy, applied.name]);

  const run = (): void => {
    setProblem(null);
    runLens(applied.name)
      .then((j) => setJobId(j.id))
      .catch((e) => setProblem(setupProblem(e)));
  };

  const cols = useMemo(() => {
    if (template?.columns.length) return template.columns;
    return rows && rows[0] ? Object.keys(rows[0]).filter((k) => k !== "id").slice(0, 4) : [];
  }, [template, rows]);

  return (
    <div className={styles.setupCard} style={{ padding: "var(--s-4)" }}>
      <div className={styles.jobRowTop}>
        <span className={styles.setupCardIcon} style={{ width: 32, height: 32 }}><PluginIcon name={template?.icon ?? "table-2"} size={16} /></span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className={styles.setupCardName}>{template?.title ?? applied.name}</div>
          <div className={styles.setupCardSummary}>
            {applied.quote.error ? <span style={{ color: "var(--bad)" }}>{applied.quote.error}</span> : quoteLine(applied.quote)}
          </div>
        </div>
        {done ? (
          <Badge tone="ok"><Check size={11} /> filled</Badge>
        ) : (
          <Button size="sm" variant={running ? "ghost" : "default"} icon={running ? <Loader2 size={14} className={styles.spin} /> : <Play size={14} />} onClick={run} disabled={running || Boolean(applied.quote.error)}>
            {running ? "Running…" : "Run now"}
          </Button>
        )}
      </div>

      {running && (
        <div className={styles.jobBarWrap}>
          <div className={styles.jobPhase}>{job?.phase ?? "Reading your conversations…"}</div>
          <div className={`${styles.bar} ${job?.progress.fraction == null ? styles.barIndeterminate : ""}`} style={{ marginTop: 6 }}>
            {job?.progress.fraction != null && <div className={styles.barFill} style={{ width: `${Math.round(job.progress.fraction * 100)}%` }} />}
          </div>
        </div>
      )}

      {problem && <div style={{ marginTop: "var(--s-3)" }}><ProblemView problem={problem} /></div>}

      {rows && rows.length > 0 && cols.length > 0 && (
        <div className={styles.miniTableWrap}>
          <table className={styles.miniTable}>
            <thead>
              <tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {rows.slice(0, 3).map((r, i) => (
                <tr key={i}>{cols.map((c) => <td key={c}>{cell(r[c])}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rows && rows.length === 0 && <div className={styles.jobPhase} style={{ marginTop: "var(--s-3)" }}>No rows yet — the lens will fill as it reads more.</div>}
    </div>
  );
}

function cell(v: unknown): ReactNode {
  if (v == null) return <span style={{ color: "var(--ink-3)" }}>—</span>;
  if (typeof v === "object") return <span className="tnum">{JSON.stringify(v).slice(0, 48)}</span>;
  return String(v).slice(0, 64);
}
