/*
 * Settings → Plugins. Installed and available connectors, grouped by repository,
 * each with an enable switch, configure (the shared SetupCard) and live status.
 * Third-party repos can be removed; the bundled set cannot. "Add from GitHub"
 * clones and validates a new repository. A "Setup" row re-opens the wizard.
 */
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { FolderGit2, Plus, Loader2, Trash2, Wand2, ArrowRight, Package } from "lucide-react";
import { Panel, Field, Input, Button, Badge } from "@/ui";
import {
  useCatalog,
  useAddRepo,
  useRemoveRepo,
  setupProblem,
  type PluginRepo,
  type SetupProblem,
} from "@/api/setup";
import { SetupCard } from "@/features/onboarding/SetupCard";
import { ProblemView } from "@/features/onboarding/ProblemView";
import { Callout } from "@/features/onboarding/parts";
import onb from "@/features/onboarding/Onboarding.module.css";
import styles from "./Settings.module.css";

export function SetupLinkPanel(): ReactNode {
  return (
    <Panel title="Setup" subtitle="The first-run experience, any time">
      <p className={styles.note}>Re-run onboarding to add sources, configure models, connect an agent, or replay the tour.</p>
      <div className={styles.row}>
        <Link to="/welcome">
          <Button variant="default" icon={<Wand2 size={15} />}>
            Run setup
            <ArrowRight size={14} style={{ marginLeft: 6 }} />
          </Button>
        </Link>
      </div>
    </Panel>
  );
}

export function PluginsPanel(): ReactNode {
  const catalog = useCatalog();
  const [openId, setOpenId] = useState<string | null>(null);

  const byRepo = useMemo(() => {
    const repos = catalog.data?.repos ?? [];
    const plugins = catalog.data?.plugins ?? [];
    return repos.map((repo) => ({
      repo,
      plugins: plugins.filter((p) => p.repo_id === repo.id),
    }));
  }, [catalog.data]);

  return (
    <Panel title="Plugins" subtitle="Connectors, grouped by repository" className={styles.span2}>
      {catalog.isLoading ? (
        <p className={styles.note}>Loading the catalog…</p>
      ) : catalog.error ? (
        <ProblemView problem={setupProblem(catalog.error)} />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--s-5)" }}>
          {(catalog.data?.problems ?? []).length > 0 && (
            <Callout tone="warn">
              Some repositories had problems:
              <ul className={onb.problemList} style={{ marginTop: "var(--s-2)" }}>
                {catalog.data!.problems!.map((p, i) => (
                  <li key={i}><code>{p.repo_id}</code> — {p.message}</li>
                ))}
              </ul>
            </Callout>
          )}

          {byRepo.map(({ repo, plugins }) => (
            <RepoGroup key={repo.id} repo={repo} openId={openId} setOpenId={setOpenId} pluginsList={plugins} />
          ))}

          <AddRepoBox />
        </div>
      )}
    </Panel>
  );
}

function RepoGroup({
  repo,
  pluginsList,
  openId,
  setOpenId,
}: {
  repo: PluginRepo;
  pluginsList: import("@/api/setup").CatalogPlugin[];
  openId: string | null;
  setOpenId: (id: string | null) => void;
}): ReactNode {
  const removeRepo = useRemoveRepo();
  const [problem, setProblem] = useState<SetupProblem | null>(null);

  return (
    <section>
      <div className={onb.sectionLabel} style={{ marginTop: 0 }}>
        <Package size={13} />
        {repo.name}
        {repo.builtin ? (
          <Badge tone="neutral">bundled</Badge>
        ) : (
          <>
            <span className={onb.footerSpacer} />
            <button
              type="button"
              className={onb.advancedToggle}
              onClick={() => removeRepo.mutate(repo.id, { onError: (e) => setProblem(setupProblem(e)) })}
              disabled={removeRepo.isPending}
            >
              <Trash2 size={13} aria-hidden /> Remove repository
            </button>
          </>
        )}
      </div>
      {problem && <ProblemView problem={problem} />}
      {pluginsList.length === 0 ? (
        <p className={styles.note}>No plugins in this repository.</p>
      ) : (
        <div className={onb.setupList}>
          {pluginsList.map((p) => (
            <SetupCard
              key={p.id}
              plugin={p}
              repoBuiltin={repo.builtin}
              open={openId === p.id}
              onToggle={() => setOpenId(openId === p.id ? null : p.id)}
              enableOnSave={false}
              showEnableToggle
            />
          ))}
        </div>
      )}
    </section>
  );
}

function AddRepoBox(): ReactNode {
  const addRepo = useAddRepo();
  const [url, setUrl] = useState("");
  const [problem, setProblem] = useState<SetupProblem | null>(null);
  const [added, setAdded] = useState<string | null>(null);

  const submit = (): void => {
    const u = url.trim();
    if (!u) return;
    setProblem(null);
    setAdded(null);
    addRepo.mutate(u, {
      onSuccess: (repo) => { setUrl(""); setAdded(repo.name); },
      onError: (e) => setProblem(setupProblem(e)),
    });
  };

  return (
    <div className={onb.repoBox}>
      <div className={onb.repoBoxHead}><FolderGit2 size={15} /> Add from GitHub</div>
      <p className={onb.repoBoxNote}>Paste a repository URL to add its connectors. Throughline reads its manifest before running any of its code.</p>
      <div className={onb.repoForm}>
        <Field label="Repository URL" htmlFor="settings-repo-url">
          <Input id="settings-repo-url" mono placeholder="https://github.com/you/throughline-plugins" value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } }} />
        </Field>
        <Button variant="default" icon={addRepo.isPending ? <Loader2 size={15} className={onb.spin} /> : <Plus size={15} />} onClick={submit} disabled={addRepo.isPending || !url.trim()} style={{ alignSelf: "flex-end" }}>
          {addRepo.isPending ? "Adding…" : "Add"}
        </Button>
      </div>
      {added && <Callout tone="info">Added <strong>{added}</strong>.</Callout>}
      {problem && <ProblemView problem={problem} />}
    </div>
  );
}
