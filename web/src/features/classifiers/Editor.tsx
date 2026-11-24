/*
 * Create or edit a classifier. Two doors: "Ask each message" (a form: a model
 * answers one question per arriving message) and "Write code" (one python
 * function over a whole conversation). Both validate live against /check
 * (read-only), can be drafted by the model from a plain goal, and can be tried
 * on a sample before saving.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  CircleCheck,
  CircleAlert,
  Code2,
  LoaderCircle,
  MessageSquareText,
  Plus,
  Trash2,
} from "lucide-react";
import {
  Badge,
  Button,
  CodeBlock,
  ErrorState,
  Field,
  IconButton,
  Input,
  Page,
  PageHeader,
  Panel,
  Segmented,
  Select,
  Skeleton,
  toast,
} from "@/ui";
import { useBootstrap } from "@/api";
import {
  defineClassifier,
  useAnnotationAttributes,
  useCheck,
  useClassifier,
  useClassifierGuide,
  useInvalidateClassifiers,
  type AnnotationAttribute,
  type ClassifierGuide,
  type ClassifierKind,
  type DraftResult,
  type ValueSchema,
} from "@/api/classifiers";
import {
  attributeProblems,
  checkMessages,
  defaultValue,
  definitionToForm,
  emptyField,
  emptyForm,
  formProblems,
  formSchema,
  formToDefinition,
  validateValue,
  type BackfillMode,
  type ClassifierForm,
  type FieldDraft,
  type FieldType,
} from "./model";
import { CodeEditor, type CodeEditorHandle } from "./CodeEditor";
import { DraftPanel, NumberField, TryPanel } from "./TryPanel";
import { ChannelPicker, ValueEditor, errorText, isMissingRoute } from "./shared";
import styles from "./Classifiers.module.css";

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function ClassifierEditor({ name }: { name?: string }): ReactNode {
  const editing = !!name;
  const navigate = useNavigate();
  const invalidate = useInvalidateClassifiers();
  const { data: boot } = useBootstrap();
  const detail = useClassifier(name);
  const guide = useClassifierGuide();
  const attrs = useAnnotationAttributes();
  const [form, setForm] = useState<ClassifierForm>(() => emptyForm("message"));
  const [loaded, setLoaded] = useState(!editing);
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const codeRef = useRef<CodeEditorHandle | null>(null);

  // Editing: load the saved definition once.
  useEffect(() => {
    if (!editing || loaded || !detail.data) return;
    const d = detail.data;
    setForm(
      definitionToForm({
        name: d.name,
        kind: d.kind,
        description: d.description ?? "",
        attribute: d.attribute,
        channels: d.channels,
        spec: d.spec,
        code: d.code ?? "",
        monthly_cap_usd: d.monthly_cap_usd ?? undefined,
      }),
    );
    setLoaded(true);
  }, [editing, loaded, detail.data]);

  // New python classifier: start from the first guide example until the owner types.
  useEffect(() => {
    if (editing || touched || form.kind !== "python" || form.code || !guide.data) return;
    const ex = guide.data.examples.python[0];
    if (ex) setForm((f) => loadPythonExample(f, ex));
  }, [editing, touched, form.kind, form.code, guide.data]);

  const attributes = attrs.data?.attributes ?? [];
  const picked = attributes.find((a) => a.name === form.attribute) ?? null;
  const schema = formSchema(form, picked?.value_schema);
  const problems = formProblems(form, schema);
  const definition = useMemo(() => formToDefinition(form), [form]);
  const debounced = useDebounced(definition, 700);
  // Only check once there is something to check: a name and an attribute.
  const checkable = debounced.name !== "" && (typeof debounced.attribute !== "string" || debounced.attribute !== "");
  const check = useCheck(checkable ? debounced : null);
  const checkStale = JSON.stringify(debounced) !== JSON.stringify(definition) || check.isFetching;
  const messages = checkMessages(check.data);
  const serverErrors = check.data && !check.data.ok ? check.data.errors.length : 0;

  const update = (patch: Partial<ClassifierForm>) => {
    setTouched(true);
    setForm((f) => ({ ...f, ...patch }));
  };

  const setKind = (kind: ClassifierKind) => {
    if (kind === form.kind) return;
    setForm((f) => ({ ...emptyForm(kind), name: f.name, description: f.description, channels: f.channels, attrMode: f.attrMode, attribute: f.attribute, newAttr: f.newAttr, monthly_cap_usd: f.monthly_cap_usd }));
    setTouched(false);
  };

  const pickAttribute = (attr: AnnotationAttribute | null, nameValue: string) => {
    setTouched(true);
    setForm((f) => {
      const next = { ...f, attribute: nameValue };
      const s = attr?.value_schema ?? {};
      if (Object.keys(validateValue(s, f.value)).length) next.value = defaultValue(s);
      return next;
    });
  };

  const onDraft = (d: DraftResult) => {
    setTouched(true);
    setForm((f) => {
      const next = definitionToForm(d.definition, f);
      if (editing) {
        next.name = f.name;
        next.kind = f.kind;
      }
      return next;
    });
    toast.ok("Draft loaded", "Review every field before saving.");
  };

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const res = await defineClassifier(definition);
      await invalidate();
      toast.ok(
        res.status === "proposed" ? `Saved ${res.classifier.name} as proposed` : `Saved ${res.classifier.name}`,
        res.status === "active" ? "It is active and runs on its next tick." : `Status: ${res.status}`,
      );
      navigate(`/classifiers/${encodeURIComponent(res.classifier.name)}`);
    } catch (e) {
      setSaveError(errorText(e));
    } finally {
      setSaving(false);
    }
  };

  if (editing && detail.error) {
    return (
      <Page width="wide">
        <ErrorState error={detail.error} title={`Could not load ${name}`} onRetry={() => void detail.refetch()} />
      </Page>
    );
  }
  if (!loaded) {
    return (
      <Page width="wide">
        <Skeleton height={32} />
        <Skeleton height={400} />
      </Page>
    );
  }

  const examples = guide.data?.examples[form.kind] ?? [];
  const firstProblem = Object.values(problems)[0] ?? null;
  const blocked = !!firstProblem || serverErrors > 0;

  return (
    <Page width="wide">
      <PageHeader
        eyebrow={
          <Link to={editing ? `/classifiers/${encodeURIComponent(name)}` : "/classifiers"} className={styles.back}>
            <ArrowLeft size={13} /> {editing ? name : "Classifiers"}
          </Link>
        }
        title={editing ? `Edit ${name}` : "New classifier"}
        lede={
          editing
            ? `Saving creates version ${(detail.data?.version ?? 0) + 1}. Earlier marks keep the version that wrote them.`
            : "Pick how it judges. Agents' definitions wait for your approval; yours are active at once."
        }
      />

      {!editing && (
        <div className={styles.modes} role="radiogroup" aria-label="Kind of classifier">
          <ModeCard
            on={form.kind === "message"}
            onClick={() => setKind("message")}
            icon={<MessageSquareText size={20} />}
            title="Ask each message"
            desc="No code. A cheap model answers one question about every arriving message and writes a value when the answer is yes."
          />
          <ModeCard
            on={form.kind === "python"}
            onClick={() => setKind("python")}
            icon={<Code2 size={20} />}
            title="Write code"
            desc="One python function judges a whole conversation, then each participant. It runs sandboxed; verdicts are copied onto messages."
          />
        </div>
      )}

      <div className={styles.editorGrid}>
        <div className={styles.stack}>
          <Panel
            title="Definition"
            actions={
              examples.length > 0 ? (
                <Select
                  aria-label="Start from an example"
                  value=""
                  onChange={(e) => {
                    const i = Number(e.target.value);
                    if (Number.isNaN(i) || !guide.data) return;
                    setTouched(true);
                    setForm((f) => loadExample(f, guide.data as ClassifierGuide, i));
                  }}
                  style={{ maxWidth: 240 }}
                >
                  <option value="">Start from an example…</option>
                  {examples.map((ex, i) => (
                    <option key={i} value={i}>
                      {ex.title}
                    </option>
                  ))}
                </Select>
              ) : undefined
            }
          >
            <div className={styles.formGrid}>
              <Field label="Name" htmlFor="cl-name" hint={editing ? "Fixed once saved" : "lowercase, digits and _"} error={touched || form.name ? problems.name : undefined}>
                <Input
                  id="cl-name"
                  mono
                  value={form.name}
                  disabled={editing}
                  invalid={!!form.name && !!problems.name}
                  placeholder="is_it_spam"
                  onChange={(e) => update({ name: e.target.value })}
                />
              </Field>
              <NumberField
                id="cl-cap"
                label="Monthly cap (USD)"
                hint="Stops spending for the month at this amount"
                step="any"
                min={0}
                value={form.monthly_cap_usd}
                placeholder="default"
                onChange={(v) => update({ monthly_cap_usd: v })}
              />
              <Field label="Description" htmlFor="cl-desc" className={styles.span2}>
                <Input id="cl-desc" value={form.description} onChange={(e) => update({ description: e.target.value })} />
              </Field>
              <div className={styles.span2}>
                <AttributeSection
                  form={form}
                  attributes={attributes}
                  loading={attrs.isLoading}
                  error={attrs.error}
                  problem={problems.attribute}
                  onPick={pickAttribute}
                  onUpdate={update}
                />
              </div>
              <Field label="Channels" className={styles.span2} hint="Which messages or conversations it reads">
                <ChannelPicker channels={boot?.channels ?? []} value={form.channels} onChange={(v) => update({ channels: v })} />
              </Field>
            </div>
          </Panel>

          {form.kind === "message" ? (
            <MessageSection form={form} schema={schema} problems={problems} onUpdate={update} />
          ) : (
            <PythonSection
              form={form}
              problems={problems}
              onUpdate={update}
              codeRef={codeRef}
              diagnostics={messages}
            />
          )}

          <CheckSummary
            enabled={checkable}
            loading={checkStale}
            missing={isMissingRoute(check.error)}
            error={check.error && !isMissingRoute(check.error) ? errorText(check.error) : null}
            messages={messages}
            ok={check.data?.ok}
            grain={check.data?.grain}
            onJump={form.kind === "python" ? (line) => codeRef.current?.jumpTo(line) : undefined}
          />

          <div className={styles.saveBar}>
            {saveError && (
              <span className={styles.errorLine} role="alert">
                {saveError}
              </span>
            )}
            {firstProblem && !saveError && <span className={styles.note}>{firstProblem}</span>}
            <Button variant="ghost" onClick={() => navigate(editing ? `/classifiers/${encodeURIComponent(name)}` : "/classifiers")}>
              Cancel
            </Button>
            <Button variant="primary" disabled={blocked || saving} onClick={() => void save()}>
              {saving ? "Saving…" : editing ? "Save new version" : "Save classifier"}
            </Button>
          </div>
        </div>

        <aside className={styles.side} aria-label="Helpers">
          <DraftPanel
            kind={form.kind}
            channels={form.channels}
            attribute={form.attrMode === "existing" ? form.attribute : null}
            onDraft={onDraft}
          />
          {form.kind === "python" && <ContractPanel guide={guide.data} loading={guide.isLoading} error={guide.error} />}
          <TryPanel
            kind={form.kind}
            definition={definition}
            disabledReason={firstProblem ? `Fix the form first: ${firstProblem}` : serverErrors ? "Fix the check errors first." : null}
          />
        </aside>
      </div>
    </Page>
  );
}

function loadPythonExample(f: ClassifierForm, ex: ClassifierGuide["examples"]["python"][number]): ClassifierForm {
  return definitionToForm({ kind: "python", attribute: ex.attribute, channels: ex.channels, code: ex.code }, f);
}

function loadExample(f: ClassifierForm, guide: ClassifierGuide, i: number): ClassifierForm {
  if (f.kind === "python") {
    const ex = guide.examples.python[i];
    return ex ? loadPythonExample(f, ex) : f;
  }
  const ex = guide.examples.message[i];
  return ex ? definitionToForm({ ...ex.definition, kind: "message" }, f) : f;
}

function ModeCard({ on, onClick, icon, title, desc }: { on: boolean; onClick: () => void; icon: ReactNode; title: string; desc: string }): ReactNode {
  return (
    <button type="button" role="radio" aria-checked={on} className={`${styles.mode} ${on ? styles.modeOn : ""}`} onClick={onClick}>
      <span className={styles.modeIcon} aria-hidden>
        {icon}
      </span>
      <span>
        <span className={styles.modeTitle}>{title}</span>
        <span className={styles.modeDesc} style={{ display: "block" }}>
          {desc}
        </span>
      </span>
    </button>
  );
}

/* ---- attribute ---------------------------------------------------------------- */

