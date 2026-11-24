/*
 * Audit — what tokens have been asking. The core keeps every request in its
 * audit log but exposes no query for it; the arrivals route surfaces a "reads"
 * lane (the recent answered reads, owner-only, web traffic excluded), which is
 * what we show here: filterable, with activity binned by hour. It is the recent
 * tail, not the whole history — stated plainly rather than faked as complete.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";
import { fetchArrivals } from "@/api";
import { useChartColors } from "@/charts";
import { EmptyState, ErrorState, Input, Skeleton } from "@/ui";
import { formatInTz } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import { hourOfDayBins } from "./timeline";
import styles from "./ledger.module.css";

interface ReadRow {
  at: string;
  token: string | number;
  asker: string | null;
  statement: string;
  row_count: number | null;
  status: string | number;
}

export default function AuditPanel() {
  const tz = usePrefs((s) => s.tz);
  const c = useChartColors("");
  const q = useQuery({ queryKey: ["ledger", "audit"], queryFn: ({ signal }) => fetchArrivals({ limit: 1, signal }), staleTime: 15_000 });
  const [term, setTerm] = useState("");
  const [onlyErrors, setOnlyErrors] = useState(false);

  const reads = (q.data?.reads ?? []) as ReadRow[];
  const filtered = useMemo(() => {
    const t = term.trim().toLowerCase();
    return reads.filter((r) => {
      if (onlyErrors && Number(r.status) < 400) return false;
      if (!t) return true;
      return r.statement.toLowerCase().includes(t) || String(r.token).toLowerCase().includes(t) || (r.asker ?? "").toLowerCase().includes(t);
    });
  }, [reads, term, onlyErrors]);

  const bins = useMemo(() => hourOfDayBins(reads.map((r) => Date.parse(r.at)), tz), [reads, tz]);
  const binMax = Math.max(1, ...bins);

  if (q.isLoading) return <Skeleton height={300} />;
  if (q.error) return <ErrorState error={q.error} />;
  if (reads.length === 0) return <EmptyState icon={<Activity />} title="No reads to show" description="The reads lane is owner-only and excludes the interface's own traffic — nothing recent to report." />;

  return (
    <div className={styles.audit}>
      <div className={styles.auditHeat} aria-label="Activity by hour of day">
        {bins.map((v, h) => (
          <span key={h} className={styles.heatBar} title={`${h}:00 — ${v}`} style={{ height: `${6 + (v / binMax) * 34}px`, background: v ? c.tokens.patina : c.tokens.bg3 }} />
        ))}
      </div>
      <div className={styles.auditControls}>
        <Input value={term} onChange={(e) => setTerm(e.target.value)} placeholder="Filter by statement, token or asker…" aria-label="Filter audit" />
        <label className={styles.auditCheck}>
          <input type="checkbox" checked={onlyErrors} onChange={(e) => setOnlyErrors(e.target.checked)} /> errors only
        </label>
      </div>

      <div className={styles.auditTable} role="table">
        <div className={styles.auditHeadRow} role="row">
          <span role="columnheader">Time</span>
          <span role="columnheader">Token · asker</span>
          <span role="columnheader">Statement</span>
          <span role="columnheader">Rows</span>
          <span role="columnheader">Status</span>
        </div>
        {filtered.map((r, i) => (
          <div key={i} className={styles.auditRow} role="row">
            <span role="cell" className={styles.auditTime}>{formatInTz(r.at, tz, { dateStyle: "short", timeStyle: "medium" })}</span>
            <span role="cell" className={styles.auditTok}>{String(r.token)}{r.asker ? ` · ${r.asker}` : ""}</span>
            <span role="cell" className={styles.auditStmt} title={r.statement}>{r.statement}</span>
            <span role="cell" className={styles.auditNum}>{r.row_count ?? "—"}</span>
            <span role="cell" className={styles.auditStatus} data-bad={Number(r.status) >= 400}>{r.status}</span>
          </div>
        ))}
      </div>
      <p className={styles.note}>Showing the {reads.length} most recent answered reads (owner tokens only; the interface's own queries are excluded). The full audit log has no query route.</p>
    </div>
  );
}
