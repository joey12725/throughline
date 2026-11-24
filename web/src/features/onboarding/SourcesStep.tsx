import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Search, FolderGit2, Check, Plus, Star, Loader2, Monitor, Upload, Container } from "lucide-react";
import { Skeleton, Field, Input, Button, Badge, Chip, ErrorState } from "@/ui";
import {
  useCatalog,
  useDetection,
  useAddRepo,
  setupProblem,
  type CatalogPlugin,
  type DetectedSource,
  type SetupProblem,
} from "@/api/setup";
import { useWizard } from "./wizard";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { PluginIcon } from "./PluginIcon";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

export function SourcesStep(): ReactNode {
  const catalog = useCatalog();
  const detect = useDetection();
  const wiz = useWizard();

  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string>("all");
  const [newIds, setNewIds] = useState<Set<string>>(new Set());
  const pendingSnapshot = useRef<Set<string> | null>(null);

  const detected = useMemo(() => {
    const m = new Map<string, DetectedSource>();
    for (const s of detect.data?.sources ?? []) m.set(s.plugin_id, s);
    return m;
  }, [detect.data]);

  const selected = new Set(wiz.selectedSources);

  const toggle = (p: CatalogPlugin): void => {
    const det = detected.get(p.id);
    if (det?.connected) return; // locked on
    const next = new Set(selected);
    if (next.has(p.id)) next.delete(p.id);
    else next.add(p.id);
    wiz.setChoices({ sources: Array.from(next) });
  };

  const plugins = catalog.data?.plugins ?? [];
  const connectedCount = useMemo(
    () => (detect.data?.sources ?? []).filter((s) => s.connected).length,
    [detect.data],
  );
  const newSelectedCount = useMemo(
    () => Array.from(selected).filter((id) => !detected.get(id)?.connected).length,
    [selected, detected],
  );

  // When a repo is added, highlight the plugins that appeared.
  useEffect(() => {
    const snap = pendingSnapshot.current;
    if (!snap) return;
    const appeared = plugins.filter((p) => !snap.has(p.id)).map((p) => p.id);
    if (appeared.length > 0) {
      pendingSnapshot.current = null;
      setNewIds(new Set(appeared));
      const t = window.setTimeout(() => setNewIds(new Set()), 3000);
      return () => window.clearTimeout(t);
    }
    return undefined;
  }, [plugins]);

  const q = query.trim().toLowerCase();
  const match = (p: CatalogPlugin): boolean => {
    if (q && !(p.name.toLowerCase().includes(q) || p.summary.toLowerCase().includes(q) || p.tags.some((t) => t.toLowerCase().includes(q)))) return false;
    if (category === "popular") return p.starter;
    if (category !== "all") return p.category === category;
    return true;
  };
  const filtered = plugins.filter(match);
  const showSections = category === "all" && !q;
  const popular = plugins.filter((p) => p.starter);

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="What you use"
        title="Choose your sources"
        lede="Pick the services you actually use — most people need only a handful. You can always add more later. Search or browse by category for the long tail."
      />

      <div className={styles.body}>
        {catalog.isLoading ? (
          <div className={styles.loadingStack}>
            <Skeleton height={44} />
            <Skeleton height={200} />
          </div>
        ) : catalog.error ? (
          <ErrorState error={catalog.error} title="Could not load the catalog" onRetry={() => void catalog.refetch()} />
        ) : (
          <>
            {connectedCount > 0 && (
              <Callout tone="info">
                <strong>{connectedCount}</strong> {connectedCount === 1 ? "source is" : "sources are"} already connected and preserved. Pick any additional sources below to add now without touching what's already set up.
              </Callout>
            )}

            <div className={styles.catalogTools}>
              <Field label="Search sources" htmlFor="src-search" className={styles.searchWrap}>
                <Input id="src-search" lead={<Search size={15} />} placeholder="Search by name, service or tag…" value={query} onChange={(e) => setQuery(e.target.value)} />
              </Field>
              <div className={styles.chips}>
                <Chip active={category === "all"} onClick={() => setCategory("all")}>All</Chip>
                <Chip active={category === "popular"} onClick={() => setCategory("popular")} lead={<Star size={12} />}>Popular</Chip>
                {(catalog.data?.categories ?? []).map((c) => (
                  <Chip key={c.id} active={category === c.id} onClick={() => setCategory(c.id)}>{c.label}</Chip>
                ))}
              </div>
            </div>

            {(catalog.data?.problems ?? []).length > 0 && (
              <Callout tone="warn">
                Some repositories had problems:
                <ul className={styles.problemList} style={{ marginTop: "var(--s-2)" }}>
                  {catalog.data!.problems!.map((p, i) => (
                    <li key={i}><code>{p.repo_id}</code> — {p.message}</li>
                  ))}
                </ul>
              </Callout>
            )}

            {showSections ? (
              <>
                {popular.length > 0 && (
                  <>
                    <div className={styles.sectionLabel}><Star size={13} /> Popular</div>
                    <Grid plugins={popular} selected={selected} detected={detected} onToggle={toggle} newIds={newIds} />
                  </>
                )}
                <div className={styles.sectionLabel}>All sources</div>
                <Grid plugins={plugins} selected={selected} detected={detected} onToggle={toggle} newIds={newIds} />
              </>
            ) : filtered.length > 0 ? (
              <Grid plugins={filtered} selected={selected} detected={detected} onToggle={toggle} newIds={newIds} />
            ) : (
              <div className={styles.empty}>No sources match “{query}”.</div>
            )}

            <AddRepoBox onAddStart={() => { pendingSnapshot.current = new Set(plugins.map((p) => p.id)); }} />
          </>
        )}
      </div>

      <WizardFooter
        continueLabel={
          newSelectedCount > 0
            ? connectedCount > 0
              ? `Continue with ${newSelectedCount} new`
              : `Continue with ${newSelectedCount}`
            : "Continue"
        }
      />
    </div>
  );
}

