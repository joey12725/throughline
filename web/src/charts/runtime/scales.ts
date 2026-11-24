/*
 * Small pure scale helpers. We lean on d3 for the heavy charts, but the geometry
 * every renderer shares — nice ticks, a linear map, a band map, grain-aligned
 * time ticks — lives here so it can be unit-tested without a DOM.
 */

export function extent(values: Iterable<number>): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (lo === Infinity) return [0, 1];
  if (lo === hi) return lo === 0 ? [0, 1] : [Math.min(0, lo), Math.max(0, hi) || lo * 2];
  return [lo, hi];
}

/** "Nice" rounded tick values across [min,max], about `count` of them. */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) {
    return [min || 0];
  }
  const span = max - min;
  const raw = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 7.5 ? 10 : norm >= 3 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
  const start = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = start; v <= max + step * 1e-6; v += step) {
    out.push(Math.round(v / step) * step);
  }
  return out;
}

export interface LinearScale {
  (x: number): number;
  invert(px: number): number;
  ticks(count?: number): number[];
  domain: [number, number];
  range: [number, number];
}

/** A linear map from a data domain to a pixel range. */
export function linear(domain: [number, number], range: [number, number]): LinearScale {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  const fn = ((x: number) => r0 + ((x - d0) / span) * (r1 - r0)) as LinearScale;
  fn.invert = (px: number) => d0 + ((px - r0) / (r1 - r0 || 1)) * span;
  fn.ticks = (count = 5) => niceTicks(d0, d1, count);
  fn.domain = domain;
  fn.range = range;
  return fn;
}

export interface BandScale {
  (key: string): number;
  bandwidth: number;
  step: number;
  domain: string[];
  /** Centre of a band. */
  center(key: string): number;
}

/** A band map over an ordered set of keys. */
export function band(domain: string[], range: [number, number], padding = 0.1): BandScale {
  const [r0, r1] = range;
  const n = Math.max(1, domain.length);
  const total = r1 - r0;
  const step = total / n;
  const bandwidth = step * (1 - padding);
  const inset = (step - bandwidth) / 2;
  const index = new Map(domain.map((k, i) => [k, i]));
  const fn = ((key: string) => r0 + (index.get(key) ?? 0) * step + inset) as BandScale;
  fn.bandwidth = bandwidth;
  fn.step = step;
  fn.domain = domain;
  fn.center = (key: string) => fn(key) + bandwidth / 2;
  return fn;
}

const GRAIN_MS: Record<string, number> = {
  minute: 60_000,
  hour: 3_600_000,
  day: 86_400_000,
  week: 604_800_000,
  month: 2_592_000_000,
  quarter: 7_776_000_000,
  year: 31_536_000_000,
};

export function grainMs(grain: string | null | undefined): number {
  return GRAIN_MS[grain ?? "day"] ?? GRAIN_MS.day!;
}

/**
 * Tick positions (ms) across a time range, aligned to a grain-ish step and
 * thinned to about `maxTicks`. Returns instants in ascending order.
 */
export function timeTicks(minMs: number, maxMs: number, grain: string | null | undefined, maxTicks = 7): number[] {
  if (!Number.isFinite(minMs) || !Number.isFinite(maxMs) || maxMs <= minMs) return [minMs];
  const base = grainMs(grain);
  const span = maxMs - minMs;
  let step = base;
  // Widen the step until we are at or under maxTicks.
  const steps = [1, 2, 3, 6, 12, 24, 48, 96, 168, 336, 720, 2160, 8760];
  const hour = 3_600_000;
  for (const h of steps) {
    step = Math.max(base, h * hour);
    if (span / step <= maxTicks) break;
  }
  if (span / step > maxTicks) step = span / maxTicks;
  const start = Math.ceil(minMs / step) * step;
  const out: number[] = [];
  for (let t = start; t <= maxMs; t += step) out.push(t);
  return out.length ? out : [minMs];
}

export function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}

/** Map a ratio 0..1 into a palette index set of length n (nearest). */
export function rampIndex(t: number, n: number): number {
  if (n <= 1) return 0;
  return clamp(Math.round(t * (n - 1)), 0, n - 1);
}
