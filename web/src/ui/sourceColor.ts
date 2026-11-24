/*
 * Source colour system. See docs/web/design.md "Source colour".
 *
 *   Hue belongs to the plugin, lightness to the channel inside it.
 *
 * The front end assigns each plugin the next free hue from a fixed order the
 * first time it sees it, and persists that assignment (UI state kind "colors",
 * with a localStorage fallback) so a new plugin never recolours the others.
 *
 * Swatch: oklch(L C H), L/C per theme (--src-l / --src-c in tokens.css).
 * Channel k of a plugin shifts lightness by ±0.055·ceil(k/2).
 *
 * Outputs are theme-aware:
 *   - css(...)     -> an `oklch(...)` string for CSS (both themes supported
 *                     directly by the browser).
 *   - rgb(...)     -> an `r,g,b` / `rgb()` string for <canvas>, computed for a
 *                     given theme because canvas cannot read CSS variables.
 */

export type ThemeName = "nocturne" | "daybook";

/** Fixed hue order (degrees). A plugin takes the next free one on first sight. */
export const HUE_ORDER: readonly number[] = [
  25, 150, 255, 45, 190, 330, 85, 215, 300, 12, 170, 120,
];

/** Reference lightness/chroma per theme, mirrored from tokens.css. */
const THEME_LC: Record<ThemeName, { l: number; c: number }> = {
  nocturne: { l: 0.74, c: 0.13 },
  daybook: { l: 0.52, c: 0.15 },
};

export interface SourceAssignments {
  /** plugin name -> index into HUE_ORDER (stable, monotonic in first-sight order) */
  readonly plugins: Readonly<Record<string, number>>;
}

export const emptyAssignments: SourceAssignments = { plugins: {} };

/**
 * Fold a list of plugin names (in their canonical order) into/over an existing
 * assignment map, giving each unseen plugin the next free hue index. Pure.
 */
export function assignPlugins(
  prev: SourceAssignments,
  pluginNames: readonly string[],
): SourceAssignments {
  const plugins: Record<string, number> = { ...prev.plugins };
  const used = new Set<number>(Object.values(plugins));
  let next = 0;
  const nextFree = (): number => {
    while (used.has(next % HUE_ORDER.length) && used.size < HUE_ORDER.length) {
      next++;
    }
    const idx = next % HUE_ORDER.length;
    used.add(idx);
    next++;
    return idx;
  };
  for (const name of pluginNames) {
    if (!(name in plugins)) plugins[name] = nextFree();
  }
  return { plugins };
}

/** The hue (degrees) a plugin resolves to, or a neutral fallback hue. */
export function pluginHue(a: SourceAssignments, plugin: string): number {
  const idx = a.plugins[plugin];
  if (idx === undefined) return HUE_ORDER[0] ?? 25;
  return HUE_ORDER[idx % HUE_ORDER.length] ?? 25;
}

/**
 * Manually move a plugin to a specific hue index (owner reassignment). If the
 * index is taken, the two plugins swap, so the palette stays a permutation.
 */
export function reassignPlugin(
  a: SourceAssignments,
  plugin: string,
  hueIndex: number,
): SourceAssignments {
  const idx = ((hueIndex % HUE_ORDER.length) + HUE_ORDER.length) % HUE_ORDER.length;
  const plugins: Record<string, number> = { ...a.plugins };
  const holder = Object.keys(plugins).find((p) => plugins[p] === idx && p !== plugin);
  const previous = plugins[plugin];
  plugins[plugin] = idx;
  if (holder && previous !== undefined) plugins[holder] = previous;
  return { plugins };
}

/** Lightness delta for channel k (0-based) within a plugin. */
function channelLightnessDelta(k: number): number {
  if (k <= 0) return 0;
  const step = Math.ceil(k / 2);
  const sign = k % 2 === 1 ? 1 : -1;
  return sign * 0.055 * step;
}

export interface Swatch {
  readonly l: number;
  readonly c: number;
  readonly h: number;
}

/** Resolve a plugin (optionally a channel index within it) to an OKLCH swatch. */
export function swatch(
  a: SourceAssignments,
  plugin: string,
  channelIndex = 0,
  theme: ThemeName = "nocturne",
): Swatch {
  const { l, c } = THEME_LC[theme];
  const h = pluginHue(a, plugin);
  const li = clamp(l + channelLightnessDelta(channelIndex), 0.28, 0.92);
  return { l: li, c, h };
}

