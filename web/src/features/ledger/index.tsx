/*
 * Ledger (/ui/ledger): the accountable surface — proposals and approvals the
 * owner can decide, model spend and the audit reads lane. Restricted tokens
 * see only what they may: the owner-only sections are hidden with a plain
 * reason, never a server error.
 */
import { useMemo, useState } from "react";
import { GitPullRequest, Coins, Activity, Lock } from "lucide-react";
import { useBootstrap } from "@/api";
import { EmptyState, Panel, PageHeader, Tabs } from "@/ui";
import Decisions from "./Decisions";
import SpendPanel from "./SpendPanel";
import AuditPanel from "./AuditPanel";
import styles from "./ledger.module.css";

type TabId = "decisions" | "spend" | "audit";

export default function LedgerScreen() {
  const { data: boot } = useBootstrap();
  const isOwner = boot?.token.kind === "owner" && boot.token.unrestricted;
  const [tab, setTab] = useState<TabId>("decisions");

  const items = useMemo(() => {
    const base = [
      { value: "decisions" as const, label: "Proposals & approvals", icon: <GitPullRequest size={14} /> },
      { value: "spend" as const, label: "Spend", icon: isOwner ? <Coins size={14} /> : <Lock size={12} /> },
      { value: "audit" as const, label: "Audit", icon: isOwner ? <Activity size={14} /> : <Lock size={12} /> },
    ];
    return base;
  }, [isOwner]);

  const ownerOnly = (what: string) => (
    <Panel className={styles.tabPanel}>
      <EmptyState icon={<Lock />} title={`${what} is owner-only`} description="This section reads cost and request data that a restricted token is not permitted to see. Sign in with the owner token to view it." />
    </Panel>
  );

  return (
    <div className={styles.screen}>
      <PageHeader title="Ledger" lede="Proposals, approvals, spend and the audit trail." />

      <Tabs items={items} value={tab} onChange={setTab} />

      <div className={styles.tabBody}>
        {tab === "decisions" && <Panel className={styles.tabPanel}><Decisions /></Panel>}
        {tab === "spend" && (isOwner ? <Panel className={styles.tabPanel}><SpendPanel enabled={tab === "spend"} /></Panel> : ownerOnly("Spend"))}
        {tab === "audit" && (isOwner ? <Panel className={styles.tabPanel}><AuditPanel /></Panel> : ownerOnly("Audit"))}
      </div>
    </div>
  );
}
