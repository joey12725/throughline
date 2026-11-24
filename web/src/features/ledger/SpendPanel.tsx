/*
 * Spend — the model/embedding provider ledger. The /v1/spend route returns
 * lifetime totals and a `by` breakdown (purpose × provider × model), not a daily
 * series, so we draw where the tokens and cost went by provider and by purpose
 * rather than inventing a per-day axis we do not have. Owner-only.
 */
import { useMemo, useState } from "react";
import { Coins } from "lucide-react";
import { useChartColors } from "@/charts";
import { EmptyState, ErrorState, Segmented, Skeleton, Stat } from "@/ui";
import { formatInTz } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import { useSpend } from "./data";
import { aggregateSpend } from "./timeline";
import styles from "./ledger.module.css";

export default function SpendPanel({ enabled }: { enabled: boolean }) {
  const tz = usePrefs((s) => s.tz);
  const c = useChartColors("");
  const q = useSpend(enabled);
  const [dim, setDim] = useState<"provider" | "purpose" | "model">("purpose");

  const by = q.data?.by ?? [];
  const slices = useMemo(() => aggregateSpend(by, dim), [by, dim]);
  const maxTok = slices.reduce((m, s) => Math.max(m, s.tokens), 1);
  const colours = c.categorical(Math.max(1, slices.length));

  if (q.isLoading) return <Skeleton height={260} />;
  if (q.error) return <ErrorState error={q.error} />;

  return (
    <div className={styles.spend}>
      <div className={styles.spendStats}>
        <Stat label="Total cost" value={`$${(q.data?.cost ?? 0).toFixed(4)}`} />
        <Stat label="Tokens" value={(q.data?.tokens ?? 0).toLocaleString("en-US")} />
        <Stat label="Model calls" value={(q.data?.calls ?? 0).toLocaleString("en-US")} />
      </div>

      {by.length === 0 ? (
        <EmptyState icon={<Coins />} title="No spend recorded" description="No model or embedding calls have been billed on this installation." />
      ) : (
        <>
          <div className={styles.spendHead}>
            <h3 className={styles.sectionH}>Where it went</h3>
            <Segmented aria-label="Break down by" size="sm" value={dim} onChange={setDim} items={[{ value: "purpose", label: "Purpose" }, { value: "provider", label: "Provider" }, { value: "model", label: "Model" }]} />
          </div>
          <ul className={styles.spendBars}>
            {slices.map((s, i) => (
              <li key={s.key} className={styles.spendRow}>
                <span className={styles.spendSwatch} style={{ background: colours[i % colours.length] }} aria-hidden />
                <span className={styles.spendLabel}>{s.key}</span>
                <span className={styles.spendTrack}><span className={styles.spendFill} style={{ width: `${(s.tokens / maxTok) * 100}%`, background: colours[i % colours.length] }} /></span>
                <span className={styles.spendTok}>{s.tokens.toLocaleString("en-US")} tok</span>
                <span className={styles.spendCost}>${s.cost.toFixed(4)}</span>
              </li>
            ))}
          </ul>

          <h3 className={styles.sectionH}>Operations</h3>
          <div className={styles.opTable} role="table">
            <div className={styles.opHeadRow} role="row">
              <span role="columnheader">Purpose</span>
              <span role="columnheader">Provider · model</span>
              <span role="columnheader">Calls</span>
              <span role="columnheader">Tokens</span>
              <span role="columnheader">Last</span>
            </div>
            {[...by].sort((a, b) => (b.tokens ?? 0) - (a.tokens ?? 0)).map((e, i) => (
              <div key={i} className={styles.opRow} role="row">
                <span role="cell">{e.purpose ?? "—"}</span>
                <span role="cell" className={styles.opProvider}>{e.provider ?? "—"} · {e.model ?? "—"}</span>
                <span role="cell" className={styles.opNum}>{(e.calls ?? 0).toLocaleString("en-US")}</span>
                <span role="cell" className={styles.opNum}>{(e.tokens ?? 0).toLocaleString("en-US")}</span>
                <span role="cell" className={styles.opLast}>{e.last_at ? formatInTz(e.last_at, tz, { dateStyle: "short", timeStyle: "short" }) : "—"}</span>
              </div>
            ))}
          </div>
          <p className={styles.note}>The ledger keeps lifetime totals, not a daily series — a per-day cost axis would be invented, so it is not drawn.</p>
        </>
      )}
    </div>
  );
}
