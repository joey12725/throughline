/*
 * One classifier: status controls (approve / pause / resume / reject, run now,
 * backfill, retract marks), its numbers, results (python: a verdict breakdown
 * and the judged threads; message: the annotated messages), recent runs and the
 * definition itself. Every write goes through a confirmation.
 */
import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft, Check, ChevronRight, History, Pause, Pencil, Play, RotateCcw, Undo2, X } from "lucide-react";
import {
  Badge,
  Button,
  CodeBlock,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  KeyValue,
  Page,
  PageHeader,
  Panel,
  RelativeTime,
  Segmented,
  Sensitive,
  Skeleton,
  Stat,
  Tabs,
  toast,
} from "@/ui";
import {
  backfillClassifier,
  retractClassifierMarks,
  runClassifier,
  setClassifierStatus,
  useClassifier,
  useClassifierResults,
  useInvalidateClassifiers,
  type ClassifierDetail,
  type ClassifierRun,
  type MessageResults,
  type PythonResults,
} from "@/api/classifiers";
import { formatUsd, orderVerdicts } from "./model";
import { KindBadge, StatusBadge, ValueChip, VerdictBadge, errorText } from "./shared";
import styles from "./Classifiers.module.css";

type Action =
  | { kind: "status"; status: "active" | "paused" | "rejected"; label: string }
  | { kind: "run" }
  | { kind: "backfill" }
  | { kind: "retract" };

export function ClassifierDetailPage({ name }: { name: string }): ReactNode {
  const navigate = useNavigate();
  const q = useClassifier(name);
  const [tab, setTab] = useState<"results" | "runs" | "definition">("results");
  const [action, setAction] = useState<Action | null>(null);

  if (q.isLoading) {
    return (
      <Page width="wide">
        <Skeleton height={32} />
        <Skeleton height={90} />
        <Skeleton height={300} />
      </Page>
    );
  }
  if (q.error || !q.data) {
    return (
      <Page width="wide">
        <BackLink />
        <ErrorState error={q.error} title={`Could not load ${name}`} onRetry={() => void q.refetch()} />
      </Page>
    );
  }
  const c = q.data;
  const s = c.stats;
  const pending = c.kind === "message" ? s?.pending_messages : s?.pending_threads;

  return (
    <Page width="wide">
      <PageHeader
        eyebrow={<BackLink />}
        title={<span className={styles.mono} style={{ fontSize: "inherit" }}>{c.name}</span>}
        lede={c.description ?? undefined}
        actions={<StatusActions c={c} onAction={setAction} onEdit={() => navigate(`/classifiers/${encodeURIComponent(c.name)}/edit`)} />}
      />
      <div className={styles.row}>
        <KindBadge kind={c.kind} />
        <StatusBadge status={c.status} />
        <span className={styles.note}>
          v{c.version} · writes <code>{c.attribute}</code> · {c.channels.length ? c.channels.join(", ") : "all channels"} · updated{" "}
          <RelativeTime value={c.updated_at} />
        </span>
      </div>
      {c.status === "proposed" && (
        <p className={styles.spend} role="status">
          An agent proposed this definition. Nothing runs until you approve it.
        </p>
      )}
      {c.last_error && <p className={styles.errorLine}>Last error: {c.last_error}</p>}

      <div className={styles.stats}>
        <Stat size="sm" label={c.kind === "python" ? "Threads judged" : "Annotations"} value={((c.kind === "python" ? s?.threads_judged : s?.annotations) ?? 0).toLocaleString()} />
        {c.kind === "python" && <Stat size="sm" label="Turns marked" value={(s?.annotations ?? 0).toLocaleString()} />}
        <Stat size="sm" label="Group marks" value={(s?.group_annotations ?? 0).toLocaleString()} />
        <Stat size="sm" label="Pending" value={pending === undefined ? "—" : pending.toLocaleString()} />
        <Stat size="sm" label="This month" value={formatUsd(s?.month_cost)} unit={c.monthly_cap_usd ? `of ${formatUsd(c.monthly_cap_usd)}` : undefined} />
        <Stat size="sm" label="All time" value={formatUsd(s?.total_cost)} />
      </div>

      <Tabs
        value={tab}
        onChange={setTab}
        items={[
          { value: "results", label: "Results" },
          { value: "runs", label: "Runs", count: c.runs?.length ?? 0 },
          { value: "definition", label: c.kind === "python" ? "Code" : "Definition" },
        ]}
      />
      {tab === "results" && (c.kind === "python" ? <PythonResultsPanel c={c} /> : <MessageResultsPanel c={c} />)}
      {tab === "runs" && <RunsPanel runs={c.runs ?? []} />}
      {tab === "definition" && <DefinitionPanel c={c} />}

      <ActionDialog c={c} action={action} onClose={() => setAction(null)} />
    </Page>
  );
}

