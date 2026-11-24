/*
 * Review one judged conversation: participants with their turn and flagged
 * counts, the classifier's verdict per participant, and the transcript with
 * flagged turns marked (and who marked them). The owner corrects by speaker or
 * for the whole conversation; the owner's verdict wins over every classifier
 * and is copied onto the turns. Corrections can be undone.
 *
 * Quick yes/no buttons appear when the attribute's schema has one boolean
 * field; any other schema gets the full value editor in a dialog.
 */
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Undo2, UserRound } from "lucide-react";
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Page,
  PageHeader,
  Panel,
  RelativeTime,
  Select,
  Sensitive,
  Skeleton,
  Switch,
  toast,
} from "@/ui";
import {
  createGroupAnnotation,
  retractGroupAnnotation,
  useClassifier,
  useInvalidateClassifiers,
  useThreadClassification,
  type ClassificationMessage,
  type GroupAnnotation,
  type Mark,
  type PythonJudgement,
  type ThreadClassification,
  type ValueSchema,
} from "@/api/classifiers";
import {
  correctionLabels,
  defaultValue,
  formatDuration,
  formatOffset,
  isFlagged,
  summarizeDetail,
  summarizeValue,
  toggleField,
  validateValue,
  type Value,
} from "./model";
import { ValueEditor, VerdictBadge, errorText } from "./shared";
import styles from "./Classifiers.module.css";

const SHOW_STEP = 400;

interface Target {
  /** undefined = whole conversation */
  participantKey?: string;
  label: string;
}

export function ThreadReview({ name, threadId }: { name: string; threadId: string }): ReactNode {
  const cl = useClassifier(name);
  const attribute = cl.data?.attribute;
  const q = useThreadClassification(threadId, attribute, name);
  const schema: ValueSchema = cl.data?.attribute_definition?.value_schema ?? {};
  const flagField = cl.data?.attribute_definition?.flag ?? null;

  const back = (
    <Link to={`/classifiers/${encodeURIComponent(name)}`} className={styles.back}>
      <ArrowLeft size={13} /> {name}
    </Link>
  );

  if (cl.isLoading || (q.isLoading && !!attribute)) {
    return (
      <Page width="wide">
        <Skeleton height={32} />
        <Skeleton height={400} />
      </Page>
    );
  }
  const err = cl.error ?? q.error;
  if (err || !q.data || !cl.data) {
    return (
      <Page width="wide">
        {back}
        <ErrorState error={err ?? "No data"} title="Could not load this conversation" onRetry={() => void (cl.error ? cl.refetch() : q.refetch())} />
      </Page>
    );
  }
  return <Review name={name} data={q.data} schema={schema} flagField={flagField} back={back} />;
}