const FIELD_TYPES: FieldType[] = ["boolean", "string", "integer", "number"];

function AttributeSection({
  form,
  attributes,
  loading,
  error,
  problem,
  onPick,
  onUpdate,
}: {
  form: ClassifierForm;
  attributes: AnnotationAttribute[];
  loading: boolean;
  error: unknown;
  problem?: string;
  onPick: (a: AnnotationAttribute | null, name: string) => void;
  onUpdate: (p: Partial<ClassifierForm>) => void;
}): ReactNode {
  const picked = attributes.find((a) => a.name === form.attribute) ?? null;
  const na = form.newAttr;
  const aErrs = form.attrMode === "new" ? attributeProblems(na) : {};
  const setNA = (p: Partial<ClassifierForm["newAttr"]>) => onUpdate({ newAttr: { ...na, ...p } });
  const setFieldDraft = (i: number, p: Partial<FieldDraft>) =>
    setNA({ fields: na.fields.map((f, j) => (j === i ? { ...f, ...p } : f)) });

  return (
    <div className={styles.stackTight}>
      <div className={styles.rowBetween}>
        <span className={styles.sectionHead}>Attribute it writes</span>
        <Segmented<"existing" | "new">
          aria-label="Attribute"
          size="sm"
          value={form.attrMode}
          onChange={(v) => onUpdate({ attrMode: v })}
          items={[
            { value: "existing", label: "Existing" },
            { value: "new", label: "Define new" },
          ]}
        />
      </div>
      {form.attrMode === "existing" ? (
        <>
          {loading ? (
            <Skeleton height={32} />
          ) : error ? (
            <p className={styles.errorLine}>Could not load attributes: {errorText(error)}</p>
          ) : (
            <Select
              aria-label="Attribute"
              value={form.attribute}
              invalid={!!problem}
              onChange={(e) => onPick(attributes.find((a) => a.name === e.target.value) ?? null, e.target.value)}
            >
              <option value="">Choose an attribute…</option>
              {attributes.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                  {a.description ? ` — ${a.description}` : ""}
                </option>
              ))}
              {form.attribute && !picked && <option value={form.attribute}>{form.attribute} (not on this server)</option>}
            </Select>
          )}
          {picked && (
            <div className={styles.row}>
              <Badge>{picked.origin}</Badge>
              {picked.recall_effect === "exclude" && <Badge tone="warn">excluded from recall</Badge>}
              <span className={styles.note}>
                fields:{" "}
                {Object.entries(picked.value_schema)
                  .map(([k, s]) => `${k}: ${s.type}${s.required ? "" : "?"}`)
                  .join(", ") || "none"}{" "}
                · {picked.active_annotations.toLocaleString()} active annotations
              </span>
            </div>
          )}
        </>
      ) : (
        <div className={styles.formGrid}>
          <Field label="Name" htmlFor="na-name" error={na.name ? aErrs.name : undefined}>
            <Input id="na-name" mono value={na.name} invalid={!!na.name && !!aErrs.name} onChange={(e) => setNA({ name: e.target.value })} />
          </Field>
          <Field label="In recall" htmlFor="na-recall" hint="Exclude hides marked messages from recall">
            <Select id="na-recall" value={na.recall_effect} onChange={(e) => setNA({ recall_effect: e.target.value as "none" | "exclude" })}>
              <option value="none">No effect</option>
              <option value="exclude">Exclude marked messages</option>
            </Select>
          </Field>
          <Field label="Description" htmlFor="na-desc" className={styles.span2}>
            <Input id="na-desc" value={na.description} onChange={(e) => setNA({ description: e.target.value })} />
          </Field>
          <Field label="Flag field" htmlFor="na-flag" hint="The yes/no field that means “flagged” (shown as a flag, used by quick corrections)" className={styles.span2}>
            <Select id="na-flag" value={na.flag} onChange={(e) => setNA({ flag: e.target.value })}>
              <option value="">None</option>
              {na.fields
                .filter((f) => f.type === "boolean" && f.name.trim())
                .map((f) => (
                  <option key={f.name} value={f.name.trim()}>
                    {f.name.trim()}
                  </option>
                ))}
            </Select>
          </Field>
          <div className={`${styles.span2} ${styles.attrFields}`}>
            <span className={styles.note}>Value fields</span>
            {na.fields.map((f, i) => (
              <div key={i}>
                <div className={styles.attrField}>
                  <Input aria-label={`Field ${i + 1} name`} mono placeholder="field_name" value={f.name} onChange={(e) => setFieldDraft(i, { name: e.target.value })} />
                  <Select aria-label={`Field ${i + 1} type`} value={f.type} onChange={(e) => setFieldDraft(i, { type: e.target.value as FieldType })}>
                    {FIELD_TYPES.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </Select>
                  <label className={styles.checkLabel}>
                    <input type="checkbox" checked={f.required} onChange={(e) => setFieldDraft(i, { required: e.target.checked })} /> required
                  </label>
                  {f.type === "boolean" ? (
                    <span />
                  ) : (
                    <Input
                      aria-label={`Field ${i + 1} choices`}
                      placeholder="choices, comma-separated (optional)"
                      value={f.values}
                      onChange={(e) => setFieldDraft(i, { values: e.target.value })}
                    />
                  )}
                  <IconButton label={`Remove field ${i + 1}`} size="sm" onClick={() => setNA({ fields: na.fields.filter((_, j) => j !== i) })}>
                    <Trash2 />
                  </IconButton>
                </div>
                {(f.type === "integer" || f.type === "number") && !f.values.trim() && (
                  <div className={styles.row} style={{ marginTop: 4 }}>
                    <Input aria-label={`Field ${i + 1} minimum`} mono type="number" placeholder="min" value={f.min} onChange={(e) => setFieldDraft(i, { min: e.target.value })} style={{ maxWidth: 100 }} />
                    <Input aria-label={`Field ${i + 1} maximum`} mono type="number" placeholder="max" value={f.max} onChange={(e) => setFieldDraft(i, { max: e.target.value })} style={{ maxWidth: 100 }} />
                  </div>
                )}
                {aErrs[`field.${i}`] && f.name && <span className={styles.errorLine}>{aErrs[`field.${i}`]}</span>}
              </div>
            ))}
            {aErrs.fields && <span className={styles.errorLine}>{aErrs.fields}</span>}
            <div>
              <Button size="sm" variant="ghost" icon={<Plus size={13} />} onClick={() => setNA({ fields: [...na.fields, { ...emptyField(), required: false }] })}>
                Add field
              </Button>
            </div>
          </div>
        </div>
      )}
      {problem && (form.attrMode === "new" ? !!na.name : true) && <span className={styles.errorLine}>{problem}</span>}
    </div>
  );
}

