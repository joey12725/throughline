/*
 * Pure helpers for the Day view: calendar-date arithmetic (no timezone needed —
 * a YYYY-MM-DD is a plain calendar day), grouping a day's records into episodes
 * by time gaps, and turning a naive local timestamp into a 0..24 hour offset for
 * the swimlanes. Everything here is unit-tested; the components stay declarative.
 */

export interface DayRecord {
  id: number;
  channel: string;
  /** Absolute instant (ISO, UTC) the record happened. */
  happenedAt: string;
  /** Absolute instant the interval ended, when the channel carries one. */
  endedAt?: string | null;
  /** The record's wall-clock time in the scope zone, as "YYYY-MM-DDTHH:MM:SS". */
  localTime: string;
  localEnd?: string | null;
  content: string;
  lat?: number | null;
  lon?: number | null;
  personId?: number | null;
  displayName?: string | null;
  threadId?: number | null;
}

export interface Episode {
  key: string;
  startMs: number;
  endMs: number;
  records: DayRecord[];
}

/** A valid YYYY-MM-DD date, or null. */
export function parseDate(s: string | null | undefined): string | null {
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) return null;
  const y = +(m[1] ?? 0);
  const mo = +(m[2] ?? 0);
  const d = +(m[3] ?? 0);
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  return s;
}

/** Split a YYYY-MM-DD into numeric [year, month, day]. */
export function ymd(date: string): [number, number, number] {
  const p = date.split("-");
  return [Number(p[0] ?? 0), Number(p[1] ?? 1), Number(p[2] ?? 1)];
}

/** Add `n` calendar days to a YYYY-MM-DD, returning YYYY-MM-DD (UTC arithmetic). */
export function addDays(date: string, n: number): string {
  const [y, m, d] = ymd(date);
  const t = Date.UTC(y, m - 1, d) + n * 86_400_000;
  const dt = new Date(t);
  const yy = dt.getUTCFullYear().toString().padStart(4, "0");
  const mm = (dt.getUTCMonth() + 1).toString().padStart(2, "0");
  const dd = dt.getUTCDate().toString().padStart(2, "0");
  return `${yy}-${mm}-${dd}`;
}

/** Today's calendar date in the given IANA zone, as YYYY-MM-DD. */
export function todayInZone(tz: string, at: Date = new Date()): string {
  try {
    // en-CA renders ISO-like YYYY-MM-DD.
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(at);
  } catch {
    return at.toISOString().slice(0, 10);
  }
}

/** Fraction-of-day (0..24) for a naive local "YYYY-MM-DDTHH:MM:SS" timestamp. */
export function localHour(localTime: string): number {
  const t = localTime.slice(11);
  const [h = "0", mi = "0", s = "0"] = t.split(":");
  return Number(h) + Number(mi) / 60 + Number(s) / 3600;
}

/**
 * Hours from the start of `date` (local) to a local timestamp, allowing values
 * outside [0,24] for records that begin the previous evening or end the next
 * morning — so an interval crossing midnight draws correctly on the day band.
 */
export function hourOffset(localTime: string, date: string): number {
  const [ly, lm, ldd] = ymd(localTime.slice(0, 10));
  const [dy, dm, dd] = ymd(date);
  const diffDays = Math.round((Date.UTC(ly, lm - 1, ldd) - Date.UTC(dy, dm - 1, dd)) / 86_400_000);
  return diffDays * 24 + localHour(localTime);
}

/**
 * Group records into episodes: a new episode starts when the gap from the
 * previous record exceeds `gapMinutes`. Records are sorted by instant first, so
 * ordering is independent of the input order.
 */
export function groupEpisodes(records: DayRecord[], gapMinutes = 40): Episode[] {
  const sorted = [...records].sort((a, b) => Date.parse(a.happenedAt) - Date.parse(b.happenedAt));
  const gapMs = gapMinutes * 60_000;
  const episodes: Episode[] = [];
  let cur: Episode | null = null;
  for (const r of sorted) {
    const t = Date.parse(r.happenedAt);
    if (Number.isNaN(t)) continue;
    if (!cur || t - cur.endMs > gapMs) {
      cur = { key: `ep:${r.id}`, startMs: t, endMs: t, records: [r] };
      episodes.push(cur);
    } else {
      cur.records.push(r);
      cur.endMs = Math.max(cur.endMs, t);
    }
    // An interval that runs on keeps the episode "open" until it ends.
    if (r.endedAt) {
      const e = Date.parse(r.endedAt);
      if (!Number.isNaN(e)) cur.endMs = Math.max(cur.endMs, e);
    }
  }
  return episodes;
}

export interface DayVitals {
  records: number;
  channels: number;
  people: number;
  places: number;
  episodes: number;
  firstMs: number | null;
  lastMs: number | null;
}

/** Headline counts for a day, derived from its records. */
export function dayVitals(records: DayRecord[], episodes: Episode[], ownerPersonId: number | null): DayVitals {
  const channels = new Set<string>();
  const people = new Set<number>();
  const places = new Set<string>();
  let firstMs: number | null = null;
  let lastMs: number | null = null;
  for (const r of records) {
    channels.add(r.channel);
    if (r.personId != null && r.personId !== ownerPersonId) people.add(r.personId);
    if (r.lat != null && r.lon != null) places.add(`${r.lat.toFixed(3)},${r.lon.toFixed(3)}`);
    const t = Date.parse(r.happenedAt);
    if (!Number.isNaN(t)) {
      firstMs = firstMs === null ? t : Math.min(firstMs, t);
      lastMs = lastMs === null ? t : Math.max(lastMs, t);
    }
  }
  return {
    records: records.length,
    channels: channels.size,
    people: people.size,
    places: places.size,
    episodes: episodes.length,
    firstMs,
    lastMs,
  };
}
