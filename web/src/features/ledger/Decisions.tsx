/*
 * Proposals & approvals — the one place the interface writes. Pending items come
 * first. Every decision goes through an explicit confirmation dialog and the
 * server's owner-gated route; nothing is ever decided automatically, and a
 * cancel leaves the item untouched. Approvals clear a cost gate; proposals
 * accept or reject an agent's suggested claim, link or correction.
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Check, X } from "lucide-react";
import { ErrorState, EmptyState, Dialog, Button, Sensitive, Skeleton, StaticChip } from "@/ui";
import { toast } from "@/ui";
import { formatInTz } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import { approveApproval, decideProposal, rejectApproval, useApprovals, useProposals, type Approval, type Proposal } from "./data";
import styles from "./ledger.module.css";

type Pending =
  | { type: "approval"; id: string; decision: "approve" | "reject" }
  | { type: "proposal"; id: number; decision: "accept" | "reject" };

export default function Decisions() {
  const tz = usePrefs((s) => s.tz);
  const qc = useQueryClient();
  const approvals = useApprovals();
  const proposals = useProposals();
  const [pending, setPending] = useState<Pending | null>(null);
  const [working, setWorking] = useState(false);

  const sortedApprovals = [...(approvals.data ?? [])].sort(pendingFirst);
  const sortedProposals = [...(proposals.data ?? [])].sort(pendingFirst);

  const confirm = async () => {
    if (!pending) return;
    setWorking(true);
    try {
      if (pending.type === "approval") {
        await (pending.decision === "approve" ? approveApproval(pending.id) : rejectApproval(pending.id));
        toast.ok(`Approval ${pending.decision === "approve" ? "approved" : "rejected"}`);
        await qc.invalidateQueries({ queryKey: ["ledger", "approvals"] });
      } else {
        await decideProposal(pending.id, pending.decision === "accept");
        toast.ok(`Proposal ${pending.decision === "accept" ? "accepted" : "rejected"}`);
        await qc.invalidateQueries({ queryKey: ["ledger", "proposals"] });
      }
      setPending(null);
    } catch (e) {
      toast.error("Could not record the decision", e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className={styles.decisions}>
      <section>
        <h3 className={styles.decH}>Approvals <span className={styles.decSub}>cost-gate decisions</span></h3>
        {approvals.isLoading && <Skeleton height={80} />}
        {approvals.error && <ErrorState error={approvals.error} />}
        {!approvals.isLoading && sortedApprovals.length === 0 && <EmptyState title="No approvals" description="No operation is waiting on a cost approval." compact />}
        <ul className={styles.decList}>
          {sortedApprovals.map((a: Approval) => (
            <li key={a.id} className={styles.decItem} data-pending={a.status === "pending"}>
              <div className={styles.decBody}>
                <div className={styles.decTop}>
                  <span className={styles.statusTag} data-tone={a.status === "pending" ? "warn" : "neutral"}>{a.status}</span>
                  <span className={styles.decCost}>{fmtCost(a.cost)} / {fmtCost(a.limit_cost)} cap</span>
                  <span className={styles.decDate}>{formatInTz(a.created_at, tz, { dateStyle: "medium", timeStyle: "short" })}</span>
                </div>
                {a.explanation && <Sensitive as="div" className={styles.decText}>{a.explanation}</Sensitive>}
              </div>
              {a.status === "pending" && (
                <div className={styles.decActions}>
                  <Button size="sm" variant="primary" icon={<Check size={14} />} onClick={() => setPending({ type: "approval", id: a.id, decision: "approve" })}>Approve</Button>
                  <Button size="sm" variant="ghost" icon={<X size={14} />} onClick={() => setPending({ type: "approval", id: a.id, decision: "reject" })}>Reject</Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h3 className={styles.decH}>Proposals <span className={styles.decSub}>agent suggestions awaiting your call</span></h3>
        {proposals.isLoading && <Skeleton height={80} />}
        {proposals.error && <ErrorState error={proposals.error} />}
        {!proposals.isLoading && sortedProposals.length === 0 && <EmptyState title="No proposals" description="No links, annotations or corrections are proposed for review." compact />}
        <ul className={styles.decList}>
          {sortedProposals.map((p: Proposal) => (
            <li key={p.id} className={styles.decItem} data-pending={p.status === "pending"}>
              <div className={styles.decBody}>
                <div className={styles.decTop}>
                  <StaticChip>{p.kind}</StaticChip>
                  <span className={styles.statusTag} data-tone={p.status === "pending" ? "warn" : "neutral"}>{p.status}</span>
                  <span className={styles.decDate}>{p.origin} · {formatInTz(p.created_at, tz, { dateStyle: "medium" })}</span>
                </div>
                <Sensitive as="div" className={styles.decText}>{summarise(p)}</Sensitive>
              </div>
              {p.status === "pending" && (
                <div className={styles.decActions}>
                  <Button size="sm" variant="primary" icon={<Check size={14} />} onClick={() => setPending({ type: "proposal", id: p.id, decision: "accept" })}>Accept</Button>
                  <Button size="sm" variant="ghost" icon={<X size={14} />} onClick={() => setPending({ type: "proposal", id: p.id, decision: "reject" })}>Reject</Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </section>

      <Dialog
        open={pending != null}
        onClose={() => !working && setPending(null)}
        title={pending ? dialogTitle(pending) : ""}
        description={pending ? dialogBody(pending) : undefined}
        footer={
          <>
            <Button variant="ghost" onClick={() => setPending(null)} disabled={working}>Cancel</Button>
            <Button variant={pending?.decision === "reject" ? "danger" : "primary"} onClick={confirm} disabled={working}>
              {working ? "Working…" : pending ? actionWord(pending) : "Confirm"}
            </Button>
          </>
        }
      >
        <p className={styles.dialogNote}>This writes to your Throughline and cannot be undone from here.</p>
      </Dialog>
    </div>
  );
}

function pendingFirst(a: { status: string }, b: { status: string }): number {
  return (a.status === "pending" ? 0 : 1) - (b.status === "pending" ? 0 : 1);
}
function fmtCost(v: string | number): string {
  const n = typeof v === "string" ? parseFloat(v) : v;
  return Number.isFinite(n) ? `$${n.toFixed(4)}` : "—";
}
function summarise(p: Proposal): string {
  const payload = p.payload ?? {};
  const s = payload["statement"] ?? payload["summary"] ?? payload["name"] ?? payload["type"];
  if (typeof s === "string") return s;
  return `${p.kind} proposal #${p.id}`;
}
function dialogTitle(p: Pending): string {
  if (p.type === "approval") return p.decision === "approve" ? "Approve this operation?" : "Reject this approval?";
  return p.decision === "accept" ? "Accept this proposal?" : "Reject this proposal?";
}
function dialogBody(p: Pending): string {
  if (p.type === "approval") return p.decision === "approve" ? "The gated operation will be allowed to run and spend up to its cap." : "The operation stays blocked.";
  return p.decision === "accept" ? "The proposed link, annotation or correction becomes part of your record." : "The proposal is dismissed and leaves no trace in your record.";
}
function actionWord(p: Pending): string {
  if (p.type === "approval") return p.decision === "approve" ? "Approve" : "Reject";
  return p.decision === "accept" ? "Accept" : "Reject";
}
