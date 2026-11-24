import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Users, Check, ChevronRight, Sparkles, UserCheck, Loader2 } from "lucide-react";
import { Skeleton, Button, Input, Field, EmptyState } from "@/ui";
import {
  setupProblem,
  usePeople,
  useSetMe,
  useResolvePeople,
  useAnswerQuestion,
  type MergeQuestion,
  type PersonCard,
} from "@/api/setup";
import { StepHeader, WizardFooter } from "./parts";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

export function PeopleStep(): ReactNode {
  const people = usePeople(true);
  const data = people.data;
  const running = Boolean(data?.running);

  const resolve = useResolvePeople();
  const [meDeferred, setMeDeferred] = useState(false);
  const [autoOpen, setAutoOpen] = useState(false);

  const openQuestions = useMemo(() => (data?.questions ?? []).filter((q) => q.status === "open"), [data?.questions]);
  const needsMe = Boolean(data && !data.me && data.me_candidates.length > 0 && !meDeferred);

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Who's who"
        title="Make sense of your people"
        lede="The same person shows up as a dozen handles across your sources. Throughline links them into one, so a search for someone finds everything — however they reached you."
      />

      <div className={styles.body}>
        {people.isLoading ? (
          <div className={styles.loadingStack}>
            <Skeleton height={80} />
            <Skeleton height={160} />
          </div>
        ) : !data ? null : needsMe ? (
          <MePicker candidates={data.me_candidates} onSkip={() => setMeDeferred(true)} />
        ) : (
          <>
            {data.stats.auto_merged > 0 && (
              <AutoSummary count={data.stats.auto_merged} people={data.stats.people} auto={data.auto} open={autoOpen} onToggle={() => setAutoOpen((v) => !v)} />
            )}

            {running ? (
              <div className={styles.importOverall}>
                <div className={styles.jobRowTop}>
                  <Loader2 size={18} className={styles.spin} style={{ color: "var(--brass)" }} aria-hidden />
                  <div style={{ flex: 1 }}>
                    <div className={styles.setupCardName}>{data.running?.phase ?? "Linking people…"}</div>
                    <div className={styles.setupCardSummary}>Questions will appear as it works.</div>
                  </div>
                </div>
                <div className={`${styles.bar} ${styles.barIndeterminate}`} style={{ marginTop: "var(--s-3)" }} />
              </div>
            ) : openQuestions.length > 0 ? (
              <QuestionCard question={openQuestions[0]!} index={data.stats.answered + 1} total={data.stats.answered + openQuestions.length} />
            ) : data.stats.answered > 0 || data.stats.auto_merged > 0 ? (
              <EmptyState icon={<Check size={22} />} title="All clear" description="No one left to sort out. You can always split or merge people later from the People screen." />
            ) : (
              <EmptyState
                icon={<Users size={22} />}
                title="Find connections"
                description={data.llm_assist ? "Your assistant model will help link accounts that belong to the same person." : "Link accounts that clearly belong to the same person. Configure an assistant model for sharper suggestions."}
                action={
                  <Button variant="primary" icon={<Sparkles size={15} />} onClick={() => resolve.mutate()} disabled={resolve.isPending}>
                    {resolve.isPending ? "Starting…" : "Link my people"}
                  </Button>
                }
              />
            )}
          </>
        )}
      </div>

      <WizardFooter skipLabel={openQuestions.length > 0 ? "Skip the rest" : "Skip"} />
    </div>
  );
}

function MePicker({ candidates, onSkip }: { candidates: Array<PersonCard & { reason: string }>; onSkip: () => void }): ReactNode {
  const setMe = useSetMe();
  return (
    <div>
      <div className={styles.sectionLabel}><UserCheck size={13} /> Which of these is you?</div>
      <div className={styles.cards}>
        {candidates.map((c) => (
          <button key={c.id} type="button" className={styles.choice} onClick={() => setMe.mutate({ person_id: c.id })} disabled={setMe.isPending}>
            <span className={styles.choiceIcon}><Users size={18} /></span>
            <span className={styles.choiceText}>
              <span className={styles.choiceTitle}>{c.name}</span>
              <span className={styles.choiceDesc}>{c.reason}</span>
            </span>
            <ChevronRight size={16} className={styles.choiceMark} aria-hidden />
          </button>
        ))}
      </div>
      {setMe.error && <ProblemView problem={setupProblem(setMe.error)} />}
      <button type="button" className={styles.advancedToggle} onClick={onSkip} style={{ marginTop: "var(--s-3)" }}>
        None of these / decide later
      </button>
    </div>
  );
}

