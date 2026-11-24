import { type ReactNode } from "react";
import { Check, Gauge, Rabbit, Scale, PiggyBank } from "lucide-react";
import type { Pace, PaceId } from "@/api/setup";
import styles from "./Onboarding.module.css";

const PACE_ICON: Record<string, typeof Gauge> = { live: Rabbit, balanced: Scale, thrifty: PiggyBank };

/**
 * The speed-vs-cost control shared by the Lenses and Pages steps. Each pace is a
 * card with a "how up to date" and a "cost per run" meter, normalised across the
 * three paces, plus how far back each run reads first.
 */
export function PaceControl({ paces, value, onChange }: { paces: Pace[]; value: PaceId; onChange: (id: PaceId) => void }): ReactNode {
  const maxCalls = Math.max(1, ...paces.map((p) => p.calls_per_day));
  const maxCost = Math.max(0.0001, ...paces.map((p) => p.max_cost_per_run));

  return (
    <div className={styles.paceCards} role="radiogroup" aria-label="How often lenses and pages update">
      {paces.map((p) => {
        const Icon = PACE_ICON[p.id] ?? Gauge;
        const selected = value === p.id;
        return (
          <button
            key={p.id}
            type="button"
            role="radio"
            aria-checked={selected}
            className={styles.paceCard}
            data-selected={selected}
            onClick={() => onChange(p.id)}
          >
            <div className={styles.paceHead}>
              <span className={styles.paceIcon}><Icon size={15} /></span>
              <span className={styles.paceLabel}>{p.label}</span>
              {selected && <Check size={14} style={{ color: "var(--brass)" }} aria-hidden />}
            </div>
            <div className={styles.paceDesc}>{p.description}</div>
            <div className={styles.paceMeters}>
              <Meter label="Up to date" fraction={p.calls_per_day / maxCalls} tone="patina" />
              <Meter label="Cost / run" fraction={p.max_cost_per_run / maxCost} tone="brass" note={p.max_cost_per_run === 0 ? "free" : `~$${p.max_cost_per_run.toFixed(2)}`} />
            </div>
            <div className={styles.paceLookback}>Reads the last {p.lookback_days} days first</div>
          </button>
        );
      })}
    </div>
  );
}

function Meter({ label, fraction, tone, note }: { label: string; fraction: number; tone: "brass" | "patina"; note?: string }): ReactNode {
  const pct = Math.max(4, Math.min(100, Math.round(fraction * 100)));
  return (
    <div className={styles.meter}>
      <div className={styles.meterHead}>
        <span>{label}</span>
        {note && <span className={styles.meterNote}>{note}</span>}
      </div>
      <div className={styles.meterTrack}>
        <div className={styles.meterFill} data-tone={tone} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
