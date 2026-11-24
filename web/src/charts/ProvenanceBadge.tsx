/*
 * The provenance badge: who built this picture (the engine, the helper agent, or
 * the owner) and the honesty flags — sampled, predicted from declared fields,
 * truncated, or re-run from a wrapped statement. Honest about what it knows.
 */
import type { ReactNode } from "react";
import { Bot, Cpu, User } from "lucide-react";
import type { ViewSpec } from "@/lens";
import styles from "./ProvenanceBadge.module.css";

export function ProvenanceBadge({ spec, sampled, truncated }: { spec: ViewSpec; sampled?: boolean; truncated?: boolean }): ReactNode {
  const by = spec.provenance.by;
  const Icon = by === "agent" ? Bot : by === "user" ? User : Cpu;
  const byLabel = by === "agent" ? "helper" : by === "user" ? "you" : "engine";
  return (
    <span className={styles.badge}>
      <span className={styles.by} title={`Built by the ${byLabel}`}>
        <Icon size={12} aria-hidden /> {byLabel}
      </span>
      {spec.provenance.predicted && <span className={styles.flag} title="Built from declared fields; no records yet">predicted</span>}
      {sampled && <span className={styles.flag} title="Value statistics were sampled; counts stay exact">sampled</span>}
      {spec.provenance.statement === "wrapped" && <span className={styles.flag} title="The statement was wrapped in an aggregate and re-run">re-run</span>}
      {truncated && <span className={`${styles.flag} ${styles.warn}`} title="The result was cut off at the row cap">truncated</span>}
    </span>
  );
}
