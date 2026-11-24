/*
 * Pure constellation layout. No DOM, no React — just the maths that turns a set
 * of people into stars on a dark field, so it can be unit-tested and reasoned
 * about. The renderer (Constellation.tsx) maps the normalised positions to the
 * canvas and paints them in the source colours.
 *
 * The field is read like the night sky around the owner at the centre:
 *   - distance from centre  ← recency of last contact (recent = close, warm),
 *                             spread by RANK (each person's quantile among all
 *                             the others) so the disc fills evenly instead of
 *                             collapsing into one ring when everyone is recent
 *   - angle (sector)        ← the channel family the person is mostly reached
 *                             through, so plugins cluster into constellations
 *   - size                  ← lifetime volume (sqrt, so a chatty few don't
 *                             drown everyone)
 *   - brightness / twinkle  ← recent activity (reduced-motion stops the twinkle)
 *
 * The rank transform keeps the radial order honest (nearer = more recent) while
 * decoupling the geometry from the raw age distribution; `recencyRings` then
 * reports where the familiar time horizons (a day, a week, a month, a year) land
 * on that transformed axis so the rings can be labelled truthfully.
 */

export interface PersonInput {
  id: number;
  name: string;
  handle?: string | null;
  records: number;
  channels: number;
  firstAt: string | null;
  lastAt: string | null;
  /** The plugin family the person is mostly reached through (for the sector + hue). */
  family: string;
  /** The dominant channel, for the colour's lightness step and the label. */
  topChannel: string;
}

export interface Star extends PersonInput {
  /** Angle in radians, 0 = east, counter-clockwise. */
  angle: number;
  /** Normalised distance from the centre, 0 (centre) .. 1 (rim). */
  radius: number;
  /** Cartesian position in the unit disc, each in [-1, 1]. */
  x: number;
  y: number;
  /** Base star radius in layout units (≈ pixels at zoom 1). */
  size: number;
  /** 0..1 — how brightly the star burns (recent contact burns bright). */
  brightness: number;
  /** Stable 0..1 phase so twinkles are not synchronised. */
  phase: number;
}

export interface LayoutOptions {
  /** "now" in epoch ms; defaults to Date.now(). */
  now?: number;
  /** Inner radius so the busiest people don't collide with the owner. */
  innerRadius?: number;
  sizeMin?: number;
  sizeMax?: number;
}

const DAY = 86_400_000;

/** A concentric recency horizon drawn on the field (e.g. "1 week" at r≈0.4). */
export interface RecencyRing {
  label: string;
  ageMs: number;
  /** Normalised distance from the centre, inner..1. */
  radius: number;
}

/** Deterministic 0..1 hash of an integer id (xorshift-ish), stable across runs. */
export function hash01(id: number): number {
  let x = (id | 0) ^ 0x9e3779b9;
  x = Math.imul(x ^ (x >>> 16), 0x45d9f3b);
  x = Math.imul(x ^ (x >>> 16), 0x45d9f3b);
  x = (x ^ (x >>> 16)) >>> 0;
  return x / 0xffffffff;
}

