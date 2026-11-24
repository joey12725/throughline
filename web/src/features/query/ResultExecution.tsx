/*
 * The Execution tab: the server's own envelope — status, truncation, stage timings,
 * warnings, cost gate and trace id. When the cost gate refuses, it points at the
 * approval flow rather than pretending the result is complete.
 */
import type { ReactNode } from "react";
import { AlertTriangle, ShieldAlert } from "lucide-react";
import { Badge } from "@/ui";
import type { ExecutionEnvelope, SqlResult } from "@/api/types";
import styles from "./query.module.css";

function Row({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <div className={styles.execRow}>
      <span className={styles.execLabel}>{label}</span>
      <span className={styles.execValue}>{children}</span>
    </div>
  );
}

export function ResultExecution({ result }: { result: SqlResult }): ReactNode {
  const e: ExecutionEnvelope = result.execution;
  const timings = e.timings_ms ?? {};
  const timingEntries = Object.entries(timings);
  const approval = e.status === "approval_required" || e.approval;

  return (
    <div className={styles.execTab}>
      {approval ? (
        <div className={styles.gateBanner}>
          <ShieldAlert size={16} aria-hidden />
          <div>
            <strong>This statement hit the cost gate.</strong>
            <p>A semantic or expensive statement needs approval before it runs. Approve it from the box (the CLI/approvals flow); the editor does not spend on your behalf.</p>
          </div>
        </div>
      ) : null}

      <div className={styles.execBlock}>
        <Row label="status">
          <Badge tone={e.status === "complete" ? "ok" : e.status === "failed" ? "bad" : "warn"}>{e.status}</Badge>
        </Row>
        <Row label="rows">
          <span className="tnum">{result.rowCount.toLocaleString("en-US")}{result.truncated ? "  (truncated)" : ""}</span>
        </Row>
        {e.truncation ? (
          <Row label="truncation">
            <span>{e.truncation.reason} · limit {e.truncation.limit.toLocaleString("en-US")}</span>
          </Row>
        ) : null}
        {e.completeness ? <Row label="completeness">{e.completeness}</Row> : null}
        {e.trace_id ? <Row label="trace"><code>{e.trace_id}</code></Row> : null}
      </div>

      {timingEntries.length > 0 && (
        <div className={styles.execBlock}>
          <div className={styles.execHeading}>Timings</div>
          {timingEntries.map(([k, v]) => (
            <Row key={k} label={k}>
              <span className="tnum">{Number(v).toLocaleString("en-US")} ms</span>
            </Row>
          ))}
        </div>
      )}

      {Array.isArray(e.warnings) && e.warnings.length > 0 && (
        <div className={styles.execBlock}>
          <div className={styles.execHeading}>Warnings</div>
          {e.warnings.map((w, i) => (
            <div key={i} className={styles.execWarn}>
              <AlertTriangle size={13} aria-hidden /> {w}
            </div>
          ))}
        </div>
      )}

      {Array.isArray(e.stages) && e.stages.length > 0 && (
        <div className={styles.execBlock}>
          <div className={styles.execHeading}>Stages</div>
          <pre className={styles.execJson}>{JSON.stringify(e.stages, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