/* ---- message kind ------------------------------------------------------------- */

function MessageSection({
  form,
  schema,
  problems,
  onUpdate,
}: {
  form: ClassifierForm;
  schema: ValueSchema;
  problems: Record<string, string>;
  onUpdate: (p: Partial<ClassifierForm>) => void;
}): ReactNode {
  const choice = form.worker === "jev" && form.answer === "choice";
  const setOption = (i: number, text: string) => {
    const old = form.options[i] ?? "";
    const options = form.options.map((o, j) => (j === i ? text : o));
    const values = { ...form.values };
    if (old in values) {
      values[text] = values[old] as Record<string, unknown>;
      delete values[old];
    }
    onUpdate({ options, values });
  };
  return (
    <Panel title="The question" subtitle="Asked once per arriving message on the chosen channels">
      <div className={styles.formGrid}>
        <Field label="Who answers" className={styles.span2} hint={form.worker === "jev" ? "Jev: a cheap structured-decision model" : "The configured LLM: slower, costlier, better at nuance"}>
          <Segmented<"jev" | "llm">
            aria-label="Who answers"
            value={form.worker}
            onChange={(v) => onUpdate({ worker: v })}
            items={[
              { value: "jev", label: "Jev" },
              { value: "llm", label: "LLM" },
            ]}
          />
        </Field>
        <Field label="Question" htmlFor="m-q" className={styles.span2} error={problems.question && form.question ? problems.question : undefined}>
          <textarea
            id="m-q"
            className={styles.textarea}
            rows={3}
            value={form.question}
            placeholder="Is this message asking me to do something?"
            onChange={(e) => onUpdate({ question: e.target.value })}
          />
        </Field>
        {form.worker === "jev" && (
          <Field label="Answer" className={styles.span2}>
            <Segmented<"yes_no" | "choice">
              aria-label="Answer type"
              value={form.answer}
              onChange={(v) => onUpdate({ answer: v })}
              items={[
                { value: "yes_no", label: "Yes / no" },
                { value: "choice", label: "Choose one" },
              ]}
            />
          </Field>
        )}
        {!choice && (
          <>
            <Field label="Confidence threshold" htmlFor="m-th" hint="Write only when at least this sure" className={styles.span2}>
              <div className={styles.rangeRow}>
                <input
                  id="m-th"
                  type="range"
                  className={styles.range}
                  min={0}
                  max={1}
                  step={0.05}
                  value={form.threshold}
                  onChange={(e) => onUpdate({ threshold: Number(e.target.value) })}
                />
                <span className={styles.rangeVal}>{form.threshold.toFixed(2)}</span>
              </div>
            </Field>
            <Field label="Value written on yes" className={styles.span2} error={problems.value && Object.keys(schema).length ? problems.value : undefined}>
              <ValueEditor schema={schema} value={form.value} onChange={(v) => onUpdate({ value: v })} idPrefix="m-val" />
            </Field>
          </>
        )}
        {choice && (
          <div className={`${styles.span2} ${styles.stackTight}`}>
            <span className={styles.sectionHead}>Options and the value each writes</span>
            <p className={styles.note}>Leave an option's value empty to write nothing for it.</p>
            {form.options.map((o, i) => (
              <div key={i} className={styles.optionRow}>
                <Input aria-label={`Option ${i + 1}`} value={o} onChange={(e) => setOption(i, e.target.value)} />
                <ValueEditor
                  schema={schema}
                  value={form.values[o] ?? {}}
                  showErrors={!!form.values[o] && Object.keys(form.values[o] ?? {}).length > 0}
                  onChange={(v) => onUpdate({ values: { ...form.values, [o]: v } })}
                  idPrefix={`m-opt-${i}`}
                />
                <IconButton
                  label={`Remove option ${i + 1}`}
                  size="sm"
                  onClick={() => {
                    const values = { ...form.values };
                    delete values[o];
                    onUpdate({ options: form.options.filter((_, j) => j !== i), values });
                  }}
                >
                  <Trash2 />
                </IconButton>
              </div>
            ))}
            {problems.options && <span className={styles.errorLine}>{problems.options}</span>}
            <div>
              <Button size="sm" variant="ghost" icon={<Plus size={13} />} onClick={() => onUpdate({ options: [...form.options, ""] })}>
                Add option
              </Button>
            </div>
          </div>
        )}
        <NumberField
          id="m-ctx"
          label="Context messages"
          hint="Earlier messages in the thread shown with each one (0–20)"
          min={0}
          max={20}
          step={1}
          value={form.context_messages}
          error={problems.context_messages}
          onChange={(v) => onUpdate({ context_messages: v })}
        />
        <NumberField id="m-min" label="Minimum length" hint="Skip messages shorter than this many characters" min={0} step={1} value={form.min_chars} placeholder="none" onChange={(v) => onUpdate({ min_chars: v })} />
        <NumberField id="m-max" label="Max messages per run" min={1} step={1} value={form.max_per_run} placeholder="default" onChange={(v) => onUpdate({ max_per_run: v })} />
        <NumberField id="m-cost" label="Max cost per run (USD)" min={0} step="any" value={form.max_cost_per_run} placeholder="default" onChange={(v) => onUpdate({ max_cost_per_run: v })} />
        <Field label="Backfill" className={styles.span2} hint="Also judge messages that already arrived" error={problems.backfill}>
          <div className={styles.row}>
            <Segmented<BackfillMode>
              aria-label="Backfill"
              size="sm"
              value={form.backfill}
              onChange={(v) => onUpdate({ backfill: v })}
              items={[
                { value: "none", label: "New only" },
                { value: "days", label: "Recent days" },
                { value: "all", label: "Everything" },
                { value: "since", label: "Since…" },
              ]}
            />
            {form.backfill === "days" && (
              <Input aria-label="Backfill days" type="number" min={1} step={1} mono value={form.backfillDays} onChange={(e) => onUpdate({ backfillDays: e.target.value })} style={{ maxWidth: 100 }} />
            )}
            {form.backfill === "since" && (
              <Input aria-label="Backfill since" type="date" mono value={form.backfillSince} onChange={(e) => onUpdate({ backfillSince: e.target.value })} style={{ maxWidth: 180 }} />
            )}
          </div>
        </Field>
      </div>
    </Panel>
  );
}

