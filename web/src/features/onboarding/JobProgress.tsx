/*
 * The live backfill view. Polls GET /v1/setup/jobs (1s) and draws an overall
 * meter plus one row per source: phase, a determinate bar when the fraction is
 * known (else a shimmer), record/point counts with the window reached, rate and
 * ETA. Failed rows expand to the SetupProblem, the log tail and a Retry button.
 */
import { useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, XCircle, Clock, Loader2, Ban, RotateCw, ChevronRight } from "lucide-react";
import { useJobs, cancelJob, retryJob, type Job } from "@/api/setup";
import { PluginIcon } from "./PluginIcon";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

function monthYear(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

function fmtNum(n: number): string {
  return n.toLocaleString();
}

function fmtEta(s: number | null): string | null {
  if (s == null || s <= 0) return null;
  if (s < 60) return `~${Math.round(s)}s left`;
  if (s < 3600) return `~${Math.round(s / 60)}m left`;
  return `~${Math.round(s / 3600)}h left`;
}

function StatusMark({ status }: { status: Job["status"] }): ReactNode {
  switch (status) {
    case "done":
      return <CheckCircle2 size={16} style={{ color: "var(--ok)" }} aria-label="done" />;
    case "failed":
      return <XCircle size={16} style={{ color: "var(--bad)" }} aria-label="failed" />;
    case "cancelled":
    case "interrupted":
      return <Ban size={16} style={{ color: "var(--ink-3)" }} aria-label={status} />;
    case "queued":
      return <Clock size={16} style={{ color: "var(--ink-3)" }} aria-label="queued" />;
    default:
      return <Loader2 size={16} className={styles.spin} style={{ color: "var(--brass)" }} aria-label="running" />;
  }
}

export function JobProgress({ kinds }: { kinds?: Array<Job["kind"]> }): ReactNode {
  const qc = useQueryClient();
  const { data } = useJobs(true);
  const all = data?.jobs ?? [];
  const jobs = kinds ? all.filter((j) => kinds.includes(j.kind)) : all;
  const summary = data?.summary;

  const refresh = (): void => void qc.invalidateQueries({ queryKey: ["setup", "jobs"] });

  if (jobs.length === 0) {
    return <div className={styles.empty}>No imports running yet.</div>;
  }

  const overall = summary?.fraction ?? null;
  const delivered = summary?.delivered ?? jobs.reduce((n, j) => n + j.progress.delivered, 0);
  const running = summary?.running ?? jobs.filter((j) => j.status === "running").length;

  return (
    <div aria-live="polite">
      <div className={styles.importOverall}>
        <div className={styles.importOverallHead}>
          <div>
            <div className={styles.importOverallNum}>{fmtNum(delivered)}</div>
            <div className={styles.importOverallSub}>records imported so far</div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div className={styles.importOverallSub}>
              {running > 0 ? `${running} source${running === 1 ? "" : "s"} importing` : "All imports settled"}
            </div>
            {overall != null && <div className="tnum" style={{ fontSize: "var(--t-sm)", color: "var(--ink)" }}>{Math.round(overall * 100)}%</div>}
          </div>
        </div>
        <div className={`${styles.bar} ${overall == null && running > 0 ? styles.barIndeterminate : ""}`}>
          {overall != null && <div className={styles.barFill} style={{ width: `${Math.round(overall * 100)}%` }} />}
        </div>
      </div>

      <div className={styles.jobRows}>
        {jobs.map((job) => (
          <JobRow key={job.id} job={job} onChanged={refresh} />
        ))}
      </div>
    </div>
  );
}

function JobRow({ job, onChanged }: { job: Job; onChanged: () => void }): ReactNode {
  const [expanded, setExpanded] = useState(job.status === "failed");
  const p = job.progress;
  const frac = p.fraction;
  const oldest = monthYear(p.oldest);
  const newest = monthYear(p.newest);
  const eta = fmtEta(p.eta_s);
  const active = job.status === "running" || job.status === "queued";
  const failed = job.status === "failed";

  const metrics: string[] = [];
  if (p.delivered > 0) metrics.push(`${fmtNum(p.delivered)} records`);
  if (p.tracks > 0) metrics.push(`${fmtNum(p.tracks)} points`);
  if (oldest && newest) metrics.push(`${oldest} – ${newest}`);
  else if (oldest) metrics.push(`back to ${oldest}`);
  if (p.rate_per_s) metrics.push(`${fmtNum(Math.round(p.rate_per_s))}/s`);

  return (
    <div className={styles.jobRow}>
      <div className={styles.jobRowTop}>
        <span className={styles.jobIcon}>
          <PluginIcon name={job.plugin_id ?? "database"} size={16} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className={styles.jobName}>{job.title}</div>
          {job.phase && <div className={styles.jobPhase}>{job.phase}</div>}
        </div>
        {eta && active && <span className="tnum" style={{ fontSize: "var(--t-xs)", color: "var(--ink-3)" }}>{eta}</span>}
        <StatusMark status={job.status} />
      </div>

      {(metrics.length > 0 || active) && (
        <div className={styles.jobBarWrap}>
          {active && (
            <div className={`${styles.bar} ${frac == null ? styles.barIndeterminate : ""}`}>
              {frac != null && <div className={styles.barFill} style={{ width: `${Math.round(frac * 100)}%` }} />}
            </div>
          )}
          {metrics.length > 0 && (
            <div className={styles.jobMetrics}>
              {metrics.map((m, i) => (
                <span key={i} className="tnum">{m}</span>
              ))}
            </div>
          )}
        </div>
      )}

      {failed && (
        <div className={styles.jobError}>
          {job.error && <ProblemView problem={job.error} />}
          {job.log_tail.length > 0 && (
            <>
              <button type="button" className={styles.advancedToggle} onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
                <ChevronRight size={14} style={{ transform: expanded ? "rotate(90deg)" : "none" }} aria-hidden />
                Log
              </button>
              {expanded && <pre className={styles.logTail}>{job.log_tail.join("\n")}</pre>}
            </>
          )}
          <div className={styles.cardActions} style={{ marginTop: "var(--s-3)" }}>
            <span className={styles.footerSpacer} />
            <button type="button" className={styles.advancedToggle} onClick={() => void retryJob(job.id).then(onChanged)}>
              <RotateCw size={14} aria-hidden /> Retry
            </button>
          </div>
        </div>
      )}

      {active && (
        <div className={styles.cardActions} style={{ marginTop: "var(--s-2)" }}>
          <span className={styles.footerSpacer} />
          <button type="button" className={styles.advancedToggle} onClick={() => void cancelJob(job.id).then(onChanged)}>
            <Ban size={14} aria-hidden /> Cancel
          </button>
        </div>
      )}
    </div>
  );
}
