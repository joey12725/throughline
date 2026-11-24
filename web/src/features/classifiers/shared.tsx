/*
 * Small pieces shared by the classifier screens: badges, the schema-driven value
 * editor, the channel picker, and the readable rendering of one python verdict
 * (verdict chip, which participants were marked, coverage, logs).
 */
import { useMemo, useState, type ReactNode } from "react";
import { Code2, MessageSquareText } from "lucide-react";
import { Badge, Chip, Input, Select, Segmented, SourceDot, Sensitive, StaticChip } from "@/ui";
import { ApiError, type ChannelRow } from "@/api";
import type {
  ClassifierKind,
  JudgedParticipant,
  Mark,
  ValueSchema,
} from "@/api/classifiers";
import {
  fieldInputKind,
  parseFieldInput,
  setField,
  summarizeDetail,
  summarizeValue,
  statusTone,
  validateValue,
  verdictTone,
  logsText,
  type Value,
} from "./model";
import styles from "./Classifiers.module.css";

export function KindBadge({ kind }: { kind: ClassifierKind }): ReactNode {
  return (
    <Badge>
      {kind === "python" ? <Code2 size={12} aria-hidden /> : <MessageSquareText size={12} aria-hidden />}
      {kind === "python" ? "code" : "per message"}
    </Badge>
  );
}

export function StatusBadge({ status }: { status: string }): ReactNode {
  return <Badge tone={statusTone(status)}>{status}</Badge>;
}

export function VerdictBadge({ verdict }: { verdict: string | null | undefined }): ReactNode {
  if (!verdict) return <Badge>no verdict</Badge>;
  return <Badge tone={verdictTone(verdict)}>{verdict}</Badge>;
}

export function ValueChip({ value }: { value: Value | null | undefined }): ReactNode {
  return <Badge className={styles.wrapBadge}>{summarizeValue(value)}</Badge>;
}

/** A friendly line for an error, with the 404 "not on this server" case named. */
export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 404) return "This server does not have that route yet.";
    if (err.status === 503) return `The model is unreachable right now (${err.message}). Try again in a moment.`;
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

export const isMissingRoute = (err: unknown): boolean => err instanceof ApiError && err.status === 404;

/* ---- value editor ---------------------------------------------------------- */

export interface ValueEditorProps {
  schema: ValueSchema;
  value: Value;
  onChange: (v: Value) => void;
  idPrefix: string;
  /** show "required"/problem lines (default true) */
  showErrors?: boolean;
}

/** One control per field of an attribute's value_schema. */
export function ValueEditor({ schema, value, onChange, idPrefix, showErrors = true }: ValueEditorProps): ReactNode {
  const errs = showErrors ? validateValue(schema, value) : {};
  const entries = Object.entries(schema);
  if (entries.length === 0) {
    return <p className={styles.note}>This attribute has no value fields; the annotation itself is the mark.</p>;
  }
  return (
    <div className={styles.valueEditor}>
      {entries.map(([name, spec]) => {
        const id = `${idPrefix}-${name}`;
        const kind = fieldInputKind(spec);
        const cur = value[name];
        let control: ReactNode;
        if (kind === "boolean") {
          control = (
            <Segmented<"true" | "false" | "">
              aria-label={name}
              size="sm"
              value={cur === true ? "true" : cur === false ? "false" : ""}
              onChange={(v) => onChange(setField(value, name, v === "" ? undefined : v === "true"))}
              items={[
                { value: "true", label: "true" },
                { value: "false", label: "false" },
                ...(spec.required ? [] : [{ value: "" as const, label: "unset" }]),
              ]}
            />
          );
        } else if (kind === "enum") {
          control = (
            <Select
              id={id}
              value={cur === undefined || cur === null ? "" : String(cur)}
              invalid={!!errs[name]}
              onChange={(e) => onChange(setField(value, name, parseFieldInput(spec, e.target.value)))}
            >
              <option value="">{spec.required ? "Choose…" : "(unset)"}</option>
              {(spec.values ?? []).map((o) => (
                <option key={String(o)} value={String(o)}>
                  {String(o)}
                </option>
              ))}
            </Select>
          );
        } else {
          control = (
            <Input
              id={id}
              mono={kind !== "text"}
              type={kind === "text" ? "text" : "number"}
              step={kind === "integer" ? 1 : "any"}
              min={spec.min}
              max={spec.max}
              invalid={!!errs[name]}
              value={cur === undefined || cur === null ? "" : String(cur)}
              onChange={(e) => onChange(setField(value, name, parseFieldInput(spec, e.target.value)))}
            />
          );
        }
        return (
          <div key={name} className={styles.valueRow}>
            <label className={styles.valueName} htmlFor={kind === "boolean" ? undefined : id}>
              {name}
              {spec.required ? "" : " (optional)"}
            </label>
            <div>{control}</div>
            {errs[name] && <div className={styles.valueErr}>{errs[name]}</div>}
          </div>
        );
      })}
      {Object.keys(value)
        .filter((k) => !(k in schema))
        .map((k) => (
          <div key={k} className={styles.valueRow}>
            <span className={styles.valueName}>{k}</span>
            <Chip onRemove={() => onChange(setField(value, k, undefined))}>not in schema · remove</Chip>
          </div>
        ))}
    </div>
  );
}

