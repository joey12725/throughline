import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Sparkles, Archive, ArrowLeftRight, Check, Database, RotateCcw, Loader2 } from "lucide-react";
import { Skeleton, Badge, Button, Field, Input, Select, Switch } from "@/ui";
import {
  useDetection,
  useRestoreView,
  useApplyRestore,
  setupProblem,
  type RestoreScope,
  type RestoreResult,
  type SetupProblem,
  type SetupState,
} from "@/api/setup";
import { useWizard } from "./wizard";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { PluginIcon } from "./PluginIcon";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

type Mode = SetupState["mode"];

const MIGRATE_SYSTEMS = [
  { id: "gbrain", label: "GBrain" },
  { id: "hermes", label: "Hermes agent stack" },
  { id: "wave", label: "Wave / Beeper archives" },
];

const RESTORE_SCOPE_META: Array<{ id: RestoreScope; label: string; desc: string }> = [
  { id: "config", label: "Settings & credentials", desc: "Merge model keys & plugin settings without wiping existing values" },
  { id: "lenses", label: "Lenses", desc: "Restore custom lens definitions (~/.throughline/lenses)" },
  { id: "pages", label: "Pages", desc: "Restore pages.toml and page definitions" },
  { id: "database", label: "Archive database", desc: "Restore the PostgreSQL archive dump" },
];