/** OKLCH css string for a plugin/channel. */
export function css(
  a: SourceAssignments,
  plugin: string,
  channelIndex = 0,
  theme: ThemeName = "nocturne",
  alpha = 1,
): string {
  const s = swatch(a, plugin, channelIndex, theme);
  const base = `oklch(${round(s.l)} ${round(s.c)} ${round(s.h)}`;
  return alpha >= 1 ? `${base})` : `${base} / ${round(alpha)})`;
}

/** `rgb(r g b)` / `rgb(r g b / a)` string for <canvas>, computed for a theme. */
export function rgb(
  a: SourceAssignments,
  plugin: string,
  channelIndex = 0,
  theme: ThemeName = "nocturne",
  alpha = 1,
): string {
  const s = swatch(a, plugin, channelIndex, theme);
  const [r, g, b] = oklchToSrgb(s.l, s.c, s.h);
  return alpha >= 1 ? `rgb(${r} ${g} ${b})` : `rgb(${r} ${g} ${b} / ${round(alpha)})`;
}

/* ---- Chart ramps --------------------------------------------------------- */

/**
 * Categorical ramp for values inside ONE channel: a separate set of hues that
 * does not borrow the source hue. Returns up to `n` distinct oklch strings.
 */
export function categorical(n: number, theme: ThemeName = "nocturne"): string[] {
  const { l, c } = THEME_LC[theme];
  const out: string[] = [];
  // Golden-angle walk so adjacent categories stay distinct at any n.
  for (let i = 0; i < n; i++) {
    const h = (i * 137.508 + 20) % 360;
    out.push(`oklch(${round(l)} ${round(c * 0.9)} ${round(h)})`);
  }
  return out;
}

/** Sequential ramp through a panel's source hue (ordered measures). */
export function sequential(
  a: SourceAssignments,
  plugin: string,
  steps: number,
  theme: ThemeName = "nocturne",
): string[] {
  const h = pluginHue(a, plugin);
  const c = THEME_LC[theme].c;
  const lo = theme === "nocturne" ? 0.35 : 0.9;
  const hi = theme === "nocturne" ? 0.9 : 0.35;
  const out: string[] = [];
  for (let i = 0; i < steps; i++) {
    const t = steps <= 1 ? 0 : i / (steps - 1);
    const l = lo + (hi - lo) * t;
    const cc = c * (0.55 + 0.45 * t);
    out.push(`oklch(${round(l)} ${round(cc)} ${round(h)})`);
  }
  return out;
}

/** Diverging ramp for signed measures (brass-warm negative, patina-cool positive). */
export function diverging(steps: number, theme: ThemeName = "nocturne"): string[] {
  const { c } = THEME_LC[theme];
  const neg = 45; // brass-ward
  const pos = 190; // patina-ward
  const mid = theme === "nocturne" ? 0.62 : 0.68;
  const out: string[] = [];
  for (let i = 0; i < steps; i++) {
    const t = steps <= 1 ? 0.5 : i / (steps - 1);
    const h = t < 0.5 ? neg : pos;
    const d = Math.abs(t - 0.5) * 2;
    const l = mid + (theme === "nocturne" ? 0.12 : -0.14) * (1 - d);
    out.push(`oklch(${round(l)} ${round(c * (0.3 + 0.7 * d))} ${round(h)})`);
  }
  return out;
}

/* ---- Colour-space maths (OKLCH -> sRGB), for canvas ---------------------- */

/** OKLCH (L 0..1, C, H degrees) to sRGB bytes [0..255]. */
export function oklchToSrgb(L: number, C: number, Hdeg: number): [number, number, number] {
  const h = (Hdeg * Math.PI) / 180;
  const a = C * Math.cos(h);
  const b = C * Math.sin(h);

  // OKLab -> LMS'
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.291485548 * b;

  const l = l_ * l_ * l_;
  const m = m_ * m_ * m_;
  const s = s_ * s_ * s_;

  // LMS -> linear sRGB
  const lr = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s;
  const lg = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s;
  const lb = -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s;

  return [toByte(lr), toByte(lg), toByte(lb)];
}

function toByte(linear: number): number {
  const c = linear <= 0.0031308 ? 12.92 * linear : 1.055 * Math.pow(linear, 1 / 2.4) - 0.055;
  return Math.max(0, Math.min(255, Math.round(c * 255)));
}

function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}

function round(x: number): number {
  return Math.round(x * 1000) / 1000;
}
