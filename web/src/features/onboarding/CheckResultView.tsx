import { type ReactNode } from "react";
import { CheckCircle2, XCircle } from "lucide-react";
import type { CheckResult } from "@/api/setup";
import styles from "./Onboarding.module.css";

/** Inline result of a "Test connection" / plugin check: green pass or red card. */
export function CheckResultView({ result }: { result: CheckResult }): ReactNode {
  return (
    <div className={styles.check} data-ok={result.ok} role="status" aria-live="polite">
      <span className={styles.checkIcon} aria-hidden>
        {result.ok ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
      </span>
      <div className={styles.checkBody}>
        <div className={styles.checkTitle}>
          {result.title}
          {typeof result.duration_ms === "number" && (
            <span className={styles.checkLatency}>{Math.round(result.duration_ms)} ms</span>
          )}
        </div>
        {result.message && <div className={styles.checkMsg}>{result.message}</div>}
        {result.hint && <div className={styles.checkHint}>{result.hint}</div>}
      </div>
    </div>
  );
}
