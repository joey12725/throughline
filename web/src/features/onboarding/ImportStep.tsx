import { useMemo, useState, type ReactNode } from "react";
import { Play, History } from "lucide-react";
import { Skeleton, Button, Badge } from "@/ui";
import { useCatalog, useJobs, useStartBackfill, type CatalogPlugin } from "@/api/setup";
import { useWizard } from "./wizard";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { PluginIcon } from "./PluginIcon";
import { JobProgress } from "./JobProgress";
import styles from "./Onboarding.module.css";

export function ImportStep(): ReactNode {
  const wiz = useWizard();
  const catalog = useCatalog();
  const jobs = useJobs(true);
  const startBackfill = useStartBackfill();
  const [started, setStarted] = useState(false);

  const eligible = useMemo<CatalogPlugin[]>(() => {
    const picked = new Set(wiz.selectedSources);
    return (catalog.data?.plugins ?? []).filter(
      // Only what the owner picked: an installed plugin with no required
      // settings is "configured" but must never import on its own.
      (p) => picked.has(p.id) && p.backfill.supported && p.status.configured && !p.status.connected,
    );
  }, [catalog.data?.plugins, wiz.selectedSources]);

  const backfillJobs = (jobs.data?.jobs ?? []).filter((j) => j.kind === "backfill");
  const showProgress = started || backfillJobs.length > 0;

  const startAll = (): void => {
    for (const p of eligible) startBackfill.mutate(p.id);
    setStarted(true);
  };

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Backfill your history"
        title="Pull your past in"
        lede="Throughline imports your history so your archive is useful right away — not months from now. Imports run in the background; you can move on while they catch up."
      />

      <div className={styles.body}>
        {catalog.isLoading ? (
          <Skeleton height={160} />
        ) : showProgress ? (
          <>
            <JobProgress kinds={["backfill", "install"]} />
            <Callout tone="info">You can keep going — imports continue in the background. Check their progress any time from Sources.</Callout>
          </>
        ) : eligible.length === 0 ? (
          <Callout tone="info">Nothing is ready to backfill yet. Configure a source with history support first, or skip ahead — you can import any time.</Callout>
        ) : (
          <>
            <p style={{ margin: 0, fontSize: "var(--t-sm)", color: "var(--ink-2)" }}>Ready to import from:</p>
            <div className={styles.detectList}>
              {eligible.map((p) => (
                <div key={p.id} className={styles.detectItem}>
                  <span className={styles.setupCardIcon} style={{ width: 28, height: 28 }}>
                    <PluginIcon name={p.icon} size={15} />
                  </span>
                  <span>{p.name}</span>
                  <span className={styles.footerSpacer} />
                  <Badge tone="neutral">
                    <History size={11} /> {p.backfill.history === "full" ? "full history" : p.backfill.history === "window" ? "recent window" : "sync"}
                  </Badge>
                </div>
              ))}
            </div>
            <div className={styles.cardActions}>
              <span className={styles.footerSpacer} />
              <Button variant="primary" icon={<Play size={15} />} onClick={startAll} disabled={startBackfill.isPending}>
                Start import{eligible.length > 1 ? "s" : ""}
              </Button>
            </div>
          </>
        )}
      </div>

      <WizardFooter skipLabel="Skip for now" />
    </div>
  );
}
