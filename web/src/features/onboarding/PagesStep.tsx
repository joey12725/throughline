import { useEffect, useMemo, useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Play, FileText, FolderDown, Loader2, Sparkles, BookOpen } from "lucide-react";
import { Skeleton, Button, Badge, Switch, Field, Input, ErrorState } from "@/ui";
import { request } from "@/api";
import {
  usePagesSetup,
  useApplyPages,
  runPages,
  useJobs,
  setupProblem,
  type PageFamily,
  type PaceId,
  type Job,
} from "@/api/setup";
import { useWizard } from "./wizard";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { PluginIcon } from "./PluginIcon";
import { PaceControl } from "./PaceControl";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

/** Page text for reading: [[person:5|Name]] links become the name, and
 * [m:12,13] source citations a small superscript count (the full page in
 * Throughline links each one to its source). */
function readable(content: string): string {
  const sup = "⁰¹²³⁴⁵⁶⁷⁸⁹";
  return (content || "")
    .replace(/\[\[[a-z]+:[^|\]]+\|([^\]]+)\]\]/g, "**$1**")
    .replace(/\[\[[a-z]+:([^\]]+)\]\]/g, "$1")
    .replace(/\s?\[m:([\d,\s]+)\]/g, (_m, ids: string) => {
      const n = ids.split(",").filter((x) => x.trim()).length;
      return String(n).split("").map((d) => sup[Number(d)]).join("");
    });
}

interface PageHit {
  slug: string;
  kind: string;
  title: string;
  summary?: string | null;
  updated_at?: string | null;
}
interface PageDoc {
  title: string;
  slug: string;
  body: string;
  summary?: string | null;
  updated_at?: string | null;
}

export function PagesStep(): ReactNode {
  const wiz = useWizard();
  const pagesQ = usePagesSetup();
  const apply = useApplyPages();
  const view = pagesQ.data;

  const [selected, setSelected] = useState<Set<string> | null>(null);
  const [markdownOn, setMarkdownOn] = useState(false);
  const [markdownDir, setMarkdownDir] = useState("");
  const [applied, setApplied] = useState(false);
  const [plans, setPlans] = useState<Record<string, { due?: number; error?: string }> | null>(null);

  const pace = ((wiz.choices.pace as PaceId) ?? view?.pace ?? "balanced") as PaceId;
  const setPace = (id: PaceId): void => wiz.setChoices({ pace: id });

  useEffect(() => {
    if (selected === null && view) {
      // Preselect the available families.
      setSelected(new Set(view.families.filter((f) => f.available).map((f) => f.id)));
      setMarkdownOn(Boolean(view.markdown_dir));
      setMarkdownDir(view.markdown_dir ?? "");
    }
  }, [view, selected]);

  const sel = selected ?? new Set<string>();

  const toggle = (f: PageFamily): void => {
    if (!f.available) return;
    const next = new Set(sel);
    if (next.has(f.id)) next.delete(f.id);
    else next.add(f.id);
    setSelected(next);
  };

  const onSave = (): void => {
    apply.mutate(
      { families: [...sel], pace, markdown_dir: markdownOn ? markdownDir.trim() || null : null },
      { onSuccess: (r) => { setApplied(true); setPlans(r.plans); } },
    );
  };

  const firstFamily = useMemo(() => view?.families.find((f) => sel.has(f.id)) ?? null, [sel, view]);

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Pages that write themselves"
        title="A running record, kept for you"
        lede="Pages are living documents an AI keeps up to date from your archive — a page for each day, each person, each event — so you always have something to read instead of a pile of raw messages."
      />

      <div className={styles.body}>
        {pagesQ.isLoading ? (
          <div className={styles.loadingStack}><Skeleton height={100} /><Skeleton height={160} /></div>
        ) : pagesQ.error ? (
          <ErrorState error={pagesQ.error} title="Could not load pages" onRetry={() => void pagesQ.refetch()} />
        ) : view && !view.available ? (
          <Callout tone="warn">{view.why_not ?? "Pages need a model that can write from your archive."}
            <div style={{ marginTop: "var(--s-3)" }}>
              <Button size="sm" variant="default" icon={<Sparkles size={14} />} onClick={() => wiz.goTo("models")}>Set up a model</Button>
            </div>
          </Callout>
        ) : view ? (
          <>
            {!view.model.ready && (
              <Callout tone="warn">{view.model.hint ?? "Pages need a model configured in the Intelligence step."}</Callout>
            )}

            <section>
              <div className={styles.sectionLabel}><BookOpen size={13} /> What to keep</div>
              <div className={styles.catalogGrid}>
                {view.families.map((f) => (
                  <FamilyCard key={f.id} family={f} selected={sel.has(f.id)} onToggle={() => toggle(f)} />
                ))}
              </div>
            </section>

            <section>
              <div className={styles.sectionLabel}>How often pages update</div>
              <PaceControl paces={view.paces} value={pace} onChange={setPace} />
            </section>

            <section>
              <Field label={<span className={styles.inlineIcon}><FolderDown size={14} /> Also save as Markdown files</span>} inline hint="Write each page to a folder on the box, so other tools can read them too">
                <Switch checked={markdownOn} onChange={setMarkdownOn} label="Save as Markdown files" />
              </Field>
              {markdownOn && (
                <Field label="Folder" htmlFor="pages-md-dir" className={styles.confidence}>
                  <Input id="pages-md-dir" mono value={markdownDir} placeholder="~/Documents/Throughline" onChange={(e) => setMarkdownDir(e.target.value)} />
                </Field>
              )}
            </section>

            <div className={styles.cardActions}>
              <span className={styles.footerSpacer} />
              <Button variant={applied ? "default" : "primary"} icon={apply.isPending ? <Loader2 size={15} className={styles.spin} /> : <Check size={15} />} onClick={onSave} disabled={apply.isPending || sel.size === 0}>
                {apply.isPending ? "Saving…" : applied ? "Update" : "Save pages"}
              </Button>
            </div>
            {apply.error && <ProblemView problem={setupProblem(apply.error)} />}

            {plans && (
              <div className={styles.noteRow}>
                {Object.entries(plans).map(([fam, p]) => (
                  <span key={fam}>
                    <FileText size={13} /> {p.error ? <span style={{ color: "var(--bad)" }}>{fam}: {p.error}</span> : `${p.due ?? 0} ${fam} ${(p.due ?? 0) === 1 ? "page" : "pages"} ready to write`}
                  </span>
                ))}
              </div>
            )}

            {applied && firstFamily && <PagesResult family={firstFamily.id} kind={firstFamily.kind} />}
          </>
        ) : null}
      </div>

      <WizardFooter />
    </div>
  );
}

