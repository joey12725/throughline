/*
 * The rate strip — arrivals per plugin over a shared ten-minute window, so the
 * eye can compare sources at a glance. Every plugin's bars share one y-scale
 * (the busiest bucket across all plugins is full height), the window is labelled
 * once, and a plugin that just received something pulses its own dot (never the
 * label). A total-per-minute counter leads. Reduced motion stills the pulse.
 */
import { memo, useMemo, type ReactNode } from "react";
import { useSourceColor } from "@/ui";
import type { PluginRate } from "./liveStream";
import styles from "./RateStrip.module.css";

const BINS = 30; // display bars (the 60 ten-second buckets, binned by pairs)

export function RateStrip({
  plugins,
  totalPerMin,
  now,
}: {
  plugins: PluginRate[];
  totalPerMin: number;
  now: number;
}): ReactNode {
  // One shared scale across every plugin, so bar heights are comparable.
  const binned = useMemo(() => plugins.map((p) => ({ p, bars: binBuckets(p.buckets, BINS) })), [plugins]);
  const sharedMax = useMemo(() => {
    let m = 1;
    for (const { bars } of binned) for (const v of bars) if (v > m) m = v;
    return m;
  }, [binned]);

  return (
    <div className={styles.strip}>
      <div className={styles.lead}>
        <div className={styles.total}>
          <span className={styles.totalNum}>{totalPerMin}</span>
          <span className={styles.totalUnit}>/min</span>
        </div>
        <div className={styles.window}>last 10 min</div>
      </div>
      <div className={styles.rows}>
        {binned.length === 0 ? (
          <span className={styles.idle}>
            <span className={styles.idleDot} aria-hidden />
            Listening for arrivals…
          </span>
        ) : (
          binned.map(({ p, bars }) => <RateRow key={p.plugin} p={p} bars={bars} max={sharedMax} now={now} />)
        )}
      </div>
    </div>
  );
}

const RateRow = memo(function RateRow({
  p,
  bars,
  max,
  now,
}: {
  p: PluginRate;
  bars: number[];
  max: number;
  now: number;
}): ReactNode {
  const { cssFor } = useSourceColor();
  const color = cssFor(p.plugin, 0);
  const fresh = now - p.last < 1800;
  const w = 132;
  const h = 24;
  const gap = 1;
  const bw = (w - gap * (bars.length - 1)) / bars.length;

  return (
    <div className={styles.row} data-testid="rate-row" style={{ ["--c" as string]: color }}>
      <span className={styles.dot} data-fresh={fresh} aria-hidden />
      <span className={styles.name} title={p.plugin}>
        {p.plugin}
      </span>
      <svg className={styles.spark} width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden>
        <line x1={0} y1={h - 0.5} x2={w} y2={h - 0.5} stroke="var(--line)" strokeWidth={1} />
        {bars.map((v, i) => {
          const bh = v > 0 ? Math.max(1.5, (v / max) * (h - 2)) : 0;
          if (bh === 0) return null;
          return (
            <rect
              key={i}
              x={i * (bw + gap)}
              y={h - bh}
              width={bw}
              height={bh}
              rx={bw > 2 ? 1 : 0}
              fill="var(--c)"
              opacity={0.42 + 0.58 * (v / max)}
            />
          );
        })}
      </svg>
      <span className={styles.perMin} title={`${p.perMin} in the last minute`}>
        {p.perMin}
        <span className={styles.perMinUnit}>/m</span>
      </span>
    </div>
  );
});

/** Sum the per-10-second buckets down to `bins` display bars (oldest → newest). */
function binBuckets(buckets: readonly number[], bins: number): number[] {
  const out = new Array(bins).fill(0);
  if (buckets.length === 0) return out;
  const per = buckets.length / bins;
  for (let i = 0; i < buckets.length; i++) {
    const bi = Math.min(bins - 1, Math.floor(i / per));
    out[bi] += buckets[i] ?? 0;
  }
  return out;
}
