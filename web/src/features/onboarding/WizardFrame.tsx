import { type ReactNode } from "react";
import { Check, Minus } from "lucide-react";
import type { StepId, StepStatus } from "@/api/setup";
import { STEPS, stepIndex } from "./steps";
import { Thread } from "./Thread";
import styles from "./Onboarding.module.css";

export interface WizardFrameProps {
  current: StepId;
  /** resolved status per step id */
  statusOf: (id: StepId) => StepStatus;
  progress: number;
  /** click a step in the stepper; omit for a static (pre-sign-in) frame */
  onPick?: (id: StepId) => void;
  onDefer?: () => void;
  children: ReactNode;
}

export function WizardFrame({ current, statusOf, progress, onPick, onDefer, children }: WizardFrameProps): ReactNode {
  const pct = Math.round(progress * 100);
  const idx = stepIndex(current);

  const nodeContent = (id: StepId, n: number): ReactNode => {
    const st = statusOf(id);
    if (st === "done") return <Check size={13} strokeWidth={3} aria-hidden />;
    if (st === "skipped") return <Minus size={13} strokeWidth={3} aria-hidden />;
    return n + 1;
  };

  return (
    <div className={styles.wizard}>
      <aside className={styles.aside} aria-label="Setup steps">
        <Thread />
        <div className={styles.brand}>
          <img className={styles.brandMark} src="/ui/icon.svg" alt="" width={40} height={40} />
          <div>
            <div className={styles.brandName}>Throughline</div>
            <p className={styles.brandTag}>Let's get you set up</p>
          </div>
        </div>

        <ol className={styles.stepper}>
          {STEPS.map((s, i) => {
            const isCurrent = s.id === current;
            const st = isCurrent ? "current" : statusOf(s.id);
            const done = statusOf(s.id) === "done";
            const clickable = Boolean(onPick) && (done || statusOf(s.id) === "skipped" || i <= idx);
            return (
              <li key={s.id} style={{ display: "contents" }}>
                <button
                  type="button"
                  className={styles.stepItem}
                  data-state={st}
                  data-done={done || undefined}
                  data-clickable={clickable || undefined}
                  aria-current={isCurrent ? "step" : undefined}
                  disabled={!clickable}
                  onClick={clickable ? () => onPick!(s.id) : undefined}
                >
                  <span className={styles.stepNode}>{nodeContent(s.id, i)}</span>
                  <span className={styles.stepLabel}>{s.label}</span>
                  {statusOf(s.id) === "skipped" && !isCurrent && <span className={styles.stepTag}>skipped</span>}
                </button>
              </li>
            );
          })}
        </ol>

        <div className={styles.progress}>
          <div className={styles.progressHead}>
            <span>Setup progress</span>
            <span className={styles.progressPct}>{pct}%</span>
          </div>
          <div className={styles.progressTrack} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Overall setup progress">
            <div className={styles.progressFill} style={{ width: `${pct}%` }} />
          </div>
          {onDefer && (
            <button type="button" className={styles.defer} onClick={onDefer}>
              Set up later
            </button>
          )}
        </div>
      </aside>

      {/* Mobile: the rail collapses to a top progress bar */}
      <div className={styles.topbar}>
        <div className={styles.topbarRow}>
          <img src="/ui/icon.svg" alt="" width={28} height={28} />
          <span className={styles.topbarName}>Throughline setup</span>
          <span className={styles.topbarStep}>{idx + 1} / {STEPS.length}</span>
        </div>
        <div className={styles.progressTrack} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Overall setup progress">
          <div className={styles.progressFill} style={{ width: `${pct}%` }} />
        </div>
      </div>

      <main className={styles.main} id="main" tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}