/* ---- python kind -------------------------------------------------------------- */

function PythonSection({
  form,
  problems,
  onUpdate,
  codeRef,
  diagnostics,
}: {
  form: ClassifierForm;
  problems: Record<string, string>;
  onUpdate: (p: Partial<ClassifierForm>) => void;
  codeRef: React.RefObject<CodeEditorHandle | null>;
  diagnostics: ReturnType<typeof checkMessages>;
}): ReactNode {
  return (
    <Panel title="Code" subtitle="One function over a whole conversation. Checked as you type; problems appear on their line.">
      <div className={styles.stack}>
        <div className={styles.codeWrap}>
          <CodeEditor
            ref={codeRef}
            ariaLabel="Classifier code"
            value={form.code}
            onChange={(code) => onUpdate({ code })}
            diagnostics={diagnostics}
          />
        </div>
        {problems.code && <span className={styles.errorLine}>{problems.code}</span>}
        <div className={styles.formGrid}>
          <NumberField id="p-settle" label="Settle minutes" hint="Wait this long after a conversation's last message" min={0} step={1} value={form.settle_minutes} placeholder="default" onChange={(v) => onUpdate({ settle_minutes: v })} />
          <NumberField id="p-minmsg" label="Minimum messages" hint="Skip shorter conversations" min={0} step={1} value={form.min_messages} placeholder="default" onChange={(v) => onUpdate({ min_messages: v })} />
          <Field label="Since" htmlFor="p-since" hint="Only conversations that started after this date">
            <Input id="p-since" type="date" mono value={form.since.slice(0, 10)} onChange={(e) => onUpdate({ since: e.target.value })} />
          </Field>
          <NumberField id="p-max" label="Max threads per run" min={1} step={1} value={form.max_threads_per_run} placeholder="default" onChange={(v) => onUpdate({ max_threads_per_run: v })} />
          <NumberField id="p-cost" label="Max cost per run (USD)" min={0} step="any" value={form.max_cost_per_run} placeholder="default" onChange={(v) => onUpdate({ max_cost_per_run: v })} />
        </div>
      </div>
    </Panel>
  );
}

