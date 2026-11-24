/*
 * The helper panel. Shows the configured provider, model and disclosure tier; takes
 * a question (Ask), fixes an error (Fix), or explains/refines the editor selection.
 * Each run shows its live step list (tool, SQL, rows, what was sent, ms, tokens,
 * cost). The product is a (statement, spec) pair: the statement as a diff against the
 * editor with Apply / Apply & run / Copy, and the spec drawn live as a chart. When no
 * model is configured, the panel says so plainly and Ask still works as a finder.
 */
import { forwardRef, useImperativeHandle, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Sparkles, Wrench, BookOpen, Wand2, Send, X, StopCircle, Check, Play, Copy, Cpu, ShieldQuestion, CornerDownLeft,
} from "lucide-react";
import { Button, IconButton, Badge, Segmented, Sensitive, toast } from "@/ui";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChartPanel } from "@/charts";
import type { ViewSpec } from "@/lens";
import type { SqlResult } from "@/api/types";
import { useAssistant, useAssistantConfig } from "./useAssistant";
import type { StartRequest, Tier, RunEvent, StepEvent } from "./assistantClient";
import { diffLines } from "./diff";
import { findMatches, fixHints, type FinderMatch } from "./finder";
import type { Catalogue } from "../catalogue";
import styles from "../query.module.css";

export interface HelperPanelProps {
  statement: string;
  selection: string;
  scope: { from: string | null; to: string | null; tz: string };
  channel?: string | null;
  result: SqlResult | null;
  runError: { code: string | null; message: string } | null;
  catalogue: Catalogue;
  plugin?: string;
  onApply: (statement: string) => void;
  onApplyAndRun: (statement: string) => void;
  onClose: () => void;
}

export interface HelperPanelHandle {
  focusInput: () => void;
}

interface Turn {
  role: "user" | "assistant";
  text: string;
}

function resultShape(r: SqlResult | null): StartRequest["result"] {
  if (!r) return null;
  return {
    columns: r.columns.map((c) => ({ name: c.name, type: c.type })),
    row_count: r.rowCount,
    truncated: r.truncated,
  };
}

function StepList({ events }: { events: RunEvent[] }): ReactNode {
  if (events.length === 0) return null;
  return (
    <ol className={styles.stepList}>
      {events.map((e) => {
        if (e.kind === "text") {
          return (
            <li key={e.seq} className={styles.stepText}>
              <div className={styles.markdown}><ReactMarkdown remarkPlugins={[remarkGfm]}>{(e as { text: string }).text}</ReactMarkdown></div>
            </li>
          );
        }
        if (e.kind !== "step") return null;
        const s = e as StepEvent;
        return (
          <li key={e.seq} className={styles.step}>
            <div className={styles.stepHead}>
              <span className={styles.stepTool}>{s.tool}</span>
              {typeof s.ms === "number" && <span className={`tnum ${styles.stepMs}`}>{s.ms} ms</span>}
              {typeof s.row_count === "number" && <span className={`tnum ${styles.stepRows}`}>{s.row_count.toLocaleString("en-US")} rows</span>}
            </div>
            {s.sql && <code className={styles.stepSql}>{s.sql}</code>}
            {s.summary && <div className={styles.stepSummary}>{s.summary}</div>}
            {s.sent && <div className={styles.stepSent} title="What was sent to the model">sent: {s.sent}</div>}
            {s.error && <div className={styles.stepErr}>{s.error}</div>}
          </li>
        );
      })}
    </ol>
  );
}

function Diff({ before, after }: { before: string; after: string }): ReactNode {
  const lines = useMemo(() => diffLines(before, after), [before, after]);
  return (
    <pre className={styles.diff} aria-label="Proposed statement diff">
      {lines.map((l, i) => (
        <div key={i} className={l.op === "add" ? styles.diffAdd : l.op === "del" ? styles.diffDel : styles.diffSame}>
          <span className={styles.diffGutter}>{l.op === "add" ? "+" : l.op === "del" ? "−" : " "}</span>
          {l.text || " "}
        </div>
      ))}
    </pre>
  );
}

