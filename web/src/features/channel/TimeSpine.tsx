/*
 * The always-present time spine: records per bucket across the channel's whole
 * life (its SQL carries no :from/:to, so the global scope never shrinks it),
 * coverage gaps hatched by the chart layer, and a brush that sets the global
 * time scope. A brushed scope shows as a brass-framed window over the band.
 */
import type { ReactNode } from "react";
import { X } from "lucide-react";
import { ChartPanel } from "@/charts";
import { useScopes, formatInTz } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import type { LensCandidate } from "@/lens";
import styles from "./TimeSpine.module.css";

export function TimeSpine({ spine }: { spine: LensCandidate | undefined }): ReactNode {
  const { from, to, clearRange } = useScopes();
  const tz = usePrefs((s) => s.tz);
  if (!spine) return null;
  const scoped = !!(from || to);
  return (
    <div className={styles.spine}>
      <div className={styles.label}>
        <span className={styles.labelText}>Whole life · brush to set the time scope</span>
        {scoped ? (
          <button type="button" className={styles.scopeChip} onClick={clearRange}>
            {from ? formatInTz(from, tz, { dateStyle: "medium" }) : "start"} – {to ? formatInTz(to, tz, { dateStyle: "medium" }) : "now"}
            <X size={12} />
          </button>
        ) : null}
      </div>
      <ChartPanel spec={spine.spec} height={120} board="channel-spine" priority="visible" className={styles.panel} />
    </div>
  );
}
