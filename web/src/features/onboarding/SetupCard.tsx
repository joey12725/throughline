/*
 * One plugin's setup card: description, numbered steps, fields, and the
 * Install / Save / Test actions. Reused by the Connect step (accordion) and the
 * Settings → Plugins panel. Third-party (non-builtin) repos show a short trust
 * warning before install, since installing runs their code on the server.
 */
import { useMemo, useState, type ReactNode } from "react";
import { ChevronRight, Download, ShieldAlert, Loader2 } from "lucide-react";
import { Badge, Button, Field, Switch } from "@/ui";
import {
  useSavePluginConfig,
  useInstallPlugin,
  checkPlugin,
  uploadExport,
  setupProblem,
  type CatalogPlugin,
  type CheckResult,
  type Job,
  type SetupProblem,
} from "@/api/setup";
import { PluginIcon } from "./PluginIcon";
import { FieldGroup, fromSetupField } from "./FieldGroup";
import { CheckResultView } from "./CheckResultView";
import { ProblemView } from "./ProblemView";
import { Callout } from "./parts";
import styles from "./Onboarding.module.css";

export interface StatusPill {
  tone: "ok" | "warn" | "bad" | "neutral";
  label: string;
}

export function pluginPill(p: CatalogPlugin, checked: boolean): StatusPill {
  const s = p.status;
  if (s.last_error) return { tone: "bad", label: "Error" };
  if (s.connected) return { tone: "ok", label: "Connected" };
  if (checked) return { tone: "ok", label: "Checked" };
  if (!s.installed) return s.installable ? { tone: "warn", label: "Install needed" } : { tone: "neutral", label: "Unavailable" };
  if (!s.configured) return { tone: "warn", label: "Needs setup" };
  if (s.enabled) return { tone: "ok", label: "Ready" };
  return { tone: "neutral", label: "Off" };
}

function seedValues(p: CatalogPlugin): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of p.setup.fields) {
    const existing = p.status.values[f.key];
    if (f.kind === "secret") out[f.key] = "";
    else if (existing != null && existing !== "") out[f.key] = existing;
    else if (f.default != null) out[f.key] = f.default;
    else out[f.key] = "";
  }
  return out;
}

export interface SetupCardProps {
  plugin: CatalogPlugin;
  repoBuiltin: boolean;
  open: boolean;
  onToggle: () => void;
  /** the live install job for this plugin, when the parent polls jobs */
  installJob?: Job | null;
  /** turn the plugin on when its config saves (the connect flow); default true */
  enableOnSave?: boolean;
  /** show an explicit on/off switch in the body (the Settings panel) */
  showEnableToggle?: boolean;
}

