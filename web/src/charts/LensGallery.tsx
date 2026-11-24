/*
 * <LensGallery /> — a hero panel plus a ranked grid of the remaining candidates.
 * Panels mount only as they scroll into view (IntersectionObserver), so a long
 * ranking does not fire dozens of statements at once; the scheduler then bounds
 * the rest.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { LensCandidate } from "@/lens";
import { Skeleton } from "@/ui";
import { ChartPanel } from "./Panel";
import styles from "./LensGallery.module.css";

export interface LensGalleryProps {
  candidates: readonly LensCandidate[];
  /** Pin board the panels save into. */
  board?: string;
  /** Hide the first candidate as a large hero. */
  hero?: boolean;
}

export function LensGallery({ candidates, board = "default", hero = true }: LensGalleryProps): ReactNode {
  if (candidates.length === 0) {
    return <div className={styles.empty}>No pictures ranked for this data yet.</div>;
  }
  const [first, ...rest] = candidates;
  return (
    <div className={styles.gallery}>
      {hero && first && (
        <LazyMount className={styles.hero} minHeight={360}>
          <ChartPanel spec={first.spec} height={340} board={board} priority="visible" />
        </LazyMount>
      )}
      <div className={styles.grid}>
        {(hero ? rest : candidates).map((c, i) => (
          <LazyMount key={`${c.spec.lens}-${i}`} minHeight={300} className={styles.cell}>
            <ChartPanel spec={c.spec} height={240} board={board} priority="normal" />
          </LazyMount>
        ))}
      </div>
    </div>
  );
}

/** Mounts its children only once scrolled near the viewport. */
export function LazyMount({ children, minHeight, className }: { children: ReactNode; minHeight: number; className?: string }): ReactNode {
  const ref = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || shown) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setShown(true);
          io.disconnect();
        }
      },
      { rootMargin: "300px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [shown]);
  return (
    <div ref={ref} className={className} style={{ minHeight }}>
      {shown ? children : <Skeleton height={minHeight - 20} radius={10} />}
    </div>
  );
}