export function WelcomeStep(): ReactNode {
  const wiz = useWizard();
  const detect = useDetection();
  const d = detect.data;

  const resumeHint = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get("resume") === "1";
  }, []);
  const returning = Boolean(d?.existing || d?.restored_hint || resumeHint);

  const [mode, setMode] = useState<Mode>(wiz.mode === "fresh" && returning ? "restore" : wiz.mode);
  const [systems, setSystems] = useState<string[]>(() => (Array.isArray(wiz.choices.migrate_from) ? (wiz.choices.migrate_from as string[]) : []));

  // Preselect restore when this looks like a returning / restored install.
  useEffect(() => {
    if (returning && wiz.mode === "fresh") setMode("restore");
  }, [returning, wiz.mode]);

  const connected = (d?.sources ?? []).filter((s) => s.connected);

  const pick = (m: Mode): void => {
    setMode(m);
    wiz.setMode(m);
  };

  const toggleSystem = (id: string): void => {
    const next = systems.includes(id) ? systems.filter((s) => s !== id) : [...systems, id];
    setSystems(next);
    wiz.setChoices({ migrate_from: next });
  };

  const paths: Array<{ id: Mode; icon: ReactNode; title: string; desc: string }> = [
    { id: "fresh", icon: <Sparkles size={20} />, title: "Start fresh", desc: "A new, empty hub. Pick what you use and bring your history in." },
    { id: "restore", icon: <Archive size={20} />, title: "Restoring a backup or continuing an install", desc: "Keep what's already configured, optionally restore selected parts of a backup, and continue adding sources." },
    { id: "migrate", icon: <ArrowLeftRight size={20} />, title: "Moving from another system", desc: "Bring your archive over from GBrain, a Hermes agent stack, or Wave/Beeper exports." },
  ];

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow={returning ? "Welcome back" : "Welcome to Throughline"}
        title={returning ? "Let's pick up where you left off" : "One hub for everything you've said and done"}
        lede={
          returning
            ? "This install already holds data. Continue your setup — anything already connected stays as it is, and you can add new sources or restore selected backup parts."
            : "Throughline gathers your messages, mail, calendar, places, health and more into one database your agents can search. Let's set it up — it takes a few minutes, and most steps are optional."
        }
      />

      <div className={styles.body}>
        <div className={styles.cards} role="radiogroup" aria-label="How would you like to start?">
          {paths.map((p) => (
            <button
              key={p.id}
              type="button"
              role="radio"
              aria-checked={mode === p.id}
              className={styles.choice}
              data-selected={mode === p.id}
              onClick={() => pick(p.id)}
            >
              <span className={styles.choiceIcon}>{p.icon}</span>
              <span className={styles.choiceText}>
                <span className={styles.choiceTitle}>{p.title}</span>
                <span className={styles.choiceDesc}>{p.desc}</span>
              </span>
              {mode === p.id && <Check size={18} className={styles.choiceMark} aria-hidden />}
            </button>
          ))}
        </div>

        {mode === "restore" && <PartialRestorePanel hasExisting={Boolean(d?.existing)} />}

        {mode === "migrate" && (
          <div>
            <div className={styles.sectionLabel}>Which system?</div>
            <div className={styles.chips}>
              {MIGRATE_SYSTEMS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className={styles.providerCard}
                  style={{ flex: "0 1 auto" }}
                  data-selected={systems.includes(s.id)}
                  onClick={() => toggleSystem(s.id)}
                  aria-pressed={systems.includes(s.id)}
                >
                  <span className={styles.providerName}>{s.label}</span>
                  {systems.includes(s.id) && <Check size={14} style={{ color: "var(--brass)" }} aria-hidden />}
                </button>
              ))}
            </div>
          </div>
        )}

        {detect.isLoading ? (
          <div className={styles.loadingStack}>
            <Skeleton height={44} />
          </div>
        ) : connected.length > 0 ? (
          <div>
            <div className={styles.sectionLabel}>Already connected</div>
            <div className={styles.detectList}>
              {connected.map((s) => (
                <div key={s.plugin_id} className={styles.detectItem}>
                  <span className={styles.setupCardIcon} style={{ width: 28, height: 28 }}>
                    <PluginIcon name={s.plugin_id} size={15} />
                  </span>
                  <span>{s.name}</span>
                  <span className={styles.footerSpacer} />
                  <span className={styles.detectRecords}>{s.records.toLocaleString()} records</span>
                  <Badge tone="ok">
                    <Check size={11} /> connected
                  </Badge>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>

      <WizardFooter hideSkip continueLabel={returning ? "Continue setup" : "Begin"} />
    </div>
  );
}

function PartialRestorePanel({ hasExisting }: { hasExisting: boolean }): ReactNode {
  const restoreQ = useRestoreView(true);
  const apply = useApplyRestore();
  const bundles = restoreQ.data?.bundles ?? [];

  const [bundleChoice, setBundleChoice] = useState<string>("");
  const [customPath, setCustomPath] = useState<string>("");
  const [scopes, setScopes] = useState<RestoreScope[]>(() => (hasExisting ? ["config", "lenses", "pages"] : ["database", "config", "lenses", "pages"]));
  const [overwrite, setOverwrite] = useState(false);
  const [result, setResult] = useState<RestoreResult | null>(null);
  const [problem, setProblem] = useState<SetupProblem | null>(null);

  useEffect(() => {
    if (!bundleChoice && bundles.length > 0) {
      setBundleChoice(bundles[0]!.name);
    }
  }, [bundleChoice, bundles]);

  const activeBundle = bundles.find((b) => b.name === bundleChoice) ?? null;
  const availableScopes = activeBundle?.available_scopes ?? restoreQ.data?.scopes ?? ["database", "config", "lenses", "pages"];
  const effectiveTarget = bundleChoice === "__custom__" ? customPath.trim() : bundleChoice;

  const toggleScope = (id: RestoreScope): void => {
    setScopes((prev) => (prev.includes(id) ? prev.filter((s) => s !== id) : [...prev, id]));
  };

  const selectedAvailable = scopes.filter((s) => availableScopes.includes(s));

  const runRestore = (): void => {
    setProblem(null);
    setResult(null);
    apply.mutate(
      {
        bundle: effectiveTarget || null,
        parts: selectedAvailable,
        overwrite,
        clean: false,
        continue_onboarding: true,
      },
      {
        onSuccess: (r) => setResult(r),
        onError: (e) => setProblem(setupProblem(e)),
      },
    );
  };

  return (
    <div className={styles.repoBox}>
      <div className={styles.repoBoxHead}>
        <RotateCcw size={15} /> Partial restore from backup (optional)
      </div>
      <p className={styles.repoBoxNote}>
        {hasExisting
          ? "Your restored archive is already loaded below. You can continue straight into onboarding to add new sources — or selectively restore config, lenses, or pages from a backup bundle without wiping what you already have."
          : "Pick a backup bundle on the server and choose which parts to restore, then continue through onboarding to add any additional sources."}
      </p>

      {restoreQ.isLoading ? (
        <Skeleton height={44} />
      ) : (
        <>
          <div className={styles.repoForm}>
            {bundles.length > 0 ? (
              <Field label="Backup bundle" htmlFor="restore-bundle">
                <Select
                  id="restore-bundle"
                  value={bundleChoice || bundles[0]!.name}
                  onChange={(e) => setBundleChoice(e.target.value)}
                >
                  {bundles.map((b) => (
                    <option key={b.name} value={b.name}>
                      {b.name} ({Math.max(1, Math.round(b.bytes / (1024 * 1024)))} MB · {b.available_scopes.join(", ")})
                    </option>
                  ))}
                  <option value="__custom__">Other path on server…</option>
                </Select>
              </Field>
            ) : (
              <Field
                label="Bundle path on server"
                htmlFor="restore-custom-path"
                hint={`No bundles found in ${restoreQ.data?.backup_dir ?? "~/.throughline/backups"}. Enter a bundle folder path or skip to continue with what is already here.`}
              >
                <Input
                  id="restore-custom-path"
                  mono
                  placeholder="/path/to/throughline-YYYYMMDDTHHMMSSZ-xxxxxx"
                  value={customPath}
                  onChange={(e) => {
                    setBundleChoice("__custom__");
                    setCustomPath(e.target.value);
                  }}
                />
              </Field>
            )}
          </div>

          {bundles.length > 0 && bundleChoice === "__custom__" && (
            <Field label="Server path" htmlFor="restore-custom-path-extra" className={styles.confidence}>
              <Input
                id="restore-custom-path-extra"
                mono
                placeholder="/path/to/throughline-YYYYMMDDTHHMMSSZ-xxxxxx"
                value={customPath}
                onChange={(e) => setCustomPath(e.target.value)}
              />
            </Field>
          )}

          <div style={{ marginTop: "var(--s-3)" }}>
            <div className={styles.sectionLabel}>
              <Database size={13} /> Parts to restore
            </div>
            <div className={styles.chips}>
              {RESTORE_SCOPE_META.map((item) => {
                const available = availableScopes.includes(item.id);
                const checked = available && scopes.includes(item.id);
                return (
                  <button
                    key={item.id}
                    type="button"
                    className={styles.providerCard}
                    style={{ flex: "1 1 200px" }}
                    data-selected={checked}
                    disabled={!available}
                    aria-pressed={checked}
                    onClick={() => toggleScope(item.id)}
                  >
                    <span style={{ flex: 1, minWidth: 0, textAlign: "left" }}>
                      <span className={styles.providerName}>{item.label}</span>
                      <span className={styles.providerTag}>{available ? item.desc : "Not in this bundle"}</span>
                    </span>
                    {checked && <Check size={14} style={{ color: "var(--brass)" }} aria-hidden />}
                  </button>
                );
              })}
            </div>
          </div>

          <div style={{ marginTop: "var(--s-3)" }}>
            <Field
              label="Overwrite existing settings & files"
              inline
              hint="Off by default: merges restored settings into your existing config without erasing what is already set"
            >
              <Switch checked={overwrite} onChange={setOverwrite} label="Overwrite existing settings" />
            </Field>
          </div>

          {selectedAvailable.includes("database") && restoreQ.data && !restoreQ.data.owner_ready && (
            <Callout tone="info">
              Database restore needs the owner database connection on the server. Leave <strong>Archive database</strong> unchecked to restore config/lenses/pages right here, or run{" "}
              <code>throughline lib backup restore {effectiveTarget || "<bundle>"} --only database --continue-onboarding</code> on the server.
            </Callout>
          )}

          <div className={styles.cardActions} style={{ marginTop: "var(--s-3)" }}>
            <span className={styles.footerSpacer} />
            <Button
              variant="default"
              icon={apply.isPending ? <Loader2 size={15} className={styles.spin} /> : <RotateCcw size={15} />}
              onClick={runRestore}
              disabled={apply.isPending || !effectiveTarget || selectedAvailable.length === 0}
            >
              {apply.isPending ? "Restoring…" : `Restore ${selectedAvailable.length} selected part${selectedAvailable.length === 1 ? "" : "s"}`}
            </Button>
          </div>

          {result && (
            <div style={{ marginTop: "var(--s-3)" }}>
              <Callout tone="info">
                Restored <strong>{result.scopes.join(", ") || "state"}</strong>
                {result.bundle ? <> from <code>{result.bundle}</code></> : null}. Existing sources were preserved — click <strong>Continue setup</strong> below to add more sources.
              </Callout>
            </div>
          )}
          {problem && (
            <div style={{ marginTop: "var(--s-3)" }}>
              <ProblemView problem={problem} />
            </div>
          )}
        </>
      )}
    </div>
  );
}
