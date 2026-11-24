/*
 * The key to the freshness matrix: how a filled day, a hole, a reported gap and
 * an inferred quiet day each read. Small, legible in both themes.
 */
import type { ReactNode } from "react";
import styles from "./Sources.module.css";

export function MatrixLegend(): ReactNode {
  return (
    <div className={styles.legend} aria-hidden>
      <span className={styles.legendItem}>
        <span className={styles.swatchActive} /> records
      </span>
      <span className={styles.legendItem}>
        <span className={styles.swatchHole} /> missing
      </span>
      <span className={styles.legendItem}>
        <span className={styles.swatchReported} /> reported gap
      </span>
      <span className={styles.legendItem}>
        <span className={styles.swatchQuiet} /> quiet
      </span>
    </div>
  );
}