function BackLink(): ReactNode {
  return (
    <Link to="/classifiers" className={styles.back}>
      <ArrowLeft size={13} /> Classifiers
    </Link>
  );
}

function StatusActions({ c, onAction, onEdit }: { c: ClassifierDetail; onAction: (a: Action) => void; onEdit: () => void }): ReactNode {
  return (
    <div className={styles.row}>
      {c.status === "proposed" && (
        <>
          <Button size="sm" variant="primary" icon={<Check size={13} />} onClick={() => onAction({ kind: "status", status: "active", label: "Approve" })}>
            Approve
          </Button>
          <Button size="sm" icon={<X size={13} />} onClick={() => onAction({ kind: "status", status: "rejected", label: "Reject" })}>
            Reject
          </Button>
        </>
      )}
      {c.status === "active" && (
        <>
          <Button size="sm" variant="primary" icon={<Play size={13} />} onClick={() => onAction({ kind: "run" })}>
            Run now
          </Button>
          <Button size="sm" icon={<Pause size={13} />} onClick={() => onAction({ kind: "status", status: "paused", label: "Pause" })}>
            Pause
          </Button>
        </>
      )}
      {c.status === "paused" && (
        <Button size="sm" variant="primary" icon={<Play size={13} />} onClick={() => onAction({ kind: "status", status: "active", label: "Resume" })}>
          Resume
        </Button>
      )}
      {c.status === "rejected" && (
        <Button size="sm" icon={<RotateCcw size={13} />} onClick={() => onAction({ kind: "status", status: "active", label: "Reactivate" })}>
          Reactivate
        </Button>
      )}
      {c.status !== "rejected" && c.status !== "proposed" && (
        <Button size="sm" icon={<History size={13} />} onClick={() => onAction({ kind: "backfill" })}>
          Backfill
        </Button>
      )}
      <Button size="sm" variant="ghost" icon={<Pencil size={13} />} onClick={onEdit}>
        Edit
      </Button>
      {c.status !== "proposed" && c.status !== "rejected" && (
        <Button size="sm" variant="ghost" onClick={() => onAction({ kind: "status", status: "rejected", label: "Reject" })}>
          Reject
        </Button>
      )}
      <Button size="sm" variant="danger" icon={<Undo2 size={13} />} onClick={() => onAction({ kind: "retract" })}>
        Retract marks
      </Button>
    </div>
  );
}

