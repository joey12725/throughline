/*
 * "Try on a sample" and "Draft with AI": the two editor side panels that spend
 * real model calls. Neither runs on its own; each needs an explicit press, and
 * both say up front that they cost a few cents.
 */
import { useState, type ReactNode } from "react";
import { CircleDollarSign, FlaskConical, Sparkles } from "lucide-react";
import { Badge, Button, Field, Input, Panel, Segmented, Select, Sensitive } from "@/ui";
import {
  draftClassifier,
  tryClassifier,
  type ClassifierDefinition,
  type ClassifierKind,
  type DraftResult,
  type MessageTryItem,
  type PythonTryItem,
  type TryResult,
  type TrySample,
} from "@/api/classifiers";
import { formatUsd, parseIds } from "./model";
import { JudgementView, ValueChip, errorText } from "./shared";
import styles from "./Classifiers.module.css";

/* ---- try ------------------------------------------------------------------- */

export function TryPanel({
  kind,
  definition,
  disabledReason,
}: {
  kind: ClassifierKind;
  definition: ClassifierDefinition;
  /** why trying is not possible yet (e.g. the form has problems) */
  disabledReason?: string | null;
}): ReactNode {
  const [mode, setMode] = useState<"recent" | "ids">("recent");
  const [limit, setLimit] = useState("5");
  const [ids, setIds] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<TryResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const idWord = kind === "python" ? "thread" : "message";

  const sample = (): TrySample | null => {
    if (mode === "recent") return { limit: Math.min(25, Math.max(1, Number(limit) || 5)) };
    const list = parseIds(ids);
    if (!list.length) return null;
    return kind === "python" ? { thread_ids: list } : { message_ids: list };
  };

  const go = async () => {
    const s = sample();
    if (!s) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await tryClassifier({ definition }, s));
    } catch (e) {
      setError(errorText(e));
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Try on a sample" subtitle="Runs the definition as it stands now. Writes nothing.">
      <div className={styles.stack}>
        <Segmented<"recent" | "ids">
          aria-label="Sample"
          size="sm"
          value={mode}
          onChange={setMode}
          items={[
            { value: "recent", label: "Recent" },
            { value: "ids", label: `Specific ${idWord}s` },
          ]}
        />
        {mode === "recent" ? (
          <Field label={`How many ${idWord}s`} htmlFor="try-limit" hint="Up to 25">
            <Select id="try-limit" value={limit} onChange={(e) => setLimit(e.target.value)}>
              {[1, 3, 5, 10, 25].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </Select>
          </Field>
        ) : (
          <Field label={`${idWord[0]?.toUpperCase()}${idWord.slice(1)} ids`} htmlFor="try-ids" hint="One per line or comma-separated">
            <textarea
              id="try-ids"
              className={`${styles.textarea} ${styles.textareaMono}`}
              rows={3}
              value={ids}
              onChange={(e) => setIds(e.target.value)}
            />
          </Field>
        )}
        <p className={styles.spend}>
          <CircleDollarSign size={14} aria-hidden style={{ flex: "none" }} />
          Trying makes real model calls and spends a few cents.
        </p>
        <Button
          variant="primary"
          icon={<FlaskConical size={14} />}
          disabled={busy || !!disabledReason || !sample()}
          onClick={() => void go()}
        >
          {busy ? "Trying…" : "Try it"}
        </Button>
        {disabledReason && <p className={styles.note}>{disabledReason}</p>}
        {error && <p className={styles.errorLine} role="alert">{error}</p>}
        {result && <TryResultView result={result} kind={kind} />}
      </div>
    </Panel>
  );
}

export function TryResultView({ result, kind }: { result: TryResult; kind: ClassifierKind }): ReactNode {
  const proj = result.projection;
  return (
    <div className={styles.stack} aria-live="polite">
      <div className={styles.projection}>
        <span>
          <span className={styles.muted}>calls</span> <span className="tnum">{result.calls}</span>
        </span>
        <span>
          <span className={styles.muted}>cost</span> <span className="tnum">{formatUsd(result.cost)}</span>
        </span>
        <span>
          <span className={styles.muted}>per item</span> <span className="tnum">{formatUsd(result.cost_per_item)}</span>
        </span>
        {proj && (
          <span>
            <span className={styles.muted}>projected</span>{" "}
            <span className="tnum">
              {(proj.threads_to_judge ?? proj.messages_to_judge ?? 0).toLocaleString()} {kind === "python" ? "threads" : "messages"} ≈{" "}
              {formatUsd(proj.estimated_cost)}
            </span>
          </span>
        )}
      </div>
      {!result.ok && <Badge tone="warn">some items failed</Badge>}
      {result.items.length === 0 && <p className={styles.note}>The sample was empty — nothing matched the channels and filters.</p>}
      <ul className={styles.tryItems}>
        {result.items.map((it, i) =>
          kind === "python" ? <PythonTry key={i} item={it as PythonTryItem} /> : <MessageTry key={i} item={it as MessageTryItem} />,
        )}
      </ul>
    </div>
  );
}