export function SetupCard({ plugin, repoBuiltin, open, onToggle, installJob, enableOnSave = true, showEnableToggle = false }: SetupCardProps): ReactNode {
  const [values, setValues] = useState<Record<string, string>>(() => seedValues(plugin));
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [problem, setProblem] = useState<SetupProblem | null>(null);
  const [checked, setChecked] = useState(false);
  const [testing, setTesting] = useState(false);
  const [confirmInstall, setConfirmInstall] = useState(false);

  const save = useSavePluginConfig();
  const install = useInstallPlugin();

  const fields = useMemo(() => plugin.setup.fields.map(fromSetupField), [plugin.setup.fields]);
  const pill = pluginPill(plugin, checked);
  const needsCompanion = plugin.requires.includes("companion");
  const installing = install.isPending || (installJob != null && (installJob.status === "running" || installJob.status === "queued"));

  const onChange = (key: string, value: string): void => {
    setValues((v) => ({ ...v, [key]: value }));
    setChecked(false);
  };

  const payload = (): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const f of plugin.setup.fields) {
      const v = values[f.key] ?? "";
      if (f.kind === "secret" && v === "") continue; // keep the saved secret
      if (v === "" && !f.required && f.kind !== "boolean") continue;
      out[f.key] = v;
    }
    return out;
  };

  const doSave = (): void => {
    setProblem(null);
    save.mutate(
      { id: plugin.id, values: payload(), enabled: enableOnSave ? true : undefined },
      { onError: (e) => setProblem(setupProblem(e)) },
    );
  };

  const doTest = async (): Promise<void> => {
    setTesting(true);
    setProblem(null);
    setCheck(null);
    try {
      const r = await checkPlugin(plugin.id, payload());
      setCheck(r);
      setChecked(r.ok);
    } catch (e) {
      setProblem(setupProblem(e));
    } finally {
      setTesting(false);
    }
  };

  const doInstall = (): void => {
    if (!repoBuiltin && !confirmInstall) {
      setConfirmInstall(true);
      return;
    }
    setConfirmInstall(false);
    setProblem(null);
    install.mutate(plugin.id, { onError: (e) => setProblem(setupProblem(e)) });
  };

  const uploader = (key: string, file: File, onProgress: (f: number) => void): Promise<string> =>
    uploadExport(plugin.id, key, file, onProgress).then((u) => u.path);

  return (
    <div className={styles.setupCard} data-open={open}>
      <button type="button" className={styles.setupCardHead} onClick={onToggle} aria-expanded={open}>
        <span className={styles.setupCardIcon}>
          <PluginIcon name={plugin.icon} size={18} />
        </span>
        <span className={styles.setupCardText}>
          <span className={styles.setupCardName}>{plugin.name}</span>
          <span className={styles.setupCardSummary}>{plugin.summary}</span>
        </span>
        <Badge tone={pill.tone}>{pill.label}</Badge>
        <ChevronRight size={16} className={styles.setupCardChevron} aria-hidden />
      </button>

      {open && (
        <div className={styles.setupCardBody}>
          {showEnableToggle && plugin.status.installed && (
            <Field label="Enabled" inline hint="Sync on a schedule">
              <Switch
                checked={plugin.status.enabled}
                onChange={(next) => save.mutate({ id: plugin.id, values: {}, enabled: next })}
                label={`Enable ${plugin.name}`}
              />
            </Field>
          )}
          <p style={{ margin: 0, fontSize: "var(--t-sm)", color: "var(--ink-2)", lineHeight: "var(--lh-normal)" }}>{plugin.description}</p>

          {needsCompanion && (
            <Callout tone="info">
              This source runs through the <strong>desktop app</strong> (the companion) — you'll set it up in the Companion step, then it syncs from your own machine.
            </Callout>
          )}

          {plugin.setup.steps.length > 0 && (
            <ol className={styles.setupSteps}>
              {plugin.setup.steps.map((s, i) => (
                <li key={i} className={styles.setupStep}>
                  <span>
                    <strong>{s.title}</strong>
                    {s.body ? <> — {s.body}</> : null}
                    {s.url && (
                      <>
                        {" "}
                        <a href={s.url} target="_blank" rel="noreferrer noopener">
                          Open ↗
                        </a>
                      </>
                    )}
                  </span>
                </li>
              ))}
            </ol>
          )}

          {fields.length > 0 && (
            <FieldGroup fields={fields} values={values} onChange={onChange} masked={plugin.status.values} uploader={uploader} />
          )}

          {!plugin.status.installed && !plugin.status.installable && plugin.status.install_hint && (
            <Callout tone="warn">{plugin.status.install_hint}</Callout>
          )}

          {confirmInstall && !repoBuiltin && (
            <Callout tone="warn" icon={ShieldAlert}>
              Installing runs code from this repository on your server. Only continue if you trust its source.
            </Callout>
          )}

          {check && <CheckResultView result={check} />}
          {problem && <ProblemView problem={problem} />}
          {plugin.status.last_error && !problem && (
            <Callout tone="warn">
              <strong>Last error:</strong> {plugin.status.last_error}
            </Callout>
          )}

          {installJob && (installJob.status === "running" || installJob.status === "queued") && (
            <div className={styles.jobBarWrap}>
              <div className={styles.jobPhase}>{installJob.phase ?? "Installing…"}</div>
              <div className={`${styles.bar} ${styles.barIndeterminate}`} style={{ marginTop: 6 }} />
            </div>
          )}

          <div className={styles.cardActions}>
            {!plugin.status.installed && plugin.status.installable && (
              <Button variant={confirmInstall ? "primary" : "default"} icon={installing ? <Loader2 size={15} className={styles.spin} /> : <Download size={15} />} onClick={doInstall} disabled={installing}>
                {installing ? "Installing…" : confirmInstall ? "Install anyway" : "Install"}
              </Button>
            )}
            <span className={styles.footerSpacer} />
            {/* Every installed plugin can be tested: its own `check`, else a
                read-only dry run on the server. */}
            {plugin.status.installed && (
              <Button variant="ghost" onClick={() => void doTest()} disabled={testing}>
                {testing ? "Testing…" : "Test"}
              </Button>
            )}
            <Button variant="primary" onClick={doSave} disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