function ActionDialog({ c, action, onClose }: { c: ClassifierDetail; action: Action | null; onClose: () => void }): ReactNode {
  const invalidate = useInvalidateClassifiers();
  const [busy, setBusy] = useState(false);
  const [maxItems, setMaxItems] = useState("");
  const [since, setSince] = useState<"30d" | "all" | "date">("30d");
  const [date, setDate] = useState("");
  const [confirmText, setConfirmText] = useState("");

  const close = () => {
    if (busy) return;
    setConfirmText("");
    onClose();
  };

  const go = async () => {
    if (!action) return;
    setBusy(true);
    try {
      if (action.kind === "status") {
        await setClassifierStatus(c.name, action.status);
        toast.ok(`${DONE[action.label] ?? action.label} ${c.name}`);
      } else if (action.kind === "run") {
        const run = await runClassifier(c.name, Number(maxItems) || undefined);
        toast.ok(`Run ${run?.status ?? "started"}`, run ? `${run.items ?? 0} items, ${run.marks ?? 0} marks, ${formatUsd(run.cost)}` : undefined);
      } else if (action.kind === "backfill") {
        await backfillClassifier(c.name, since === "date" ? date : since);
        toast.ok("Backfill queued", "It runs within the classifier's per-run limits.");
      } else {
        await retractClassifierMarks(c.name);
        toast.ok("Marks retracted", `${c.name}'s annotations are withdrawn. Your own corrections stay.`);
      }
      await invalidate();
      setConfirmText("");
      onClose();
    } catch (e) {
      toast.error("That did not work", errorText(e));
    } finally {
      setBusy(false);
    }
  };

  if (!action) return <Dialog open={false} onClose={close} title="" />;

  let title = "";
  let body: ReactNode = null;
  let confirmLabel = "Confirm";
  let danger = false;
  let ready = true;
  if (action.kind === "status") {
    title = `${action.label} ${c.name}?`;
    confirmLabel = action.label;
    danger = action.status === "rejected";
    body = (
      <p className={styles.note}>
        {action.status === "active"
          ? "It will start judging on its next tick and spend within its limits."
          : action.status === "paused"
            ? "It stops judging. Existing marks stay."
            : "It stops and will not run again unless reactivated. Existing marks stay until retracted."}
      </p>
    );
  } else if (action.kind === "run") {
    title = `Run ${c.name} now?`;
    confirmLabel = "Run now";
    body = (
      <div className={styles.stack}>
        <p className={styles.spend}>This makes real model calls and spends money, up to the per-run cost limit.</p>
        <Field label="Max items" htmlFor="run-max" hint="Optional; the spec's limit otherwise">
          <Input id="run-max" type="number" mono min={1} value={maxItems} onChange={(e) => setMaxItems(e.target.value)} />
        </Field>
      </div>
    );
  } else if (action.kind === "backfill") {
    title = `Backfill ${c.name}`;
    confirmLabel = "Queue backfill";
    ready = since !== "date" || /^\d{4}-\d{2}-\d{2}$/.test(date);
    body = (
      <div className={styles.stack}>
        <p className={styles.note}>Judge items that arrived before the classifier existed. Runs spend within the classifier's limits.</p>
        <Segmented<"30d" | "all" | "date">
          aria-label="Backfill since"
          value={since}
          onChange={setSince}
          items={[
            { value: "30d", label: "Last 30 days" },
            { value: "all", label: "Everything" },
            { value: "date", label: "Since…" },
          ]}
        />
        {since === "date" && <Input aria-label="Since date" type="date" mono value={date} onChange={(e) => setDate(e.target.value)} />}
      </div>
    );
  } else {
    title = `Retract every mark from ${c.name}?`;
    confirmLabel = "Retract marks";
    danger = true;
    ready = confirmText === c.name;
    body = (
      <div className={styles.stack}>
        <p className={styles.note}>
          Withdraws the {(c.stats?.annotations ?? 0).toLocaleString()} annotations and {(c.stats?.group_annotations ?? 0).toLocaleString()} group marks
          this classifier wrote. Your own corrections stay. The definition is kept.
        </p>
        <Field label={`Type ${c.name} to confirm`} htmlFor="retract-confirm">
          <Input id="retract-confirm" mono value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoComplete="off" />
        </Field>
      </div>
    );
  }

  return (
    <Dialog
      open
      onClose={close}
      title={title}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={() => void go()} disabled={busy || !ready}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </>
      }
    >
      {body}
    </Dialog>
  );
}

/* ---- results ------------------------------------------------------------------ */

const PAGE = 50;
const DONE: Record<string, string> = { Approve: "Approved", Reject: "Rejected", Pause: "Paused", Resume: "Resumed", Reactivate: "Reactivated" };

