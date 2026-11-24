/*
 * The field renderer shared by provider credentials and plugin config. It takes
 * a normalised field list and a string→string value map (booleans are the
 * strings "true"/"false", per the contract), and handles every FieldKind plus:
 *   - secret   → masked input with show/hide + paste
 *   - locked   → read-only "set by the server environment"
 *   - when     → conditional visibility on other fields' values
 *   - advanced → collapsed behind "Show advanced"
 *   - file     → drag-and-drop upload (via the `uploader` callback)
 */
import { useId, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronRight, Eye, EyeOff, FileUp, Lock, UploadCloud, ExternalLink, Check } from "lucide-react";
import { Field, Input, Select, Switch, Button } from "@/ui";
import type { ProviderField, SetupField } from "@/api/setup";
import styles from "./Onboarding.module.css";

export interface NormField {
  key: string;
  label: string;
  kind: string;
  required: boolean;
  help?: string | null;
  help_url?: string | null;
  placeholder?: string | null;
  default?: string | null;
  options?: string[];
  advanced?: boolean;
  when?: Record<string, string[]>;
  locked?: boolean;
}

export function fromProviderField(f: ProviderField, locked: boolean): NormField {
  return {
    key: f.key,
    label: f.label,
    kind: f.kind,
    required: f.required,
    help: f.help,
    help_url: f.help_url,
    placeholder: f.placeholder,
    default: f.default,
    options: f.options,
    locked,
  };
}

export function fromSetupField(f: SetupField): NormField {
  return {
    key: f.key,
    label: f.label,
    kind: f.kind,
    required: f.required,
    help: f.help,
    help_url: f.help_url,
    placeholder: f.placeholder,
    default: f.default,
    options: f.options,
    advanced: f.advanced,
    when: f.when,
  };
}

function visible(field: NormField, values: Record<string, string>): boolean {
  if (!field.when) return true;
  return Object.entries(field.when).every(([k, allowed]) => allowed.includes(values[k] ?? ""));
}

