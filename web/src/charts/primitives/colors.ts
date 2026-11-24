/*
 * Colour for a chart, resolved to the active theme. Hue belongs to the plugin,
 * values inside one channel use the categorical ramp, ordered measures the
 * sequential ramp through the source hue, signed measures the diverging ramp.
 * Status never borrows a source hue. All of this is the design system's; we only
 * choose which ramp a given encode channel earns (see dataviz "colour by job").
 */
import { useMemo } from "react";
import { sourceColor, useSourceColor } from "@/ui";
import type { ThemeName } from "@/ui/sourceColor";
import { chartTokens, statusTone, type ChartTokens } from "../runtime/tokens";

export interface ChartColors {
  theme: ThemeName;
  tokens: ChartTokens;
  /** Up to n distinct hues for the values of one channel (identity). */
  categorical: (n: number) => string[];
  /** A light→dark ramp through the panel's source hue (magnitude). */
  sequential: (steps: number) => string[];
  /** A two-pole ramp with a neutral midpoint (polarity). */
  diverging: (steps: number) => string[];
  /** The source colour of the panel's plugin (series == the channel itself). */
  source: (channelIndex?: number, alpha?: number) => string;
  /** rgb() form of the source colour, for <canvas>. */
  sourceRgb: (channelIndex?: number, alpha?: number) => string;
  /** Status colour for a status value, by tone (good/warn/bad), with a glyph. */
  status: (value: string) => { color: string; tone: "ok" | "warn" | "bad" | "neutral"; glyph: string };
  /** Categorical ramp as rgb() strings (for <canvas>, which may not take oklch). */
  categoricalRgb: (n: number) => string[];
}

/** Parse an "oklch(L C H[ / a])" string to an "rgb(r g b[ / a])" string. */
export function oklchStringToRgb(s: string): string {
  const m = /oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\s*\)/.exec(s);
  if (!m) return s;
  const [r, g, b] = sourceColor.oklchToSrgb(Number(m[1]), Number(m[2]), Number(m[3]));
  const a = m[4] ? Number(m[4]) : 1;
  return a >= 1 ? `rgb(${r} ${g} ${b})` : `rgb(${r} ${g} ${b} / ${a})`;
}

const GLYPH = { ok: "●", warn: "▲", bad: "■", neutral: "○" } as const;

export function useChartColors(plugin = ""): ChartColors {
  const { theme, assignments } = useSourceColor();
  const tokens = chartTokens(theme);
  return useMemo<ChartColors>(() => {
    const statusColor = (tone: "ok" | "warn" | "bad" | "neutral"): string =>
      tone === "ok" ? tokens.ok : tone === "warn" ? tokens.warn : tone === "bad" ? tokens.bad : tokens.ink3;
    return {
      theme,
      tokens,
      categorical: (n) => sourceColor.categorical(Math.max(1, n), theme),
      categoricalRgb: (n) => sourceColor.categorical(Math.max(1, n), theme).map(oklchStringToRgb),
      sequential: (steps) => sourceColor.sequential(assignments, plugin, Math.max(1, steps), theme),
      diverging: (steps) => sourceColor.diverging(Math.max(1, steps), theme),
      source: (channelIndex = 0, alpha = 1) => sourceColor.css(assignments, plugin, channelIndex, theme, alpha),
      sourceRgb: (channelIndex = 0, alpha = 1) => sourceColor.rgb(assignments, plugin, channelIndex, theme, alpha),
      status: (value) => {
        const tone = statusTone(value);
        return { color: statusColor(tone), tone, glyph: GLYPH[tone] };
      },
    };
  }, [theme, tokens, assignments, plugin]);
}