function PythonResultsPanel({ c }: { c: ClassifierDetail }): ReactNode {
  const [verdict, setVerdict] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const r = useClassifierResults(c.name, { limit: PAGE, offset, verdict: verdict || null });
  const data = r.data?.kind === "python" ? (r.data as PythonResults) : null;
  const counts = data ? orderVerdicts(data.verdicts ?? {}) : [];
  const total = counts.reduce((n, [, v]) => n + v, 0);

  return (
    <Panel
      title="Judged conversations"
      subtitle="Open one to see who was marked and to correct it"
      footer={
        data && (
          <div className={styles.pager}>
            <span>
              {data.items.length ? `${offset + 1}–${offset + data.items.length}` : "0"}
              {verdict ? ` of ${(data.verdicts?.[verdict] ?? 0).toLocaleString()}` : total ? ` of ${total.toLocaleString()}` : ""}
            </span>
            <span className={styles.row}>
              <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                Newer
              </Button>
              <Button size="sm" variant="ghost" disabled={data.items.length < PAGE} onClick={() => setOffset(offset + PAGE)}>
                Older
              </Button>
            </span>
          </div>
        )
      }
    >
      <div className={styles.stack}>
        {counts.length > 0 && (
          <div className={styles.row} role="group" aria-label="Filter by verdict">
            <VerdictFilter label="All" count={total} on={verdict === ""} onClick={() => (setVerdict(""), setOffset(0))} />
            {counts.map(([k, n]) => (
              <VerdictFilter key={k} label={k} count={n} on={verdict === k} onClick={() => (setVerdict(k), setOffset(0))} />
            ))}
          </div>
        )}
        {r.isLoading && <Skeleton lines={5} height={36} />}
        {r.error && <ErrorState error={r.error} title="Could not load results" onRetry={() => void r.refetch()} />}
        {data && data.items.length === 0 && (
          <EmptyState compact title="Nothing judged yet" description={c.status === "active" ? "Conversations are judged once they settle." : "Approve or resume it to start judging."} />
        )}
        {data && data.items.length > 0 && (
          <ul className={styles.threadList}>
            {data.items.map((it) => {
              const marked = (it.result.marks ?? []).filter((m) => m.target === "participant").length;
              return (
                <li key={String(it.thread_id)}>
                  <Link className={styles.threadRow} to={`/classifiers/${encodeURIComponent(c.name)}/threads/${encodeURIComponent(String(it.thread_id))}`}>
                    <div style={{ minWidth: 0 }}>
                      <Sensitive className={styles.tryTitle}>{it.title || `Thread ${it.thread_id}`}</Sensitive>
                      <div className={styles.threadMeta}>
                        <span className={styles.mono}>{it.channel}</span>
                        {it.started_at && <RelativeTime value={it.started_at} exact />}
                        {it.result.messages !== undefined && <span>{it.result.messages} messages</span>}
                        {marked > 0 && <span>{marked} speaker{marked === 1 ? "" : "s"} marked</span>}
                        {it.version !== c.version && <span>judged by v{it.version}</span>}
                      </div>
                      {it.result.status === "error" && it.result.error && <p className={styles.errorLine}>{it.result.error}</p>}
                    </div>
                    <span className={styles.row}>
                      {it.result.status === "done" ? <VerdictBadge verdict={it.result.verdict} /> : <Badge tone={it.result.status === "error" ? "bad" : "neutral"}>{it.result.status}</Badge>}
                      <ChevronRight size={14} aria-hidden className={styles.muted} />
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function VerdictFilter({ label, count, on, onClick }: { label: string; count: number; on: boolean; onClick: () => void }): ReactNode {
  return (
    <Button size="sm" variant={on ? "primary" : "default"} aria-pressed={on} onClick={onClick}>
      {label}
      <span className={`tnum ${styles.count}`}>{count.toLocaleString()}</span>
    </Button>
  );
}

function MessageResultsPanel({ c }: { c: ClassifierDetail }): ReactNode {
  const [offset, setOffset] = useState(0);
  const r = useClassifierResults(c.name, { limit: PAGE, offset });
  const data = r.data?.kind === "message" ? (r.data as MessageResults) : null;
  return (
    <Panel
      title="Annotated messages"
      subtitle="Newest first"
      footer={
        data && (
          <div className={styles.pager}>
            <span>{data.items.length ? `${offset + 1}–${offset + data.items.length}` : "0"}</span>
            <span className={styles.row}>
              <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                Newer
              </Button>
              <Button size="sm" variant="ghost" disabled={data.items.length < PAGE} onClick={() => setOffset(offset + PAGE)}>
                Older
              </Button>
            </span>
          </div>
        )
      }
    >
      {r.isLoading && <Skeleton lines={5} height={36} />}
      {r.error && <ErrorState error={r.error} title="Could not load results" onRetry={() => void r.refetch()} />}
      {data && data.items.length === 0 && <EmptyState compact title="No annotations yet" description="Marks appear here as messages are judged." />}
      {data &&
        data.items.map((m) => (
          <div key={String(m.annotation_id)} className={styles.msgItem}>
            <div className={styles.rowBetween}>
              <span className={styles.row}>
                <span className={styles.mono}>{m.channel}</span>
                {m.author_handle && <Sensitive className={styles.note}>{m.author_handle}</Sensitive>}
                {m.happened_at && <RelativeTime value={m.happened_at} className={styles.note} />}
              </span>
              <span className={styles.row}>
                <ValueChip value={m.value} />
                {m.confidence !== null && <span className={styles.note}>{Math.round(m.confidence * 100)}%</span>}
                {m.status !== "active" && <Badge>{m.status}</Badge>}
              </span>
            </div>
            {m.content && (
              <Sensitive as="div">
                <p className={styles.excerpt}>{m.content}</p>
              </Sensitive>
            )}
            {m.reason && <p className={styles.reason}>{m.reason}</p>}
          </div>
        ))}
    </Panel>
  );
}

/* ---- runs & definition ---------------------------------------------------------- */

function RunsPanel({ runs }: { runs: ClassifierRun[] }): ReactNode {
  if (!runs.length) {
    return (
      <Panel>
        <EmptyState compact title="No runs yet" />
      </Panel>
    );
  }
  return (
    <Panel flush>
      <div className={styles.tableScroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Started</th>
              <th>Trigger</th>
              <th>Status</th>
              <th className={styles.num}>Items</th>
              <th className={styles.num}>Marks</th>
              <th className={styles.num}>Calls</th>
              <th className={styles.num}>Cost</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={String(run.id)}>
                <td>
                  <RelativeTime value={run.started_at} />
                  {run.errors?.length > 0 && (
                    <ul className={styles.runErrors}>
                      {run.errors.slice(0, 5).map((e, i) => (
                        <li key={i}>
                          {e.thread_id !== undefined && <span className={styles.mono}>thread {e.thread_id}: </span>}
                          {e.message_id !== undefined && <span className={styles.mono}>message {e.message_id}: </span>}
                          {e.error}
                        </li>
                      ))}
                      {run.errors.length > 5 && <li>…and {run.errors.length - 5} more</li>}
                    </ul>
                  )}
                </td>
                <td className={styles.mono}>{run.trigger}</td>
                <td>
                  <StatusBadge status={run.status} />
                </td>
                <td className={styles.num}>{run.items}</td>
                <td className={styles.num}>{run.marks}</td>
                <td className={styles.num}>{run.calls}</td>
                <td className={styles.num}>{formatUsd(run.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function DefinitionPanel({ c }: { c: ClassifierDetail }): ReactNode {
  const a = c.attribute_definition;
  return (
    <div className={styles.stack}>
      {c.kind === "python" && (
        <Panel title="Code" subtitle={`Version ${c.version}`}>
          {c.code ? <CodeBlock code={c.code} lang="python" /> : <p className={styles.note}>No code stored.</p>}
        </Panel>
      )}
      <Panel title="Spec">
        <KeyValue
          rows={Object.entries(c.spec ?? {}).map(([k, v]) => ({
            key: <span className={styles.mono}>{k}</span>,
            value: typeof v === "object" && v !== null ? <CodeBlock inline code={JSON.stringify(v)} /> : <span className={styles.mono}>{String(v)}</span>,
          }))}
        />
      </Panel>
      {a && (
        <Panel title={`Attribute ${a.name}`} subtitle={a.description ?? undefined}>
          <KeyValue
            rows={[
              { key: "Origin", value: a.origin },
              { key: "In recall", value: a.recall_effect === "exclude" ? "marked messages are excluded" : "no effect" },
              { key: "Active annotations", value: a.active_annotations.toLocaleString() },
              {
                key: "Fields",
                value: (
                  <span className={styles.mono}>
                    {Object.entries(a.value_schema)
                      .map(([k, s]) => `${k}: ${s.type}${s.required ? "" : "?"}${s.values ? ` (${s.values.join("|")})` : ""}`)
                      .join(", ") || "none"}
                  </span>
                ),
              },
            ]}
          />
        </Panel>
      )}
    </div>
  );
}