function FamilyCard({ family, selected, onToggle }: { family: PageFamily; selected: boolean; onToggle: () => void }): ReactNode {
  const on = selected || family.enabled;
  const disabled = !family.available;
  return (
    <button
      type="button"
      className={styles.srcCard}
      data-selected={on}
      data-locked={family.enabled}
      aria-pressed={on}
      onClick={onToggle}
      disabled={disabled}
      title={family.why_not ?? undefined}
    >
      <div className={styles.srcTop}>
        <span className={styles.srcIcon}><PluginIcon name={family.icon} size={18} /></span>
        <span className={styles.srcName}>{family.title}</span>
        {on && <Check size={16} className={styles.srcCheck} style={{ color: family.enabled ? "var(--ok)" : "var(--brass)" }} aria-hidden />}
      </div>
      <div className={styles.srcSummary}>{family.pitch}</div>
      {family.enabled ? (
        <div className={styles.srcConnected}><Check size={12} /> On · {family.pages.toLocaleString()} pages</div>
      ) : !family.available && family.why_not ? (
        <div className={styles.srcWhyNot}>{family.why_not}</div>
      ) : null}
    </button>
  );
}

function PagesResult({ family, kind }: { family: string; kind: string }): ReactNode {
  const jobs = useJobs(true);
  const [jobId, setJobId] = useState<number | null>(null);
  const [hits, setHits] = useState<PageHit[] | null>(null);
  const [doc, setDoc] = useState<PageDoc | null>(null);
  const [problem, setProblem] = useState<ReturnType<typeof setupProblem> | null>(null);
  const [loading, setLoading] = useState(false);

  const job: Job | undefined = jobId != null ? jobs.data?.jobs.find((j) => j.id === jobId) : undefined;
  const written = job?.progress.delivered ?? 0;
  const running = job != null && (job.status === "running" || job.status === "queued");
  const done = job != null && job.status === "done";

  useEffect(() => {
    if (done && hits === null && !loading) {
      setLoading(true);
      // The newest page of this family's kind (most recently updated first).
      request<PageHit[]>(`/pages?kind=${encodeURIComponent(kind)}&limit=5`)
        .then(async (r) => {
          setHits(r ?? []);
          const newest = (r ?? [])[0];
          if (!newest) {
            setProblem({ title: "No page yet", message: "None of the recent days had enough to write about.", hint: "Pages appear once there is enough to say. They'll keep coming as new data arrives." });
          } else {
            const page = await request<PageDoc>(`/pages/${newest.slug.split("/").map(encodeURIComponent).join("/")}`);
            setDoc(page);
          }
        })
        .catch((e) => setProblem(setupProblem(e)))
        .finally(() => setLoading(false));
    }
  }, [done, hits, loading, kind]);

  const run = (): void => {
    setProblem(null);
    runPages(family).then((j) => setJobId(j.id)).catch((e) => setProblem(setupProblem(e)));
  };

  return (
    <div style={{ marginTop: "var(--s-2)" }}>
      {!running && !done && (
        <div className={styles.cardActions}>
          <span className={styles.footerSpacer} />
          <Button variant="primary" icon={<Play size={15} />} onClick={run}>Write my first pages</Button>
        </div>
      )}

      {running && (
        <div className={styles.importOverall}>
          <div className={styles.jobRowTop}>
            <Loader2 size={18} className={styles.spin} style={{ color: "var(--brass)" }} aria-hidden />
            <div style={{ flex: 1 }}>
              <div className={styles.setupCardName}>{job?.phase ?? "Writing pages…"}</div>
              <div className={styles.setupCardSummary}>{(job?.progress.delivered ?? 0).toLocaleString()} written</div>
            </div>
          </div>
          <div className={`${styles.bar} ${job?.progress.fraction == null ? styles.barIndeterminate : ""}`} style={{ marginTop: "var(--s-3)" }}>
            {job?.progress.fraction != null && <div className={styles.barFill} style={{ width: `${Math.round(job.progress.fraction * 100)}%` }} />}
          </div>
        </div>
      )}

      {problem && <ProblemView problem={problem} />}

      {doc && (
        <>
          <div className={styles.noteRow} style={{ marginBottom: "var(--s-2)" }}>
            <Badge tone="ok"><Check size={11} /> {written} {family} {written === 1 ? "page" : "pages"} written</Badge>
          </div>
          <article className={styles.pageReader} aria-label={`${family} page preview`}>
            <Markdown remarkPlugins={[remarkGfm]}>{readable(doc.body) || `# ${doc.title}\n\n_(empty page)_`}</Markdown>
          </article>
        </>
      )}
    </div>
  );
}
