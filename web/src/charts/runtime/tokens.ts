/*
 * Chart tokens, mirrored from ui/tokens.css so a <canvas> (which cannot read CSS
 * variables) and an SVG draw the exact same colours in both themes. If a value
 * changes in tokens.css it changes here — the two are a pair, like --src-l/c and
 * sourceColor.ts. SVG renderers may still use var(--…); canvas uses these.
 */
import type { ThemeName } from "@/ui/sourceColor";

export interface ChartTokens {
  bg0: string;
  bg1: string;
  bg2: string;
  bg3: string;
  line: string;
  lineStrong: string;
  ink: string;
  ink2: string;
  ink3: string;
  brass: string;
  patina: string;
  ok: string;
  warn: string;
  bad: string;
  /** Hairline grid / soft fills derived from ink. */
  gridSoft: string;
  fontUi: string;
  fontMono: string;
  fontRead: string;
}

const NOCTURNE: ChartTokens = {
  bg0: "#0A0C0F",
  bg1: "#10131A",
  bg2: "#171B24",
  bg3: "#1F2430",
  line: "#2A3040",
  lineStrong: "#3A4254",
  ink: "#ECE6D8",
  ink2: "#A6A392",
  ink3: "#6B6E72",
  brass: "#D9A64A",
  patina: "#58B9A6",
  ok: "#6FBF8E",
  warn: "#E0A93C",
  bad: "#E0603C",
  gridSoft: "rgba(236,230,216,0.08)",
  fontUi: '"Instrument Sans Variable","Instrument Sans",system-ui,sans-serif',
  fontMono: '"JetBrains Mono Variable","JetBrains Mono",ui-monospace,monospace',
  fontRead: '"Newsreader Variable","Newsreader",Georgia,serif',
};

const DAYBOOK: ChartTokens = {
  bg0: "#F5F1E7",
  bg1: "#FBF8F1",
  bg2: "#FFFFFF",
  bg3: "#ECE6D7",
  line: "#E1D9C8",
  lineStrong: "#CFC5B0",
  ink: "#1C1A16",
  ink2: "#57534A",
  ink3: "#8A857A",
  brass: "#A9772A",
  patina: "#2E8576",
  ok: "#2E7D5B",
  warn: "#A86A00",
  bad: "#B3261E",
  gridSoft: "rgba(28,26,22,0.08)",
  fontUi: '"Instrument Sans Variable","Instrument Sans",system-ui,sans-serif',
  fontMono: '"JetBrains Mono Variable","JetBrains Mono",ui-monospace,monospace',
  fontRead: '"Newsreader Variable","Newsreader",Georgia,serif',
};

export function chartTokens(theme: ThemeName): ChartTokens {
  return theme === "daybook" ? DAYBOOK : NOCTURNE;
}

/** Status colour + glyph for a category.status value, derived from shape, not name. */
export function statusTone(value: string): "ok" | "warn" | "bad" | "neutral" {
  const v = value.toLowerCase();
  if (/(ok|success|succeeded|complete|done|sent|delivered|active|healthy|pass|up|green|ready)/.test(v)) return "ok";
  if (/(late|pending|queued|waiting|partial|stale|retry|warn|degraded|skip|paused|hold)/.test(v)) return "warn";
  if (/(fail|error|bad|dead|reject|cancel|abort|lost|down|blocked|denied|critical|missing)/.test(v)) return "bad";
  return "neutral";
}