function ContractPanel({ guide, loading, error }: { guide: ClassifierGuide | undefined; loading: boolean; error: unknown }): ReactNode {
  return (
    <Panel title="The contract" subtitle="What the function receives and must return">
      {loading ? (
        <Skeleton lines={4} />
      ) : error ? (
        <p className={styles.note}>{isMissingRoute(error) ? "This server has no classifier guide yet." : errorText(error)}</p>
      ) : guide ? (
        <details className={styles.contract} open>
          <summary>Show the contract</summary>
          <div className={styles.contractBody}>
            <CodeBlock code={guide.python.contract} lang="python" wrap />
            {guide.python.allowed_imports.length > 0 && (
              <div className={styles.stackTight}>
                <span className={styles.sectionHead}>Allowed imports</span>
                <div className={styles.row}>
                  {guide.python.allowed_imports.map((m) => (
                    <code key={m} className={styles.inlineCode}>
                      {m}
                    </code>
                  ))}
                </div>
              </div>
            )}
            {(guide.python.not_available?.length ?? 0) > 0 && (
              <div className={styles.stackTight}>
                <span className={styles.sectionHead}>Not available</span>
                <p className={styles.note}>{guide.python.not_available?.join(", ")}</p>
              </div>
            )}
            {guide.python.rules.length > 0 && (
              <div className={styles.stackTight}>
                <span className={styles.sectionHead}>Rules</span>
                <ul className={styles.rules}>
                  {guide.python.rules.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </details>
      ) : null}
    </Panel>
  );
}

function CheckSummary({
  enabled,
  loading,
  missing,
  error,
  messages,
  ok,
  grain,
  onJump,
}: {
  enabled: boolean;
  loading: boolean;
  missing: boolean;
  error: string | null;
  messages: ReturnType<typeof checkMessages>;
  ok: boolean | undefined;
  grain?: string;
  onJump?: (line: number) => void;
}): ReactNode {
  let state: ReactNode;
  if (!enabled) state = <span className={styles.note}>Checks run once it has a name and an attribute.</span>;
  else if (missing) state = <span className={styles.note}>This server cannot check definitions yet; it will validate on save.</span>;
  else if (error) state = <span className={`${styles.checkState} ${styles.bad}`}><CircleAlert size={14} /> Check failed: {error}</span>;
  else if (ok === undefined || (loading && ok === undefined)) state = <span className={styles.checkState}><LoaderCircle size={14} /> Checking…</span>;
  else if (ok)
    state = (
      <span className={`${styles.checkState} ${styles.ok}`}>
        <CircleCheck size={14} /> Checks pass{grain ? ` · judges per ${grain}` : ""}
        {loading && <span className={styles.muted}> · re-checking…</span>}
      </span>
    );
  else
    state = (
      <span className={`${styles.checkState} ${styles.bad}`}>
        <CircleAlert size={14} /> {messages.filter((m) => m.severity === "error").length} problem(s)
        {loading && <span className={styles.muted}> · re-checking…</span>}
      </span>
    );
  return (
    <div className={styles.stackTight} aria-live="polite">
      {state}
      {enabled && !missing && messages.length > 0 && (
        <ul className={styles.diagList}>
          {messages.map((m, i) => (
            <li key={i} className={`${styles.diag} ${m.severity === "error" ? styles.diagError : styles.diagWarning}`}>
              {m.line !== null && onJump ? (
                <button type="button" className={styles.diagLine} onClick={() => onJump(m.line as number)}>
                  line {m.line}
                </button>
              ) : (
                <span>{m.severity === "error" ? "error" : "warning"}</span>
              )}
              <span>{m.message}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

