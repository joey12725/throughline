/*
 * Pure formatting for chart labels, ticks and tooltips. Numbers read as tabular
 * mono in the UI; these produce the strings. All pure, all unit-tested.
 */

/** Compact number for an axis tick or a direct label: 1.2k, 3.4M, 12, 0.08. */
export function compactNumber(x: number): string {
  if (!Number.isFinite(x)) return "—";
  const a = Math.abs(x);
  if (a === 0) return "0";
  if (a >= 1_000_000_000) return trim(x / 1_000_000_000) + "B";
  if (a >= 1_000_000) return trim(x / 1_000_000) + "M";
  if (a >= 1_000) return trim(x / 1_000) + "k";
  if (a >= 1) return trim(x);
  if (a >= 0.01) return x.toFixed(2);
  return x.toPrecision(2);
}

function trim(x: number): string {
  const r = Math.round(x * 10) / 10;
  return Number.isInteger(r) ? String(r) : r.toFixed(1);
}

/** Exact number with thousands separators, for tooltips. */
export function exactNumber(x: number): string {
  if (!Number.isFinite(x)) return "—";
  if (Number.isInteger(x)) return x.toLocaleString("en-US");
  const a = Math.abs(x);
  return a >= 100 ? Math.round(x).toLocaleString("en-US") : a >= 1 ? x.toFixed(2) : x.toPrecision(3);
}

/** A span of seconds as a short human duration: 45s, 3m, 2h 10m, 1d 4h. */
export function humanDuration(seconds: number): string {
  if (!Number.isFinite(seconds)) return "—";
  const s = Math.abs(Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) {
    const rs = s % 60;
    return rs ? `${m}m ${rs}s` : `${m}m`;
  }
  const h = Math.floor(m / 60);
  if (h < 24) {
    const rm = m % 60;
    return rm ? `${h}h ${rm}m` : `${h}h`;
  }
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `${d}d ${rh}h` : `${d}d`;
}

/** 0..1 (or 0..100 if looksLikePercent) rendered as a percent. */
export function humanPercent(value: number, unit?: string): string {
  if (!Number.isFinite(value)) return "—";
  const pct = unit === "pct" || value > 1.0001 ? value : value * 100;
  const r = Math.abs(pct) >= 10 ? Math.round(pct) : Math.round(pct * 10) / 10;
  return `${r}%`;
}

const UNIT_LABEL: Record<string, string> = { s: "s", ms: "ms", kg: "kg", m: "m", meters: "m", metres: "m", pct: "%", lb: "lb" };

/** Format a measure value honouring a known unit (duration, percent, mass…). */
export function formatMeasure(value: number, unit?: string): string {
  if (!Number.isFinite(value)) return "—";
  if (unit === "s") return humanDuration(value);
  if (unit === "ms") return humanDuration(value / 1000);
  if (unit === "pct") return humanPercent(value, "pct");
  // Only append a real, short unit token. Mined descriptors like "0-100" or long
  // phrases are not units and would clutter an axis, so they are dropped.
  const u = unit && (UNIT_LABEL[unit] ?? (/^[a-z%]{1,4}$/i.test(unit) ? unit : ""));
  return u ? `${exactNumber(value)} ${u}` : exactNumber(value);
}

/** Humanise a snake_case field or value into a label. */
export function humanLabel(name: string): string {
  const s = name.replace(/[_\s]+/g, " ").trim();
  if (!s) return name;
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/* ---- time ---------------------------------------------------------------- */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function weekdayLabel(isoWeekday1to7: number): string {
  return WEEKDAYS[(isoWeekday1to7 - 1 + 7) % 7] ?? String(isoWeekday1to7);
}

export function hourLabel(hour: number): string {
  const h = ((hour % 24) + 24) % 24;
  if (h === 0) return "12a";
  if (h === 12) return "12p";
  return h < 12 ? `${h}a` : `${h - 12}p`;
}

/**
 * A time-axis tick label chosen for the grain. UTC parts by default; when a
 * timezone-aware label is wanted the caller passes a formatter instead.
 */
export function timeTickLabel(ms: number, grain: string | null | undefined): string {
  const d = new Date(ms);
  if (!Number.isFinite(d.getTime())) return "—";
  const g = grain ?? "day";
  switch (g) {
    case "minute":
    case "hour":
      return `${d.getUTCHours().toString().padStart(2, "0")}:${d.getUTCMinutes().toString().padStart(2, "0")}`;
    case "day":
    case "week":
      return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
    case "month":
      return `${MONTHS[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
    case "quarter":
      return `Q${Math.floor(d.getUTCMonth() / 3) + 1} ${String(d.getUTCFullYear()).slice(2)}`;
    case "year":
      return String(d.getUTCFullYear());
    default:
      return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
  }
}

/** A full moment for a tooltip, in the given timezone. */
export function tooltipTime(value: string | number | Date, tz = "UTC"): string {
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", timeZone: tz }).format(d);
  } catch {
    return d.toISOString();
  }
}

/** A day label (no time) for a tooltip, in the given timezone. */
export function tooltipDay(value: string | number | Date, tz = "UTC"): string {
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: tz }).format(d);
  } catch {
    return d.toISOString().slice(0, 10);
  }
}

/** Compact relative time from a timestamp (ms): "now", "3h ago", "5d ago". */
export function relativeShort(ms: number, now = Date.now()): string {
  if (!Number.isFinite(ms)) return "—";
  const s = Math.round((now - ms) / 1000);
  const ahead = s < 0;
  const a = Math.abs(s);
  let out: string;
  if (a < 45) return "now";
  else if (a < 3600) out = `${Math.round(a / 60)}m`;
  else if (a < 86400) out = `${Math.round(a / 3600)}h`;
  else if (a < 2_592_000) out = `${Math.round(a / 86400)}d`;
  else if (a < 31_536_000) out = `${Math.round(a / 2_592_000)}mo`;
  else out = `${Math.round(a / 31_536_000)}y`;
  return ahead ? `in ${out}` : `${out} ago`;
}

/** Clip a long free-text string for a label or a cell, on a word boundary. */
export function clip(text: string, max: number): string {
  if (text.length <= max) return text;
  const cut = text.slice(0, max);
  const sp = cut.lastIndexOf(" ");
  return (sp > max * 0.6 ? cut.slice(0, sp) : cut).trimEnd() + "…";
}