function AutoSummary({
  count,
  people,
  auto,
  open,
  onToggle,
}: {
  count: number;
  people: number;
  auto: Array<{ name: string; merged: string[]; reason: string; confidence: number }>;
  open: boolean;
  onToggle: () => void;
}): ReactNode {
  return (
    <div className={styles.autoSummary}>
      <div className={styles.autoSummaryHead}>
        <Check size={16} style={{ color: "var(--ok)" }} aria-hidden />
        Linked {count} account{count === 1 ? "" : "s"} into {people} {people === 1 ? "person" : "people"} automatically
      </div>
      <p style={{ margin: "var(--s-2) 0 0", fontSize: "var(--t-sm)", color: "var(--ink-2)" }}>
        These were clear matches, so they're done — you can split anyone later in People.
      </p>
      {auto.length > 0 && (
        <>
          <button type="button" className={styles.advancedToggle} onClick={onToggle} aria-expanded={open} style={{ marginTop: "var(--s-2)" }}>
            <ChevronRight size={14} style={{ transform: open ? "rotate(90deg)" : "none" }} aria-hidden />
            {open ? "Hide" : "Show"} what was linked
          </button>
          {open && (
            <ul className={styles.autoList}>
              {auto.map((a, i) => (
                <li key={i} className={styles.autoItem}>
                  <strong>{a.name}</strong> — {a.merged.join(", ")} <span style={{ color: "var(--ink-3)" }}>· {a.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

function QuestionCard({ question, index, total }: { question: MergeQuestion; index: number; total: number }): ReactNode {
  const answer = useAnswerQuestion();
  const [name, setName] = useState(question.suggested_name ?? "");

  // Keyboard 1 / 2 / 3 → same / different / not sure.
  useEffect(() => {
    setName(question.suggested_name ?? "");
    function onKey(e: KeyboardEvent): void {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (e.key === "1") answer.mutate({ id: question.id, answer: "same", name: name.trim() || undefined });
      else if (e.key === "2") answer.mutate({ id: question.id, answer: "different" });
      else if (e.key === "3") answer.mutate({ id: question.id, answer: "skip" });
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question.id, name]);

  const pct = Math.round(question.confidence * 100);
  const level = pct >= 75 ? "good" : pct >= 45 ? "fair" : "weak";

  return (
    <div className={styles.question}>
      <div className={styles.questionCount}>{index} of {total}</div>
      <p className={styles.questionPrompt}>{question.prompt}</p>

      <div className={styles.personPair}>
        <PersonMini person={question.a} />
        <div className={styles.pairVs}>vs</div>
        <PersonMini person={question.b} />
      </div>

      <div className={styles.confidence}>
        <div className={styles.confidenceHead}>
          <span>Confidence</span>
          <span className="tnum">{pct}%</span>
        </div>
        <div className={styles.progressTrack}>
          <div className={styles.progressFill} style={{ width: `${pct}%`, background: level === "good" ? "var(--ok)" : level === "fair" ? "var(--warn)" : "var(--bad)" }} />
        </div>
      </div>

      {question.reasons.length > 0 && (
        <ul className={styles.reasons}>
          {question.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}

      <Field label="If they're the same, call them" htmlFor="merge-name" className={styles.confidence}>
        <Input id="merge-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Name for the merged person" />
      </Field>

      <div className={styles.answerRow}>
        <Button variant="primary" onClick={() => answer.mutate({ id: question.id, answer: "same", name: name.trim() || undefined })} disabled={answer.isPending}>
          Same person <span className={styles.answerKey}>1</span>
        </Button>
        <Button variant="default" onClick={() => answer.mutate({ id: question.id, answer: "different" })} disabled={answer.isPending}>
          Different people <span className={styles.answerKey}>2</span>
        </Button>
        <span className={styles.footerSpacer} />
        <Button variant="ghost" onClick={() => answer.mutate({ id: question.id, answer: "skip" })} disabled={answer.isPending}>
          Not sure <span className={styles.answerKey}>3</span>
        </Button>
      </div>
    </div>
  );
}

function PersonMini({ person }: { person: PersonCard }): ReactNode {
  return (
    <div className={styles.personCard}>
      <div className={styles.personName}>{person.name}</div>
      <div className={styles.personHandles}>
        {person.handles.slice(0, 4).map((h, i) => (
          <div key={i} className={styles.personHandle}>
            <span className={styles.personHandleSys}>{h.system}</span>
            <span>{h.value}</span>
          </div>
        ))}
      </div>
      <div className={styles.personMeta}>
        <span className="tnum">{person.messages.toLocaleString()} messages</span>
        {person.last_at && <span>last seen {new Date(person.last_at).toLocaleDateString()}</span>}
      </div>
    </div>
  );
}
