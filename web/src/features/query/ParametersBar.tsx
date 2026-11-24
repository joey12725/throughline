/*
 * The parameters bar: an input for each `:param` the statement needs that the global
 * scope did not supply. Values are typed best-effort (a number, a date, else text) and
 * substituted as escaped literals before the statement runs.
 */
import type { ReactNode } from "react";
import { SlidersHorizontal } from "lucide-react";
import { Input } from "@/ui";
import styles from "./query.module.css";

export interface ParametersBarProps {
  params: string[];
  values: Record<string, string>;
  onChange: (name: string, value: string) => void;
}

export function ParametersBar({ params, values, onChange }: ParametersBarProps): ReactNode {
  if (params.length === 0) return null;
  return (
    <div className={styles.paramBar}>
      <span className={styles.paramLead}>
        <SlidersHorizontal size={13} aria-hidden /> parameters
      </span>
      {params.map((p) => (
        <label key={p} className={styles.paramField}>
          <span className={styles.paramName}>:{p}</span>
          <Input
            mono
            value={values[p] ?? ""}
            placeholder="value"
            onChange={(e) => onChange(p, e.target.value)}
            aria-label={`Value for :${p}`}
            className={styles.paramInput}
          />
        </label>
      ))}
    </div>
  );
}
