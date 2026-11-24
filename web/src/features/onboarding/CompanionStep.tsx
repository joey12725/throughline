import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Monitor, Apple, Download, Copy, Check, Laptop } from "lucide-react";
import { Skeleton, Button, CodeBlock, IconButton } from "@/ui";
import { useCompanion, useCatalog, pairCompanion, setupProblem, type CompanionPairing, type SetupProblem } from "@/api/setup";
import { useWizard } from "./wizard";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

function detectOS(): "macos" | "windows" | "linux" {
  const ua = navigator.userAgent.toLowerCase();
  if (ua.includes("mac")) return "macos";
  if (ua.includes("win")) return "windows";
  return "linux";
}

const OS_ICON = { macos: Apple, windows: Monitor, linux: Laptop } as const;

export function CompanionStep(): ReactNode {
  const wiz = useWizard();
  const companion = useCompanion();
  const catalog = useCatalog();

  const needed = useMemo(() => {
    const picked = new Set(wiz.selectedSources);
    return (catalog.data?.plugins ?? []).some((p) => picked.has(p.id) && p.requires.includes("companion"));
  }, [catalog.data?.plugins, wiz.selectedSources]);

  const os = useMemo(detectOS, []);
  const [pairing, setPairing] = useState<CompanionPairing | null>(null);
  const [problem, setProblem] = useState<SetupProblem | null>(null);
  const [pairingBusy, setPairingBusy] = useState(false);

  const devices = companion.data?.devices ?? [];
  const baseline = useRef<number | null>(null);
  useEffect(() => {
    if (baseline.current === null && companion.data) baseline.current = devices.length;
  }, [companion.data, devices.length]);
  const newDevice = baseline.current !== null && devices.length > baseline.current ? devices[devices.length - 1] : null;

  const generate = async (): Promise<void> => {
    setPairingBusy(true);
    setProblem(null);
    try {
      setPairing(await pairCompanion("This computer"));
      baseline.current = devices.length;
    } catch (e) {
      setProblem(setupProblem(e));
    } finally {
      setPairingBusy(false);
    }
  };

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Your desktop app"
        title="Bring your own machine into the loop"
        lede={
          needed
            ? "Some of the sources you picked read from your own computer — your editor logs, local app data — so they run through a small companion app. Install it, then pair it with a one-time code."
            : "The companion app is optional. It lets Throughline sync sources that live on your computer, like local app data. Install it any time you need one."
        }
      />

      <div className={styles.body}>
        {companion.isLoading ? (
          <Skeleton height={160} />
        ) : companion.data ? (
          <>
            <div>
              <div className={styles.sectionLabel}>1 · Install the companion</div>
              <div className={styles.osButtons}>
                {companion.data.downloads.map((d) => {
                  const Icon = OS_ICON[d.os];
                  const highlight = d.os === os;
                  return (
                    <a key={d.os} href={d.url} target="_blank" rel="noreferrer noopener">
                      <Button variant={highlight ? "primary" : "default"} icon={<Icon size={15} />}>
                        {d.label}
                      </Button>
                    </a>
                  );
                })}
              </div>
              <p className={styles.repoBoxNote} style={{ marginTop: "var(--s-3)" }}>Or install it from a terminal:</p>
              <CodeBlock code={companion.data.install_command} wrap />
            </div>

            <div>
              <div className={styles.sectionLabel}>2 · Pair it with this hub</div>
              {!companion.data.owner_ready ? (
                <Callout tone="warn">
                  {wiz.state?.owner_hint ?? "This server can't mint pairing codes right now. Pair from the box with the command the server printed."}
                </Callout>
              ) : newDevice ? (
                <div className={styles.deviceConnected}>
                  <Check size={16} /> Connected: <strong>{newDevice.name}</strong>
                </div>
              ) : pairing ? (
                <>
                  <div className={styles.pairCode}>
                    <span className={styles.pairCodeValue}>{pairing.code}</span>
                    <IconButton label="Copy code" onClick={() => void navigator.clipboard?.writeText(pairing.code)}>
                      <Copy size={16} />
                    </IconButton>
                  </div>
                  <p className={styles.repoBoxNote} style={{ marginTop: "var(--s-3)" }}>Paste it into the app, or run:</p>
                  <CodeBlock code={pairing.command} wrap />
                  <div className={styles.waiting} role="status" aria-live="polite">
                    <span className={styles.pulse} aria-hidden /> Waiting for your computer…
                  </div>
                </>
              ) : (
                <Button variant="primary" icon={<Download size={15} />} onClick={() => void generate()} disabled={pairingBusy}>
                  {pairingBusy ? "Generating…" : "Generate pairing code"}
                </Button>
              )}
              {problem && <div style={{ marginTop: "var(--s-3)" }}><ProblemView problem={problem} /></div>}
            </div>

            {devices.length > 0 && !newDevice && (
              <Callout tone="info">
                {devices.length === 1 ? "1 device is" : `${devices.length} devices are`} already paired with this hub.
              </Callout>
            )}
          </>
        ) : null}
      </div>

      <WizardFooter skipLabel={needed ? "Skip for now" : "Skip"} />
    </div>
  );
}