function Grid({
  plugins,
  selected,
  detected,
  onToggle,
  newIds,
}: {
  plugins: CatalogPlugin[];
  selected: Set<string>;
  detected: Map<string, DetectedSource>;
  onToggle: (p: CatalogPlugin) => void;
  newIds?: Set<string>;
}): ReactNode {
  return (
    <div className={styles.catalogGrid}>
      {plugins.map((p) => (
        <SourceCard key={p.id} plugin={p} selected={selected.has(p.id)} detected={detected.get(p.id)} onToggle={() => onToggle(p)} isNew={newIds?.has(p.id)} />
      ))}
    </div>
  );
}

function SourceCard({
  plugin,
  selected,
  detected,
  onToggle,
  isNew,
}: {
  plugin: CatalogPlugin;
  selected: boolean;
  detected?: DetectedSource;
  onToggle: () => void;
  isNew?: boolean;
}): ReactNode {
  const connected = Boolean(detected?.connected);
  const on = connected || selected;

  const badges: ReactNode[] = [];
  if (plugin.requires.includes("companion")) badges.push(<Badge key="c" tone="neutral"><Monitor size={11} /> Desktop app</Badge>);
  if (plugin.requires.includes("upload")) badges.push(<Badge key="u" tone="neutral"><Upload size={11} /> Upload</Badge>);
  if (plugin.requires.includes("docker")) badges.push(<Badge key="d" tone="neutral"><Container size={11} /> Docker</Badge>);
  if (plugin.backfill.history === "full") badges.push(<Badge key="h" tone="neutral">Full history</Badge>);
  else if (plugin.backfill.history === "window") badges.push(<Badge key="w" tone="neutral">Recent window</Badge>);

  return (
    <button
      type="button"
      className={styles.srcCard}
      data-selected={on}
      data-locked={connected}
      data-new={isNew || undefined}
      aria-pressed={on}
      onClick={onToggle}
      disabled={connected}
    >
      <div className={styles.srcTop}>
        <span className={styles.srcIcon}><PluginIcon name={plugin.icon} size={18} /></span>
        <span className={styles.srcName}>{plugin.name}</span>
        {on && <Check size={16} className={styles.srcCheck} style={{ color: connected ? "var(--ok)" : "var(--brass)" }} aria-hidden />}
      </div>
      <div className={styles.srcSummary}>{plugin.summary}</div>
      {connected ? (
        <div className={styles.srcConnected}>
          <Check size={12} /> Already connected · {detected!.records.toLocaleString()} records
        </div>
      ) : (
        badges.length > 0 && <div className={styles.srcBadges}>{badges}</div>
      )}
    </button>
  );
}

function AddRepoBox({ onAddStart }: { onAddStart: () => void }): ReactNode {
  const addRepo = useAddRepo();
  const [url, setUrl] = useState("");
  const [problem, setProblem] = useState<SetupProblem | null>(null);
  const [added, setAdded] = useState<string | null>(null);

  const submit = (): void => {
    const u = url.trim();
    if (!u) return;
    setProblem(null);
    setAdded(null);
    onAddStart();
    addRepo.mutate(u, {
      onSuccess: (repo) => {
        setUrl("");
        setAdded(repo.name);
      },
      onError: (e) => setProblem(setupProblem(e)),
    });
  };

  return (
    <div className={styles.repoBox}>
      <div className={styles.repoBoxHead}><FolderGit2 size={15} /> Add from GitHub</div>
      <p className={styles.repoBoxNote}>
        Paste a repository URL to add its connectors. A repository may contain several plugins and must follow the plugin onboarding contract
        (<code>docs/plugin-onboarding-contract.md</code>). Throughline reads its manifest before running any of its code.
      </p>
      <div className={styles.repoForm}>
        <Field label="Repository URL" htmlFor="repo-url">
          <Input id="repo-url" mono placeholder="https://github.com/you/throughline-plugins" value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } }} />
        </Field>
        <Button variant="default" icon={addRepo.isPending ? <Loader2 size={15} className={styles.spin} /> : <Plus size={15} />} onClick={submit} disabled={addRepo.isPending || !url.trim()} style={{ alignSelf: "flex-end" }}>
          {addRepo.isPending ? "Adding…" : "Add"}
        </Button>
      </div>
      {added && <Callout tone="info">Added <strong>{added}</strong>. Its new plugins are highlighted in the grid above.</Callout>}
      {problem && <div style={{ marginTop: "var(--s-3)" }}><ProblemView problem={problem} /></div>}
    </div>
  );
}
