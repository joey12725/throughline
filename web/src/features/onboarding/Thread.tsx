import { type ReactNode } from "react";
import styles from "./Onboarding.module.css";

/*
 * The throughline motif: a single brass thread stitched down the rail, echoing
 * the app icon. It draws itself on mount (stroke-dashoffset) and then rests.
 * Purely decorative — aria-hidden — and still under prefers-reduced-motion.
 */
export function Thread(): ReactNode {
  return (
    <svg
      className={styles.asideThread}
      viewBox="0 0 320 900"
      preserveAspectRatio="xMidYMin slice"
      fill="none"
      aria-hidden
    >
      <defs>
        <linearGradient id="tl-thread" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--brass)" stopOpacity="0" />
          <stop offset="18%" stopColor="var(--brass)" stopOpacity="0.5" />
          <stop offset="82%" stopColor="var(--brass)" stopOpacity="0.22" />
          <stop offset="100%" stopColor="var(--brass)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path
        d="M44 -20 C 44 60, 40 100, 44 140 S 49 220, 44 280 S 39 360, 44 420 S 49 500, 44 560 S 40 640, 44 700 S 48 820, 44 1040"
        stroke="url(#tl-thread)"
        strokeWidth="1.5"
        strokeLinecap="round"
        style={{
          strokeDasharray: 2000,
          strokeDashoffset: 2000,
          animation: "tl-thread-draw 2200ms var(--ease) forwards",
        }}
      />
      <style>{`@keyframes tl-thread-draw { to { stroke-dashoffset: 0; } }
        @media (prefers-reduced-motion: reduce) {
          .${styles.asideThread} path { animation: none !important; stroke-dashoffset: 0 !important; }
        }`}</style>
    </svg>
  );
}
