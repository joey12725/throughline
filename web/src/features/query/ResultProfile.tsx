/*
 * The Profile tab: one card per column — role, type, fill rate, distinct count and a
 * small distribution — plus the engine's plain-language reading of the whole result.
 * Everything comes from the lens engine's `profileResult`/`describeResult`, so it is
 * the same statistics the charts rank on.
 */
import { useMemo, type ReactNode } from "react";
import { Sensitive } from "@/ui";
import { describeResult, inferRoles, profileResult, type FieldProfile, type RoleMap } from "@/lens";
import type { SqlResult } from "@/api/types";
import styles from "./query.module.css";

function pct(x: number | null): string {
  if (x === null) return "—";
  return `${Math.round(x * 100)}%`;
}

function Distribution({ f }: { f: FieldProfile }): ReactNode {
  if (f.numeric) {
    const { min, max, avg } = f.numeric;
    const span = max - min || 1;
    const at = ((avg - min) / span) * 100;
    return (
      <div className={styles.dist}>
        <div className={styles.distBar}>
          <span className={styles.distAvg} style={{ left: `${Math.max(0, Math.min(100, at))}%` }} />
        </div>
        <div className={styles.distScale}>
          <span className="tnum">{min.toLocaleString("en-US")}</span>
          <span className="tnum">avg {avg.toLocaleString("en-US", { maximumFractionDigits: 2 })}</span>
          <span className="tnum">{max.toLocaleString("en-US")}</span>
        </div>
      </div>
    );
  }
  if (f.boolean) {
    return (
      <div className={styles.dist}>
        <div className={styles.distBar}>
          <span className={styles.distFill} style={{ width: `${f.boolean.true_share * 100}%` }} />
        </div>
        <div className={styles.distScale}><span>true {pct(f.boolean.true_share)}</span></div>
      </div>
    );
  }
  if (f.text?.top?.length) {
    const max = f.text.top[0]?.n || 1;
    return (
      <ul className={styles.topList}>
        {f.text.top.slice(0, 5).map((t) => (
          <li key={t.value}>
            <span className={styles.topBar} style={{ width: `${(t.n / max) * 100}%` }} aria-hidden />
            <Sensitive className={styles.topVal}>{t.value || "∅"}</Sensitive>
            <span className={`tnum ${styles.topN}`}>{t.n.toLocaleString("en-US")}</span>
          </li>
        ))}
      </ul>
    );
  }
  if (f.time) {
    return (
      <div className={styles.distScale}>
        <span className="tnum">{f.time.min?.slice(0, 10) ?? "—"}</span>
        <span>→</span>
        <span className="tnum">{f.time.max?.slice(0, 10) ?? "—"}</span>
      </div>
    );
  }
  if (f.json?.keys?.length) {
    return <div className={styles.jsonKeys}>{f.json.keys.slice(0, 8).map((k) => <code key={k}>{k}</code>)}</div>;
  }
  return <div className={styles.distScale}><span>{f.distinct !== null ? `${f.distinct.toLocaleString("en-US")} distinct` : "—"}</span></div>;
}

export function ResultProfile({ result }: { result: SqlResult }): ReactNode {
  const { profile, roles, narrative } = useMemo(() => {
    try {
      const p = profileResult(result.rows.slice(0, 2000) as never, result.columns as never, result.truncated);
      const r: RoleMap = inferRoles(p);
      return { profile: p, roles: r, narrative: describeResult(p) };
    } catch {
      return { profile: null, roles: {} as RoleMap, narrative: [] as string[] };
    }
  }, [result]);

  if (!profile) return <div className={styles.resultEmpty}>Could not profile this result.</div>;

  return (
    <div className={styles.profileTab}>
      {narrative.length > 0 && (
        <div className={styles.narrative}>
          {narrative.map((n, i) => (
            <p key={i}>{n}</p>
          ))}
        </div>
      )}
      <div className={styles.profileGrid}>
        {profile.columns.map((f) => (
          <div key={f.name} className={styles.profileCard}>
            <div className={styles.profileHead}>
              <span className={styles.profileName}>{f.name}</span>
              <span className={styles.profileRole}>{roles[f.name]?.role ?? f.kind}</span>
            </div>
            <div className={styles.profileMeta}>
              <span>{f.pg_type ?? f.kind}</span>
              <span>fill {pct(f.fill)}</span>
              {f.distinct !== null && <span>{f.distinct.toLocaleString("en-US")} distinct</span>}
            </div>
            <Distribution f={f} />
            {f.description ? <p className={styles.profileDesc}>{f.description}</p> : null}
          </div>
        ))}
      </div>
    </div>
  );
}
