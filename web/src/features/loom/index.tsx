/*
 * The Loom — the first screen. One editorial sentence of vitals, the Loom itself
 * (every channel as a lane across the whole life, with a river toggle), then
 * Today, Moving now, Needs attention and Ask. Everything derives from the
 * bootstrap shape and one aggregate query; no channel or plugin is named here.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useBootstrap } from "@/api";
import { ErrorState, Skeleton } from "@/ui";
import { useChannelIndex } from "./useChannelIndex";
import { Vitals } from "./Vitals";
import { LoomChart } from "./LoomChart";
import { Today } from "./Today";
import { MovingNow } from "./MovingNow";
import { NeedsAttention } from "./NeedsAttention";
import { Ask } from "./Ask";
import styles from "./Loom.module.css";

export default function LoomScreen(): ReactNode {
  const boot = useBootstrap();
  const index = useChannelIndex();
  const loomHeight = useLoomHeight();

  if (boot.isLoading || !index) {
    return (
      <div className={styles.page}>
        <Skeleton height={34} />
        <Skeleton height={loomHeight} />
      </div>
    );
  }
  if (boot.isError || !boot.data) {
    return (
      <div className={styles.page}>
        <ErrorState error={boot.error} title="Could not load the archive" />
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.vitals}>
        <Vitals boot={boot.data} />
      </header>

      <LoomChart index={index} height={loomHeight} />

      <section className={styles.below} aria-label="Today, arrivals, attention and ask">
        <div className={styles.colLeft}>
          <Today index={index} />
          <Ask index={index} />
        </div>
        <div className={styles.colMid}>
          <MovingNow index={index} />
        </div>
        <div className={styles.colRight}>
          <NeedsAttention index={index} />
        </div>
      </section>
    </div>
  );
}

function useLoomHeight(): number {
  const [vh, setVh] = useState(() => (typeof window === "undefined" ? 900 : window.innerHeight));
  useEffect(() => {
    const on = (): void => setVh(window.innerHeight);
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  return useMemo(() => Math.round(Math.max(440, Math.min(820, vh - 228))), [vh]);
}