export interface FieldGroupProps {
  fields: NormField[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  /** masked/current values from the server (e.g. "••••last4"), for placeholders */
  masked?: Record<string, string | null>;
  /** upload handler for `file` fields; resolves to the stored path */
  uploader?: (key: string, file: File, onProgress: (f: number) => void) => Promise<string>;
}

export function FieldGroup({ fields, values, onChange, masked, uploader }: FieldGroupProps): ReactNode {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const basic = useMemo(() => fields.filter((f) => !f.advanced), [fields]);
  const advanced = useMemo(() => fields.filter((f) => f.advanced), [fields]);

  const render = (f: NormField): ReactNode =>
    visible(f, values) ? (
      <FieldInput key={f.key} field={f} value={values[f.key] ?? ""} onChange={onChange} masked={masked?.[f.key] ?? null} uploader={uploader} />
    ) : null;

  return (
    <div className={styles.fields}>
      {basic.map(render)}
      {advanced.length > 0 && (
        <>
          <button type="button" className={styles.advancedToggle} onClick={() => setShowAdvanced((v) => !v)} aria-expanded={showAdvanced}>
            <ChevronRight size={14} style={{ transform: showAdvanced ? "rotate(90deg)" : "none", transition: "transform 160ms" }} aria-hidden />
            Advanced options
          </button>
          {showAdvanced && <div className={styles.advancedFields}>{advanced.map(render)}</div>}
        </>
      )}
    </div>
  );
}

function FieldInput({
  field,
  value,
  onChange,
  masked,
  uploader,
}: {
  field: NormField;
  value: string;
  onChange: (key: string, value: string) => void;
  masked: string | null;
  uploader?: (key: string, file: File, onProgress: (f: number) => void) => Promise<string>;
}): ReactNode {
  const id = useId();
  const hint = (
    <>
      {field.help}
      {field.help_url && (
        <>
          {field.help ? " " : null}
          <a href={field.help_url} target="_blank" rel="noreferrer noopener">
            Learn more <ExternalLink size={11} style={{ verticalAlign: "-1px" }} aria-hidden />
          </a>
        </>
      )}
    </>
  );
  const label = (
    <>
      {field.label}
      {!field.required && <span style={{ color: "var(--ink-3)", fontWeight: 400 }}> · optional</span>}
    </>
  );

  if (field.locked) {
    return (
      <Field label={label} hint={field.help}>
        <div className={styles.locked}>
          <Lock size={13} aria-hidden />
          <span>set by the server environment</span>
          {masked && <span className={styles.lockedVal}>{masked}</span>}
        </div>
      </Field>
    );
  }

  switch (field.kind) {
    case "boolean":
      return (
        <Field label={label} inline hint={field.help}>
          <Switch checked={value === "true"} onChange={(c) => onChange(field.key, c ? "true" : "false")} label={field.label} />
        </Field>
      );
    case "select":
      return (
        <Field label={label} htmlFor={id} hint={field.help ? hint : undefined}>
          <Select id={id} value={value || field.default || ""} onChange={(e) => onChange(field.key, e.target.value)}>
            {!field.required && <option value="">—</option>}
            {(field.options ?? []).map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </Select>
        </Field>
      );
    case "secret":
      return <SecretField id={id} field={field} label={label} hint={hint} value={value} masked={masked} onChange={onChange} />;
    case "file":
      return <FileField id={id} field={field} label={label} value={value} onChange={onChange} uploader={uploader} />;
    case "textarea":
    case "list":
      return (
        <Field label={label} htmlFor={id} hint={field.help ? hint : undefined}>
          <textarea
            id={id}
            className={styles.logTail}
            style={{ maxHeight: "none", minHeight: 80, color: "var(--ink)" }}
            rows={4}
            spellCheck={false}
            placeholder={field.placeholder ?? (field.kind === "list" ? "one per line" : undefined) ?? undefined}
            value={value}
            onChange={(e) => onChange(field.key, e.target.value)}
          />
        </Field>
      );
    case "number":
      return (
        <Field label={label} htmlFor={id} hint={field.help ? hint : undefined}>
          <Input id={id} type="number" mono value={value} placeholder={field.placeholder ?? undefined} onChange={(e) => onChange(field.key, e.target.value)} />
        </Field>
      );
    default: {
      const type = field.kind === "email" ? "email" : field.kind === "url" ? "url" : "text";
      const mono = field.kind === "url" || field.kind === "path" || field.kind === "directory";
      return (
        <Field label={label} htmlFor={id} hint={field.help ? hint : undefined}>
          <Input
            id={id}
            type={type}
            mono={mono}
            autoCapitalize="none"
            spellCheck={false}
            placeholder={field.placeholder ?? undefined}
            value={value}
            onChange={(e) => onChange(field.key, e.target.value)}
          />
        </Field>
      );
    }
  }
}

function SecretField({
  id,
  field,
  label,
  hint,
  value,
  masked,
  onChange,
}: {
  id: string;
  field: NormField;
  label: ReactNode;
  hint: ReactNode;
  value: string;
  masked: string | null;
  onChange: (key: string, value: string) => void;
}): ReactNode {
  const [show, setShow] = useState(false);
  return (
    <Field label={label} htmlFor={id} hint={field.help || field.help_url ? hint : undefined}>
      <div className={styles.secretWrap}>
        <Input
          id={id}
          mono
          type={show ? "text" : "password"}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          placeholder={masked ?? field.placeholder ?? "paste your key"}
          value={value}
          onChange={(e) => onChange(field.key, e.target.value)}
        />
        <button
          type="button"
          className={styles.secretToggle}
          onClick={() => setShow((v) => !v)}
          aria-label={show ? "Hide" : "Show"}
          title={show ? "Hide" : "Show"}
        >
          {show ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
      </div>
    </Field>
  );
}

function FileField({
  id,
  field,
  label,
  value,
  onChange,
  uploader,
}: {
  id: string;
  field: NormField;
  label: ReactNode;
  value: string;
  onChange: (key: string, value: string) => void;
  uploader?: (key: string, file: File, onProgress: (f: number) => void) => Promise<string>;
}): ReactNode {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [drag, setDrag] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function take(file: File): Promise<void> {
    setError(null);
    setName(file.name);
    if (!uploader) {
      onChange(field.key, file.name);
      return;
    }
    setProgress(0);
    try {
      const path = await uploader(field.key, file, setProgress);
      onChange(field.key, path);
      setProgress(1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setProgress(null);
    }
  }

  const done = value && progress === 1;

  return (
    <Field label={label} htmlFor={id} hint={field.help ?? undefined}>
      {value && !error ? (
        <div className={styles.dropFile}>
          {done ? <Check size={15} style={{ color: "var(--ok)" }} aria-hidden /> : <FileUp size={15} aria-hidden />}
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-xs)" }}>{name ?? value}</span>
          <span className={styles.footerSpacer} />
          {progress !== null && progress < 1 ? (
            <span className="tnum" style={{ fontSize: "var(--t-xs)", color: "var(--ink-3)" }}>{Math.round(progress * 100)}%</span>
          ) : (
            <Button size="sm" variant="ghost" onClick={() => { onChange(field.key, ""); setName(null); setProgress(null); }}>
              Replace
            </Button>
          )}
        </div>
      ) : (
        <div
          className={styles.dropzone}
          data-drag={drag}
          role="button"
          tabIndex={0}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inputRef.current?.click(); } }}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files?.[0]; if (f) void take(f); }}
        >
          <UploadCloud size={22} className={styles.dropIcon} aria-hidden />
          <span className={styles.dropMain}>Drop your export here, or click to choose</span>
          <span className={styles.dropSub}>{field.placeholder ?? "A file exported from the service"}</span>
          <input
            ref={inputRef}
            id={id}
            type="file"
            style={{ display: "none" }}
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void take(f); }}
          />
        </div>
      )}
      {error && <div style={{ color: "var(--bad)", fontSize: "var(--t-xs)", marginTop: "var(--s-2)" }}>{error}</div>}
    </Field>
  );
}