function Review({
  name,
  data,
  schema,
  flagField,
  back,
}: {
  name: string;
  data: ThreadClassification;
  schema: ValueSchema;
  flagField: string | null;
  back: ReactNode;
}): ReactNode {
  const invalidate = useInvalidateClassifiers();
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<Target | null>(null);
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const [speaker, setSpeaker] = useState("");
  const [shown, setShown] = useState(SHOW_STEP);

  const tf = toggleField(schema, flagField);
  const flagged = (v: Value | null | undefined) => isFlagged(v, flagField);
  const words = correctionLabels(data.attribute);
  const verdict = data.verdicts.find((v) => v.name === name) ?? null;
  const judgement = (verdict?.result ?? null) as PythonJudgement | null;
  const labelOf = useMemo(() => new Map(data.participants.map((p) => [p.key, p.label ?? p.key])), [data.participants]);

  const ga = (kind: "thread" | "participant", key: string | null, owner: boolean): GroupAnnotation | undefined =>
    data.group_annotations.find(
      (g) => g.target_kind === kind && (kind === "thread" || g.participant_key === key) && (g.method === "owner") === owner,
    );
  const marksFor = (key: string): Mark[] => (judgement?.marks ?? []).filter((m) => m.target === "participant" && m.key === key);

  const write = async (target: Target, value: Value | null, tag: string, note?: string) => {
    setBusy(tag);
    try {
      await createGroupAnnotation({
        thread_id: data.thread.id,
        attribute: data.attribute,
        ...(target.participantKey !== undefined ? { participant_key: target.participantKey } : {}),
        value,
        reason: note || "owner correction from the web review",
      });
      toast.ok("Correction saved", `${target.label}: ${summarizeValue(value)}`);
      await invalidate();
    } catch (e) {
      toast.error("Could not save the correction", errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const undo = async (g: GroupAnnotation, tag: string) => {
    setBusy(tag);
    try {
      await retractGroupAnnotation(g.id);
      toast.ok("Correction undone", "The classifier's verdict applies again.");
      await invalidate();
    } catch (e) {
      toast.error("Could not undo", errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const controls = (target: Target, owner: GroupAnnotation | undefined, tag: string, yes: string, no: string): ReactNode => {
    const cur = owner?.value ?? undefined;
    return (
      <div className={styles.row}>
        {tf ? (
          <>
            <Button
              size="sm"
              variant={owner && cur?.[tf] === true ? "primary" : "default"}
              aria-pressed={!!owner && cur?.[tf] === true}
              disabled={!!busy}
              onClick={() => void write(target, { ...defaultValue(schema), [tf]: true }, `${tag}:yes`)}
            >
              {yes}
            </Button>
            <Button
              size="sm"
              variant={owner && cur?.[tf] === false ? "primary" : "default"}
              aria-pressed={!!owner && cur?.[tf] === false}
              disabled={!!busy}
              onClick={() => void write(target, { ...defaultValue(schema), [tf]: false }, `${tag}:no`)}
            >
              {no}
            </Button>
          </>
        ) : (
          <Button size="sm" disabled={!!busy} onClick={() => setEditing(target)}>
            Set value…
          </Button>
        )}
        {owner && (
          <Button size="sm" variant="ghost" icon={<Undo2 size={13} />} disabled={!!busy} onClick={() => void undo(owner, `${tag}:undo`)}>
            Undo my correction
          </Button>
        )}
      </div>
    );
  };

  const threadOwner = ga("thread", null, true);
  const threadModel = ga("thread", null, false);
  const msgs = data.messages.filter(
    (m) => (!flaggedOnly || (m.annotation && flagged(m.annotation.value))) && (!speaker || m.participant === speaker),
  );

  return (
    <Page width="wide">
      <PageHeader
        eyebrow={back}
        title={<Sensitive>{data.thread.title || `Thread ${data.thread.id}`}</Sensitive>}
        lede={
          <span className={styles.row}>
            <span className={styles.mono}>{data.thread.channel}</span>
            {data.thread.started_at && <RelativeTime value={data.thread.started_at} exact />}
            {data.thread.duration_s ? <span>{formatDuration(data.thread.duration_s)}</span> : null}
            <span>
              <span className="tnum">{data.flagged_total.toLocaleString()}</span> of <span className="tnum">{data.messages_total.toLocaleString()}</span> turns flagged as{" "}
              <code>{data.attribute}</code>
            </span>
          </span>
        }
      />

      <div className={styles.reviewGrid}>
        <div className={styles.stack}>
          <Panel title="Whole conversation">
            <div className={styles.stack}>
              <div className={styles.row}>
                <span className={styles.note}>{name}:</span>
                {judgement ? <VerdictBadge verdict={judgement.verdict} /> : <Badge>not judged</Badge>}
                {verdict && <span className={styles.note}>v{verdict.version} · <RelativeTime value={verdict.judged_at} /></span>}
              </div>
              {threadModel && (
                <p className={styles.note}>
                  {threadModel.value
                    ? `Classifier marked the whole conversation: ${summarizeValue(threadModel.value, flagField)}`
                    : `Classifier's note: ${summarizeDetail(threadModel.detail) || "no value written"}`}
                  {threadModel.confidence != null && ` (${Math.round(threadModel.confidence * 100)}%)`}
                </p>
              )}
              {threadOwner && (
                <p className={styles.note}>
                  <Badge tone="ok">your correction</Badge> {summarizeValue(threadOwner.value, flagField)} · covers {threadOwner.messages_covered} turns
                </p>
              )}
              {controls({ label: "Whole conversation" }, threadOwner, "thread", words.allYes, words.allNo)}
              {data.verdicts.length > 1 && (
                <div className={styles.stackTight}>
                  <span className={styles.sectionHead}>Other classifiers</span>
                  {data.verdicts
                    .filter((v) => v.name !== name)
                    .map((v) => (
                      <span key={v.name} className={styles.row}>
                        <span className={styles.mono}>{v.name}</span>
                        <VerdictBadge verdict={(v.result as PythonJudgement).verdict} />
                      </span>
                    ))}
                </div>
              )}
            </div>
          </Panel>

          <Panel title="Participants" subtitle="Correct a speaker; your verdict is copied onto their turns">
            {data.participants.length === 0 && <EmptyState compact title="No participants" />}
            {data.participants.map((p) => {
              const owner = ga("participant", p.key, true);
              const model = ga("participant", p.key, false);
              const marks = marksFor(p.key);
              return (
                <div key={p.key} className={styles.pCard}>
                  <div className={styles.rowBetween}>
                    <span className={styles.row}>
                      <UserRound size={14} aria-hidden className={styles.muted} />
                      <Sensitive className={styles.pName}>{p.label ?? p.key}</Sensitive>
                      {p.is_owner && <Badge>you</Badge>}
                    </span>
                    <span className={styles.pCounts}>
                      {p.turns} turns · {p.flagged} flagged
                    </span>
                  </div>
                  <div className={styles.row}>
                    {model ? (
                      <Badge className={styles.wrapBadge} tone={flagged(model.value) ? "warn" : "neutral"}>classifier: {summarizeValue(model.value, flagField)}</Badge>
                    ) : marks.length ? (
                      marks.map((m, i) => (
                        <Badge key={i} className={styles.wrapBadge} tone={flagged(m.value) ? "warn" : "neutral"}>
                          classifier: {summarizeValue(m.value, flagField)}
                        </Badge>
                      ))
                    ) : (
                      <span className={styles.note}>{p.is_owner ? "owner turns are not judged" : "classifier: not marked"}</span>
                    )}
                    {(model?.confidence ?? marks[0]?.confidence) != null && (
                      <span className={styles.note}>{Math.round(((model?.confidence ?? marks[0]?.confidence) as number) * 100)}%</span>
                    )}
                    {owner && <Badge className={styles.wrapBadge} tone="ok">you: {summarizeValue(owner.value, flagField)}</Badge>}
                  </div>
                  {(model?.reason ?? marks[0]?.reason) && <p className={styles.reason}>{model?.reason ?? marks[0]?.reason}</p>}
                  {(!p.is_owner || p.flagged > 0 || owner) &&
                    controls({ participantKey: p.key, label: p.label ?? p.key }, owner, `p:${p.key}`, words.yes, words.no)}
                </div>
              );
            })}
          </Panel>
        </div>

        <Panel
          title="Transcript"
          subtitle={data.messages.length < data.messages_total ? `Showing ${data.messages.length.toLocaleString()} of ${data.messages_total.toLocaleString()} turns` : undefined}
          actions={
            <div className={styles.row}>
              <Select aria-label="Speaker" value={speaker} onChange={(e) => setSpeaker(e.target.value)} style={{ maxWidth: 180 }}>
                <option value="">Everyone</option>
                {data.participants.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.label ?? p.key}
                  </option>
                ))}
              </Select>
              <label className={styles.checkLabel}>
                <Switch checked={flaggedOnly} onChange={setFlaggedOnly} label="Flagged only" /> Flagged only
              </label>
            </div>
          }
        >
          <div className={styles.stack}>
            <div className={styles.legend} aria-hidden>
              <span><span className={`${styles.swatch} ${styles.swatchModel}`} />flagged by a classifier</span>
              <span><span className={`${styles.swatch} ${styles.swatchOwner}`} />flagged by you</span>
              <span><span className={`${styles.swatch} ${styles.swatchCleared}`} />cleared by you</span>
            </div>
            {msgs.length === 0 ? (
              <EmptyState compact title={flaggedOnly ? "No flagged turns" : "No turns"} />
            ) : (
              <ol className={styles.turns}>
                {msgs.slice(0, shown).map((m) => (
                  <Turn key={String(m.id)} m={m} flagField={flagField} speaker={m.participant ? (labelOf.get(m.participant) ?? m.participant) : "—"} />
                ))}
              </ol>
            )}
            {msgs.length > shown && (
              <Button size="sm" onClick={() => setShown(shown + SHOW_STEP)}>
                Show {Math.min(SHOW_STEP, msgs.length - shown)} more
              </Button>
            )}
          </div>
        </Panel>
      </div>

      <ValueDialog
        target={editing}
        schema={schema}
        busy={!!busy}
        onClose={() => setEditing(null)}
        onSave={async (v, detail) => {
          if (!editing) return;
          await write(editing, v, "dialog", detail);
          setEditing(null);
        }}
      />
    </Page>
  );
}

function Turn({ m, speaker, flagField }: { m: ClassificationMessage; speaker: string; flagField: string | null }): ReactNode {
  const a = m.annotation;
  const flagged = !!a && isFlagged(a.value, flagField);
  const byOwner = a?.method === "owner";
  const cls = [
    styles.turn,
    flagged && (byOwner ? styles.turnFlaggedOwner : styles.turnFlagged),
    a && !flagged && byOwner && styles.turnClearedOwner,
    flagged && styles.turnDim,
  ]
    .filter(Boolean)
    .join(" ");
  const who = !a ? null : byOwner ? "you" : a.method === "rule" ? "rule" : "classifier";
  return (
    <li className={cls}>
      <span className={styles.turnTime}>{m.offset_s !== null ? formatOffset(m.offset_s) : <RelativeTime value={m.at} />}</span>
      <div style={{ minWidth: 0 }}>
        <div className={styles.turnHead}>
          <Sensitive className={styles.turnSpeaker}>{speaker}</Sensitive>
          {a && (
            <Badge tone={flagged ? (byOwner ? "neutral" : "warn") : "ok"}>
              {flagged ? "flagged" : "cleared"} · {who}
            </Badge>
          )}
          {a?.job && !byOwner && <span className={styles.mono}>{a.job}</span>}
          {flagged && <span className="sr-only">This turn is flagged {byOwner ? "by you" : "by a classifier"}.</span>}
        </div>
        <Sensitive as="div">
          <p className={styles.turnText}>{m.text ?? ""}</p>
        </Sensitive>
      </div>
    </li>
  );
}

function ValueDialog({
  target,
  schema,
  busy,
  onClose,
  onSave,
}: {
  target: Target | null;
  schema: ValueSchema;
  busy: boolean;
  onClose: () => void;
  onSave: (v: Value | null, detail?: string) => Promise<void>;
}): ReactNode {
  const [value, setValue] = useState<Value>(() => defaultValue(schema));
  const [note, setNote] = useState("");
  const errs = validateValue(schema, value);
  return (
    <Dialog
      open={!!target}
      onClose={onClose}
      title={target ? `Correct ${target.label}` : ""}
      description="Your verdict wins over every classifier and is copied onto the turns."
      size="sm"
      footer={
        <>
          <Button variant="ghost" disabled={busy} onClick={() => void onSave(null, note.trim() || undefined)}>
            Mark as nothing
          </Button>
          <Button variant="primary" disabled={busy || Object.keys(errs).length > 0} onClick={() => void onSave(value, note.trim() || undefined)}>
            {busy ? "Saving…" : "Save correction"}
          </Button>
        </>
      }
    >
      <div className={styles.stack}>
        <ValueEditor schema={schema} value={value} onChange={setValue} idPrefix="corr" />
        <Field label="Why" htmlFor="corr-note" hint="Optional; kept as the correction's reason">
          <Input id="corr-note" value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </div>
    </Dialog>
  );
}