export const HelperPanel = forwardRef<HelperPanelHandle, HelperPanelProps>(function HelperPanel(props, ref) {
  const cfg = useAssistantConfig();
  const { run, running, unavailable, start, cancel } = useAssistant();
  const [tier, setTier] = useState<Tier>(cfg.tier);
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [finder, setFinder] = useState<FinderMatch[] | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const localMode = cfg.isLocal || unavailable;

  useImperativeHandle(ref, () => ({ focusInput: () => inputRef.current?.focus() }), []);

  const baseReq = (): Omit<StartRequest, "verb"> => ({
    statement: props.statement,
    selection: props.selection || undefined,
    result: resultShape(props.result),
    tier,
    scope: props.scope,
    channel: props.channel ?? undefined,
    history: turns.slice(-10),
  });

  const launch = (verb: StartRequest["verb"], question?: string): void => {
    if (localMode) {
      if (verb === "fix") {
        setFinder(null);
        return;
      }
      setFinder(findMatches(props.catalogue, question ?? input));
      if (question ?? input) setTurns((t) => [...t, { role: "user", text: question ?? input }]);
      return;
    }
    const req: StartRequest = { verb, ...baseReq() };
    if (question) {
      req.question = question;
      setTurns((t) => [...t, { role: "user", text: question }]);
    }
    if (verb === "fix" && props.runError) req.error = props.runError;
    void start(req);
  };

  const onAsk = (): void => {
    const q = input.trim();
    if (!q) return;
    launch("ask", q);
    setInput("");
  };

  // Record the assistant's answer as a turn once a run finishes.
  const lastFinal = run?.status === "done" ? run.final?.answer : undefined;
  const recordedRef = useRef<string | null>(null);
  if (lastFinal && recordedRef.current !== run?.run) {
    recordedRef.current = run?.run ?? null;
    queueMicrotask(() => setTurns((t) => (t.some((x) => x.role === "assistant" && x.text === lastFinal) ? t : [...t, { role: "assistant", text: lastFinal }])));
  }

  const proposed = run?.final?.statement;
  const proposedSpec = (run?.final?.spec ?? null) as ViewSpec | null;

  return (
    <aside className={styles.helper} aria-label="Query helper">
      <div className={styles.helperHead}>
        <div className={styles.helperTitle}>
          <Sparkles size={15} aria-hidden /> Helper
        </div>
        <IconButton size="sm" variant="ghost" label="Close helper" onClick={props.onClose}>
          <X size={15} />
        </IconButton>
      </div>

      <div className={styles.helperProvider}>
        <Cpu size={13} aria-hidden />
        {localMode ? (
          <span className={styles.providerLocal}>No model configured — finder mode</span>
        ) : (
          <>
            <span className={styles.providerName}>{cfg.provider}</span>
            {cfg.model && <span className={styles.providerModel}>{cfg.model}</span>}
          </>
        )}
        {!localMode && cfg.tiers.length > 1 ? (
          <Segmented
            aria-label="Disclosure tier"
            size="sm"
            value={tier}
            onChange={(v) => setTier(v as Tier)}
            items={cfg.tiers.map((t) => ({ value: t, label: t }))}
            className={styles.tierSeg}
          />
        ) : (
          <Badge tone="neutral">{tier}</Badge>
        )}
      </div>

      <div className={styles.helperVerbs}>
        <Button size="sm" variant="ghost" icon={<Wrench size={14} />} onClick={() => launch("fix")} disabled={!props.runError}>Fix</Button>
        <Button size="sm" variant="ghost" icon={<BookOpen size={14} />} onClick={() => launch("explain")} disabled={!props.statement}>Explain</Button>
        <Button size="sm" variant="ghost" icon={<Wand2 size={14} />} onClick={() => launch("refine")} disabled={!props.statement}>Refine</Button>
      </div>

      <div className={styles.helperScroll}>
        {localMode && (
          <div className={styles.noModel}>
            <ShieldQuestion size={16} aria-hidden />
            <div>
              <strong>No assistant model is configured.</strong>
              <p>Ask still works as a finder over the catalogue. To enable the agent, set these on the box and restart:</p>
              <code className={styles.envList}>
                THROUGHLINE_ASSISTANT_PROVIDER=anthropic|openai{"\n"}
                THROUGHLINE_ASSISTANT_MODEL=&lt;model id&gt;{"\n"}
                THROUGHLINE_ASSISTANT_API_KEY=&lt;key&gt;{"\n"}
                THROUGHLINE_ASSISTANT_BASE_URL=&lt;endpoint, for openai&gt;
              </code>
            </div>
          </div>
        )}

        {localMode && props.runError && (
          <div className={styles.fixBox}>
            <div className={styles.fixTitle}><Wrench size={13} /> Fix suggestions</div>
            {fixHints(props.runError.code, props.runError.message).map((h, i) => (
              <div key={i} className={styles.fixHint}>
                <strong>{h.title}</strong>
                <span>{h.detail}</span>
              </div>
            ))}
          </div>
        )}

        {localMode && finder && (
          <div className={styles.finder}>
            {finder.length === 0 ? (
              <p className={styles.fieldDesc}>No match. Try a channel, field or function name.</p>
            ) : (
              finder.map((m, i) => (
                <div key={i} className={styles.finderItem}>
                  <div className={styles.finderMain}>
                    <span className={styles.finderLabel}>{m.label}</span>
                    <span className={styles.finderKind}>{m.kind}</span>
                  </div>
                  <p className={styles.finderDetail}>{m.detail}</p>
                  {m.statement && (
                    <button type="button" className={styles.recipeInsert} onClick={() => props.onApply(m.statement as string)}>
                      <CornerDownLeft size={12} /> use this
                    </button>
                  )}
                </div>
              ))
            )}
          </div>
        )}

        {!localMode && run && (
          <div className={styles.runBox}>
            <div className={styles.runStatus}>
              <Badge tone={run.status === "done" ? "ok" : run.status === "failed" ? "bad" : run.status === "cancelled" ? "warn" : "neutral"}>{run.status}</Badge>
              {run.usage && (
                <span className={styles.usage}>
                  {run.usage.tokens_in ?? 0}+{run.usage.tokens_out ?? 0} tok
                  {typeof run.usage.cost_usd === "number" && ` · $${run.usage.cost_usd.toFixed(3)}`}
                </span>
              )}
              {running && (
                <Button size="sm" variant="ghost" icon={<StopCircle size={14} />} onClick={cancel}>Cancel</Button>
              )}
            </div>
            <StepList events={run.events} />
            {run.error && <div className={styles.stepErr}>{run.error}</div>}
            {run.final?.answer && (
              <div className={`${styles.markdown} ${styles.finalAnswer}`}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{run.final.answer}</ReactMarkdown>
              </div>
            )}
            {(run.final?.notes ?? []).length > 0 && (
              <ul className={styles.notes}>
                {(run.final?.notes ?? []).map((n, i) => <li key={i}>{n}</li>)}
              </ul>
            )}
            {proposed && (
              <div className={styles.proposed}>
                <div className={styles.proposedHead}>Proposed statement</div>
                <Diff before={props.statement} after={proposed} />
                <div className={styles.proposedActions}>
                  <Button size="sm" variant="primary" icon={<Check size={14} />} onClick={() => props.onApply(proposed)}>Apply</Button>
                  <Button size="sm" variant="default" icon={<Play size={13} />} onClick={() => props.onApplyAndRun(proposed)}>Apply & run</Button>
                  <Button size="sm" variant="ghost" icon={<Copy size={13} />} onClick={() => void navigator.clipboard.writeText(proposed).then(() => toast.ok("Copied"))}>Copy</Button>
                </div>
              </div>
            )}
            {proposedSpec && (
              <div className={styles.proposedSpec}>
                <ChartPanel spec={proposedSpec} height={220} />
              </div>
            )}
          </div>
        )}

        {!localMode && !run && turns.length === 0 && (
          <p className={styles.helperHint}>
            Ask a question in plain language, or pick <Sensitive as="span">Explain</Sensitive> / Refine on your statement. The helper is a read-only tool loop; it never writes.
          </p>
        )}
      </div>

      <form
        className={styles.helperInputRow}
        onSubmit={(e) => {
          e.preventDefault();
          onAsk();
        }}
      >
        <textarea
          ref={inputRef}
          className={styles.helperInput}
          placeholder={localMode ? "Find a channel, field or function…" : "Ask a question…"}
          value={input}
          rows={2}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              onAsk();
            }
          }}
          aria-label="Ask the helper"
        />
        <IconButton type="submit" size="sm" variant="default" label="Ask" disabled={!input.trim() || running}>
          <Send size={15} />
        </IconButton>
      </form>
    </aside>
  );
});
