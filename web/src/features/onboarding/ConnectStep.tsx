import { useEffect, useMemo, useState, type ReactNode } from "react";
import { PlugZap } from "lucide-react";
import { Skeleton, EmptyState, ErrorState } from "@/ui";
import { useCatalog, useJobs, type CatalogPlugin } from "@/api/setup";
import { useWizard } from "./wizard";
import { StepHeader, WizardFooter } from "./parts";
import { SetupCard } from "./SetupCard";
import styles from "./Onboarding.module.css";

export function ConnectStep(): ReactNode {
  const wiz = useWizard();
  const catalog = useCatalog();
  const jobs = useJobs(true);

  const builtin = useMemo(() => {
    const m = new Map<string, boolean>();
    for (const r of catalog.data?.repos ?? []) m.set(r.id, r.builtin);
    return m;
  }, [catalog.data?.repos]);

  const toSetup = useMemo<CatalogPlugin[]>(() => {
    const picked = new Set(wiz.selectedSources);
    return (catalog.data?.plugins ?? []).filter((p) => picked.has(p.id) && !p.status.connected);
  }, [catalog.data?.plugins, wiz.selectedSources]);

  const installJobFor = (id: string) => jobs.data?.jobs.find((j) => j.kind === "install" && j.plugin_id === id) ?? null;

  const [openId, setOpenId] = useState<string | null>(null);
  useEffect(() => {
    if (openId === null && toSetup.length > 0) {
      const firstUnconfigured = toSetup.find((p) => !p.status.configured) ?? toSetup[0];
      if (firstUnconfigured) setOpenId(firstUnconfigured.id);
    }
  }, [toSetup, openId]);

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Set up your sources"
        title="Connect what you picked"
        lede="Each source needs a little setup — a key, a URL, or an export to upload. Save it, then test the connection. You can leave any of these for later."
      />

      <div className={styles.body}>
        {catalog.isLoading ? (
          <div className={styles.loadingStack}>
            <Skeleton height={64} />
            <Skeleton height={64} />
          </div>
        ) : catalog.error ? (
          <ErrorState error={catalog.error} title="Could not load the catalog" onRetry={() => void catalog.refetch()} />
        ) : toSetup.length === 0 ? (
          <EmptyState
            icon={<PlugZap size={22} />}
            title="Nothing to connect yet"
            description="You haven't picked any sources that need setup. Go back to choose some, or skip ahead."
          />
        ) : (
          <div className={styles.setupList}>
            {toSetup.map((p) => (
              <SetupCard
                key={p.id}
                plugin={p}
                repoBuiltin={builtin.get(p.repo_id) ?? true}
                open={openId === p.id}
                onToggle={() => setOpenId((cur) => (cur === p.id ? null : p.id))}
                installJob={installJobFor(p.id)}
              />
            ))}
          </div>
        )}
      </div>

      <WizardFooter />
    </div>
  );
}
