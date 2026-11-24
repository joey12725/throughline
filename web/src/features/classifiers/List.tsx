/*
 * The classifier list: one card per classifier. Proposed definitions (written by
 * an agent, waiting for the owner) sort first and carry Approve / Reject.
 */
import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Check, Plus, Tags, X } from "lucide-react";
import { Badge, Button, EmptyState, ErrorState, Page, PageHeader, RelativeTime, Skeleton, toast } from "@/ui";
import {
  setClassifierStatus,
  useClassifiers,
  useInvalidateClassifiers,
  type Classifier,
} from "@/api/classifiers";
import { KindBadge, StatusBadge, errorText, isMissingRoute } from "./shared";
import { formatUsd } from "./model";
import styles from "./Classifiers.module.css";

const ORDER: Record<string, number> = { proposed: 0, active: 1, paused: 2, rejected: 3 };

export function ClassifierList(): ReactNode {
  const navigate = useNavigate();
  const list = useClassifiers();
  const items = [...(list.data?.classifiers ?? [])].sort(
    (a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || a.name.localeCompare(b.name),
  );
  const proposed = items.filter((c) => c.status === "proposed").length;

  return (
    <Page width="wide">
      <PageHeader
        title="Classifiers"
        lede="Jobs that read arriving messages or whole conversations and write annotations on them. Agents propose; you approve."
        actions={
          <Button variant="primary" icon={<Plus size={14} />} onClick={() => navigate("/classifiers/new")}>
            New classifier
          </Button>
        }
      />
      {list.isLoading && (
        <div className={styles.cards}>
          <Skeleton height={180} />
          <Skeleton height={180} />
          <Skeleton height={180} />
        </div>
      )}
      {list.error &&
        (isMissingRoute(list.error) ? (
          <EmptyState
            icon={<Tags size={28} />}
            title="Classifiers are not on this server yet"
            description="This Throughline does not serve /v1/classifiers. Update the server to use them."
          />
        ) : (
          <ErrorState error={list.error} title="Could not load classifiers" onRetry={() => void list.refetch()} />
        ))}
      {list.data && items.length === 0 && (
        <EmptyState
          icon={<Tags size={28} />}
          title="No classifiers yet"
          description="Ask a question of every arriving message, or write a function that judges a whole conversation."
          action={
            <Button variant="primary" icon={<Plus size={14} />} onClick={() => navigate("/classifiers/new")}>
              New classifier
            </Button>
          }
        />
      )}
      {list.data && items.length > 0 && (
        <>
          <p className={styles.note}>
            {items.length} classifier{items.length === 1 ? "" : "s"}
            {proposed > 0 && <> · <span className={styles.warn}>{proposed} waiting for approval</span></>}
            {list.data.sandbox && <> · python runs in a <code>{list.data.sandbox}</code> sandbox</>}
          </p>
          <div className={styles.cards}>
            {items.map((c) => (
              <ClassifierCard key={c.name} c={c} />
            ))}
          </div>
        </>
      )}
    </Page>
  );
}

function ClassifierCard({ c }: { c: Classifier }): ReactNode {
  const invalidate = useInvalidateClassifiers();
  const [busy, setBusy] = useState<string | null>(null);
  const run = c.stats?.last_run ?? null;
  const pending = c.kind === "message" ? c.stats?.pending_messages : c.stats?.pending_threads;

  const decide = async (status: "active" | "rejected") => {
    setBusy(status);
    try {
      await setClassifierStatus(c.name, status);
      toast.ok(status === "active" ? `Approved ${c.name}` : `Rejected ${c.name}`);
      await invalidate();
    } catch (e) {
      toast.error("Could not change the status", errorText(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <article
      className={[styles.card, c.status === "proposed" && styles.cardProposed, c.status === "rejected" && styles.cardMuted]
        .filter(Boolean)
        .join(" ")}
      aria-label={c.name}
    >
      <div className={styles.cardHead}>
        <div className={styles.stackTight}>
          <Link to={`/classifiers/${encodeURIComponent(c.name)}`} className={styles.cardName}>
            {c.name}
          </Link>
          <div className={styles.row}>
            <KindBadge kind={c.kind} />
            <StatusBadge status={c.status} />
            <span className={styles.note}>v{c.version}</span>
          </div>
        </div>
      </div>
      {c.description && <p className={styles.cardDesc}>{c.description}</p>}
      <div className={styles.facts}>
        <Fact k="Attribute" v={c.attribute} />
        <Fact k="Channels" v={c.channels.length ? c.channels.join(", ") : "all"} />
        <Fact
          k="Last run"
          v={
            run ? (
              <span className={styles.row} style={{ gap: 4 }}>
                <RelativeTime value={run.started_at} />
                <Badge tone={run.status === "done" ? "ok" : run.status === "failed" ? "bad" : "warn"}>{run.status}</Badge>
              </span>
            ) : c.last_run_at ? (
              <RelativeTime value={c.last_run_at} />
            ) : (
              "never"
            )
          }
        />
        <Fact k="This month" v={`${formatUsd(c.stats?.month_cost)}${c.monthly_cap_usd ? ` / ${formatUsd(c.monthly_cap_usd)}` : ""}`} />
        <Fact k={c.kind === "python" ? "Threads judged" : "Annotations"} v={(c.kind === "python" ? c.stats?.threads_judged : c.stats?.annotations) ?? 0} />
        <Fact k="Pending" v={pending ?? "—"} />
      </div>
      {c.last_error && <p className={styles.errorLine}>Last error: {c.last_error}</p>}
      {c.status === "proposed" && (
        <div className={styles.proposedBar}>
          <span>Proposed by an agent — nothing runs until you approve.</span>
          <span className={styles.row}>
            <Button size="sm" variant="ghost" icon={<X size={13} />} disabled={!!busy} onClick={() => void decide("rejected")}>
              {busy === "rejected" ? "Rejecting…" : "Reject"}
            </Button>
            <Button size="sm" variant="primary" icon={<Check size={13} />} disabled={!!busy} onClick={() => void decide("active")}>
              {busy === "active" ? "Approving…" : "Approve"}
            </Button>
          </span>
        </div>
      )}
    </article>
  );
}

function Fact({ k, v }: { k: string; v: ReactNode }): ReactNode {
  return (
    <div className={styles.fact}>
      <span className={styles.factKey}>{k}</span>
      <span className={styles.factVal}>{v}</span>
    </div>
  );
}