/** Map an age (ms since last contact) onto 0 (just now) .. 1 (as old as it gets). */
export function recencyNorm(ageMs: number, maxAgeMs: number): number {
  if (maxAgeMs <= 0) return 0;
  const a = Math.max(0, ageMs);
  return clamp01(Math.log1p(a) / Math.log1p(maxAgeMs));
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

function ms(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/**
 * Lay out the stars. Families are given angular sectors sized by membership and
 * ordered deterministically by name, so a family always keeps its patch of sky.
 * Within a family, members are placed by volume with a small id-keyed jitter so
 * two equally-recent people never land exactly on top of each other.
 */
export function buildConstellation(people: PersonInput[], opts: LayoutOptions = {}): Star[] {
  const now = opts.now ?? Date.now();
  const innerRadius = opts.innerRadius ?? 0.14;
  const sizeMin = opts.sizeMin ?? 3;
  const sizeMax = opts.sizeMax ?? 15;

  if (people.length === 0) return [];

  const maxRecords = Math.max(1, ...people.map((p) => p.records));
  // Age (ms since last contact) per person; never-contacted is maximally distant.
  const ageById = new Map<number, number>();
  for (const p of people) {
    const last = ms(p.lastAt);
    ageById.set(p.id, last === null ? Number.POSITIVE_INFINITY : Math.max(0, now - last));
  }
  // Rank transform: each person's radius is their recency quantile among all the
  // others, so a cluster of equally-recent people spreads across the disc rather
  // than piling into a single ring. Ties break by id for a deterministic sky.
  const ranked = [...people].sort((a, b) => (ageById.get(a.id)! - ageById.get(b.id)!) || a.id - b.id);
  const qById = new Map<number, number>();
  const denom = Math.max(1, ranked.length - 1);
  ranked.forEach((p, rank) => qById.set(p.id, ranked.length === 1 ? 0 : rank / denom));

  // Group by family, order families by name for a stable sky.
  const byFamily = new Map<string, PersonInput[]>();
  for (const p of people) {
    const list = byFamily.get(p.family);
    if (list) list.push(p);
    else byFamily.set(p.family, [p]);
  }
  const families = [...byFamily.keys()].sort();
  const total = people.length;

  const stars: Star[] = [];
  let cursor = 0; // running fraction of the circle consumed
  for (const fam of families) {
    const members = byFamily.get(fam)!;
    // Sort members: most voluminous first, stable by id.
    members.sort((a, b) => b.records - a.records || a.id - b.id);
    const span = members.length / total; // fraction of the circle for this family
    const start = cursor * Math.PI * 2;
    const arc = span * Math.PI * 2;
    // A small gap between families so sectors read as separate constellations.
    const pad = arc * 0.12;
    const usable = Math.max(arc - pad, arc * 0.5);
    members.forEach((p, i) => {
      const t = members.length === 1 ? 0.5 : i / (members.length - 1);
      const jitter = (hash01(p.id) - 0.5) * (usable / Math.max(members.length, 6));
      const angle = start + pad / 2 + t * usable + jitter;

      const q = qById.get(p.id) ?? 1; // 0 = most recent, 1 = stalest
      const radius = innerRadius + (1 - innerRadius) * q;
      const size = sizeMin + (sizeMax - sizeMin) * Math.sqrt(p.records / maxRecords);
      const brightness = clamp01(0.35 + 0.65 * (1 - q));

      stars.push({
        ...p,
        angle,
        radius,
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius,
        size,
        brightness,
        phase: hash01(p.id * 2654435761),
      });
    });
    cursor += span;
  }
  return stars;
}

const HORIZONS: { label: string; ageMs: number }[] = [
  { label: "1 day", ageMs: DAY },
  { label: "1 week", ageMs: 7 * DAY },
  { label: "1 month", ageMs: 30 * DAY },
  { label: "1 year", ageMs: 365 * DAY },
];

/**
 * Where the familiar time horizons land on the rank-transformed radial axis, for
 * the labelled guide rings. A horizon sits on the boundary between the people
 * nearer than it and those beyond, matching exactly how `buildConstellation`
 * places the stars, so a ring reads as "everyone inside this circle is newer
 * than a week". Horizons with no one on one side are dropped (nothing to anchor).
 */
export function recencyRings(people: PersonInput[], opts: LayoutOptions = {}): RecencyRing[] {
  const now = opts.now ?? Date.now();
  const innerRadius = opts.innerRadius ?? 0.14;
  const n = people.length;
  if (n < 2) return [];
  const ages = people
    .map((p) => {
      const last = ms(p.lastAt);
      return last === null ? Number.POSITIVE_INFINITY : Math.max(0, now - last);
    })
    .sort((a, b) => a - b);
  const denom = n - 1;
  const rings: RecencyRing[] = [];
  for (const h of HORIZONS) {
    const nearer = ages.filter((a) => a <= h.ageMs).length; // 1..n people inside
    if (nearer === 0 || nearer === n) continue; // horizon is off the field
    const q = (nearer - 0.5) / denom; // boundary between star nearer-1 and nearer
    rings.push({ label: h.label, ageMs: h.ageMs, radius: innerRadius + (1 - innerRadius) * clamp01(q) });
  }
  // Collapse rings that round to the same radius (keep the finer horizon).
  const out: RecencyRing[] = [];
  for (const r of rings) {
    if (out.length && Math.abs(out[out.length - 1]!.radius - r.radius) < 0.05) continue;
    out.push(r);
  }
  return out;
}

/** The families present, with their aggregate volume — for the legend. */
export function familyTotals(people: PersonInput[]): { family: string; records: number; count: number }[] {
  const m = new Map<string, { records: number; count: number }>();
  for (const p of people) {
    const e = m.get(p.family) ?? { records: 0, count: 0 };
    e.records += p.records;
    e.count += 1;
    m.set(p.family, e);
  }
  return [...m.entries()]
    .map(([family, v]) => ({ family, ...v }))
    .sort((a, b) => b.records - a.records);
}