/* ---- channel picker -------------------------------------------------------- */

export function ChannelPicker({
  channels,
  value,
  onChange,
}: {
  channels: readonly ChannelRow[];
  value: string[];
  onChange: (v: string[]) => void;
}): ReactNode {
  const [filter, setFilter] = useState("");
  const all = value.length === 0;
  const grouped = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const m = new Map<string, ChannelRow[]>();
    for (const c of channels) {
      if (q && !c.name.toLowerCase().includes(q)) continue;
      const list = m.get(c.plugin) ?? [];
      list.push(c);
      m.set(c.plugin, list);
    }
    return Array.from(m.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [channels, filter]);
  const known = new Set(channels.map((c) => c.name));
  const unknown = value.filter((v) => !known.has(v));

  const toggle = (name: string) =>
    onChange(value.includes(name) ? value.filter((v) => v !== name) : [...value, name].sort());

  return (
    <div className={styles.channels}>
      <div className={styles.row}>
        <Chip active={all} onClick={() => onChange([])}>
          All channels
        </Chip>
        {!all && <span className={styles.note}>{value.length} selected</span>}
        {channels.length > 12 && (
          <Input
            aria-label="Filter channels"
            placeholder="Filter channels…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            style={{ maxWidth: 220 }}
          />
        )}
      </div>
      <div className={styles.channelList} role="group" aria-label="Channels">
        {grouped.map(([plugin, list]) => (
          <div key={plugin} style={{ display: "contents" }}>
            <span className={styles.pluginLabel}>{plugin}</span>
            {list.map((c) => (
              <Chip
                key={c.name}
                active={value.includes(c.name)}
                lead={<SourceDot plugin={c.plugin} />}
                onClick={() => toggle(c.name)}
              >
                {c.name}
              </Chip>
            ))}
          </div>
        ))}
        {unknown.map((c) => (
          <Chip key={c} active onClick={() => toggle(c)}>
            {c} (not on this server)
          </Chip>
        ))}
        {grouped.length === 0 && <span className={styles.note}>No channels match.</span>}
      </div>
    </div>
  );
}

/* ---- python verdict -------------------------------------------------------- */

export interface JudgementProps {
  verdict?: string | null;
  marks?: Mark[];
  participants?: JudgedParticipant[];
  wouldCover?: { messages: number; of: number } | null;
  logs?: string | string[] | null;
  error?: string | null;
  status?: string;
}

/** Verdict, thread mark, which participants were marked, coverage and logs. */
export function JudgementView({ verdict, marks = [], participants = [], wouldCover, logs, error, status }: JudgementProps): ReactNode {
  const threadMarks = marks.filter((m) => m.target === "thread");
  const byKey = new Map<string, Mark[]>();
  for (const m of marks) {
    if (m.target !== "participant" || !m.key) continue;
    byKey.set(m.key, [...(byKey.get(m.key) ?? []), m]);
  }
  const messageMarks = marks.filter((m) => m.target === "message").length;
  const text = logsText(logs);
  return (
    <div className={styles.stackTight}>
      <div className={styles.row}>
        {status && status !== "done" && <Badge tone={status === "error" ? "bad" : "neutral"}>{status}</Badge>}
        {status !== "error" && <VerdictBadge verdict={verdict} />}
        {threadMarks.map((m, i) => (
          <StaticChip key={i}>
            whole thread · {m.value ? summarizeValue(m.value) : summarizeDetail(m.detail) || "note"}
          </StaticChip>
        ))}
        {messageMarks > 0 && <StaticChip>{messageMarks} turn marks</StaticChip>}
        {wouldCover && (
          <span className={styles.note}>
            would mark <span className="tnum">{wouldCover.messages}</span> of <span className="tnum">{wouldCover.of}</span> messages
          </span>
        )}
      </div>
      {error && <p className={styles.errorLine}>{error}</p>}
      {participants.length > 0 && (
        <ul className={styles.participants} aria-label="Participants">
          {participants.map((p) => {
            const pm = byKey.get(p.key) ?? [];
            return (
              <li key={p.key} className={styles.participant}>
                <Sensitive>{p.label ?? p.key}</Sensitive>
                {p.is_owner && <Badge>owner</Badge>}
                <span className={styles.muted}>
                  <span className="tnum">{p.turns}</span> turns
                </span>
                {pm.length ? (
                  pm.map((m, i) => (
                    <span key={i} className={styles.row} style={{ gap: 4 }}>
                      <Badge className={styles.wrapBadge} tone="warn">{summarizeValue(m.value)}</Badge>
                      {m.confidence !== null && m.confidence !== undefined && (
                        <span className={styles.muted}>{Math.round(m.confidence * 100)}%</span>
                      )}
                      {m.reason && <span className={styles.reason}>{m.reason}</span>}
                    </span>
                  ))
                ) : (
                  <span className={styles.muted}>not marked</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {text && (
        <details className={styles.logs}>
          <summary>logs</summary>
          <pre>{text}</pre>
        </details>
      )}
    </div>
  );
}
