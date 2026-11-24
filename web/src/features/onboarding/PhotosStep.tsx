import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Images, Ban, Check } from "lucide-react";
import { Skeleton, Button } from "@/ui";
import {
  useProviders,
  useSaveProvider,
  testProvider,
  setupProblem,
  type CheckResult,
  type SetupProblem,
} from "@/api/setup";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { FieldGroup, fromProviderField } from "./FieldGroup";
import { CheckResultView } from "./CheckResultView";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

export function PhotosStep(): ReactNode {
  const providers = useProviders();
  const immich = useMemo(() => providers.data?.providers.find((p) => p.id === "immich" || p.roles.includes("images")), [providers.data]);

  const [mode, setMode] = useState<"connect" | "skip" | null>(() => (immich?.configured ? "connect" : null));
  const [values, setValues] = useState<Record<string, string>>({});
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [problem, setProblem] = useState<SetupProblem | null>(null);
  const [testing, setTesting] = useState(false);
  const saveProvider = useSaveProvider();

  useEffect(() => {
    if (mode === null && immich?.configured) setMode("connect");
  }, [mode, immich?.configured]);

  const fields = useMemo(
    () => (immich ? immich.fields.map((f) => fromProviderField(f, immich.locked.includes(f.key))) : []),
    [immich],
  );

  const payload = (): Record<string, string> => {
    const out: Record<string, string> = {};
    if (!immich) return out;
    for (const f of immich.fields) {
      if (immich.locked.includes(f.key)) continue;
      const v = values[f.key] ?? "";
      if (f.kind === "secret" && v === "") continue;
      if (v === "") continue;
      out[f.key] = v;
    }
    return out;
  };

  const doTest = async (): Promise<void> => {
    if (!immich) return;
    setTesting(true);
    setProblem(null);
    setCheck(null);
    try {
      setCheck(await testProvider(immich.id, payload()));
    } catch (e) {
      setProblem(setupProblem(e));
    } finally {
      setTesting(false);
    }
  };

  const doSave = (): void => {
    if (!immich) return;
    setProblem(null);
    const p = payload();
    if (Object.keys(p).length > 0) saveProvider.mutate({ id: immich.id, values: p }, { onError: (e) => setProblem(setupProblem(e)) });
  };

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Your image library"
        title="Bring your photos into the picture"
        lede="Connect your own Immich server and Throughline can line your photos up with everything else — the message you sent that afternoon, where you were, who you were with. Your images stay on your server; Throughline only reads them."
      />

      <div className={styles.body}>
        {providers.isLoading ? (
          <Skeleton height={120} />
        ) : !immich ? (
          <Callout tone="info">Photo support isn't available on this server yet. You can skip this and add it later.</Callout>
        ) : (
          <>
            <div className={styles.cards} role="radiogroup" aria-label="Photos">
              <button type="button" role="radio" aria-checked={mode === "connect"} className={styles.choice} data-selected={mode === "connect"} onClick={() => setMode("connect")}>
                <span className={styles.choiceIcon}><Images size={20} /></span>
                <span className={styles.choiceText}>
                  <span className={styles.choiceTitle}>Connect my Immich</span>
                  <span className={styles.choiceDesc}>Point Throughline at your Immich instance with a URL and an API key.</span>
                </span>
                {mode === "connect" && <Check size={18} className={styles.choiceMark} aria-hidden />}
              </button>
              <button type="button" role="radio" aria-checked={mode === "skip"} className={styles.choice} data-selected={mode === "skip"} onClick={() => setMode("skip")}>
                <span className={styles.choiceIcon}><Ban size={20} /></span>
                <span className={styles.choiceText}>
                  <span className={styles.choiceTitle}>Not now</span>
                  <span className={styles.choiceDesc}>Skip photos for now — you can connect Immich any time from Settings.</span>
                </span>
                {mode === "skip" && <Check size={18} className={styles.choiceMark} aria-hidden />}
              </button>
            </div>

            {mode === "connect" && (
              <div className={styles.providerConfig}>
                <FieldGroup fields={fields} values={values} onChange={(k, v) => { setValues((s) => ({ ...s, [k]: v })); setCheck(null); }} masked={immich.values} />
                {check && <div style={{ marginTop: "var(--s-3)" }}><CheckResultView result={check} /></div>}
                {problem && <div style={{ marginTop: "var(--s-3)" }}><ProblemView problem={problem} /></div>}
                <div className={styles.cardActions} style={{ marginTop: "var(--s-4)" }}>
                  <span className={styles.footerSpacer} />
                  <Button variant="ghost" onClick={() => void doTest()} disabled={testing}>{testing ? "Testing…" : "Test connection"}</Button>
                  <Button variant="primary" onClick={doSave} disabled={saveProvider.isPending}>{saveProvider.isPending ? "Saving…" : "Save"}</Button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <WizardFooter skipLabel="Skip" />
    </div>
  );
}
