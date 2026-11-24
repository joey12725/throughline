import type { ReactNode } from "react";
import { Skeleton } from "@/ui";
import styles from "./RouteFallback.module.css";

/** The loading state shown while a lazy route chunk resolves. */
export function RouteFallback(): ReactNode {
  return (
    <div className={styles.wrap} aria-busy>
      <Skeleton width={180} height={22} />
      <div className={styles.grid}>
        <Skeleton height={120} radius={10} />
        <Skeleton height={120} radius={10} />
        <Skeleton height={120} radius={10} />
      </div>
      <Skeleton lines={4} />
    </div>
  );
}