function PythonTry({ item }: { item: PythonTryItem }): ReactNode {
  return (
    <li className={styles.tryItem}>
      <div className={styles.rowBetween}>
        <Sensitive className={styles.tryTitle}>{item.title || `Thread ${item.thread_id}`}</Sensitive>
        <span className={styles.mono}>#{item.thread_id}</span>
      </div>
      <JudgementView
        status={item.status}
        verdict={item.verdict}
        marks={item.marks}
        participants={item.participants}
        wouldCover={item.would_cover}
        logs={item.logs}
        error={item.error}
      />
    </li>
  );
}

function MessageTry({ item }: { item: MessageTryItem }): ReactNode {
  const tone = item.status === "done" ? (item.value ? "warn" : "neutral") : item.status === "error" ? "bad" : "neutral";
  return (
    <li className={styles.tryItem}>
      <div className={styles.rowBetween}>
        <span className={styles.row}>
          <span className={styles.mono}>{item.channel}</span>
          {item.author && <Sensitive className={styles.note}>{item.author}</Sensitive>}
        </span>
        <span className={styles.mono}>#{item.message_id}</span>
      </div>
      {item.content && (
        <Sensitive as="div">
          <p className={styles.excerpt}>{item.content}</p>
        </Sensitive>
      )}
      <div className={styles.row}>
        <Badge tone={tone}>{item.status === "done" ? (item.value ? "marked" : "not marked") : item.status}</Badge>
        {item.value && <ValueChip value={item.value} />}
        {item.confidence !== null && item.confidence !== undefined && (
          <span className={styles.note}>{Math.round(item.confidence * 100)}% confident</span>
        )}
      </div>
      {item.reason && <p className={styles.reason}>{item.reason}</p>}
      {item.error && <p className={styles.errorLine}>{item.error}</p>}
    </li>
  );
}

/* ---- draft with AI ----------------------------------------------------------- */

export function DraftPanel({
  kind,
  channels,
  attribute,
  onDraft,
}: {
  kind: ClassifierKind;
  channels: string[];
  attribute: string | null;
  onDraft: (d: DraftResult) => void;
}): ReactNode {
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<string[] | null>(null);
  const [model, setModel] = useState<string | null>(null);

  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      const d = await draftClassifier({
        goal: goal.trim(),
        kind,
        channels: channels.length ? channels : undefined,
        attribute: attribute || undefined,
      });
      onDraft(d);
      setNotes(d.notes ? (Array.isArray(d.notes) ? d.notes : [d.notes]) : null);
      setModel(d.model);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Draft with AI" subtitle="Describe what to catch; the model fills the form for you to review.">
      <div className={styles.stack}>
        <Field label="Goal" htmlFor="draft-goal">
          <textarea
            id="draft-goal"
            className={styles.textarea}
            rows={3}
            value={goal}
            placeholder={
              kind === "python"
                ? "Flag recordings where a TV or music is playing in the background…"
                : "Mark messages where someone asks me to do something by a date…"
            }
            onChange={(e) => setGoal(e.target.value)}
          />
        </Field>
        <p className={styles.spend}>
          <CircleDollarSign size={14} aria-hidden style={{ flex: "none" }} />
          One model call, usually under a cent. It replaces what is in the form.
        </p>
        <Button icon={<Sparkles size={14} />} disabled={busy || goal.trim().length < 8} onClick={() => void go()}>
          {busy ? "Drafting…" : "Draft it"}
        </Button>
        {error && <p className={styles.errorLine} role="alert">{error}</p>}
        {notes && notes.length > 0 && (
          <div className={styles.stackTight}>
            <span className={styles.sectionHead}>Notes from the draft{model ? ` · ${model}` : ""}</span>
            <ul className={styles.rules}>
              {notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </div>
        )}
        {!notes && model && <p className={styles.note}>Drafted by {model}. Review before saving.</p>}
      </div>
    </Panel>
  );
}

/** A compact numeric field, used for the run limits. */
export function NumberField({
  id,
  label,
  hint,
  value,
  onChange,
  error,
  step,
  min,
  max,
  placeholder,
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  value: string;
  onChange: (v: string) => void;
  error?: string;
  step?: number | "any";
  min?: number;
  max?: number;
  placeholder?: string;
}): ReactNode {
  return (
    <Field label={label} htmlFor={id} hint={hint} error={error}>
      <Input
        id={id}
        type="number"
        mono
        step={step}
        min={min}
        max={max}
        value={value}
        placeholder={placeholder}
        invalid={!!error}
        onChange={(e) => onChange(e.target.value)}
      />
    </Field>
  );
}
