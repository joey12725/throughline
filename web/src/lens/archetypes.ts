// Archetypes: ten weighted evidence functions, each 0..1 with its reasons. A channel has a
// primary archetype and any number of secondaries above 0.5; they are scored, not exclusive.

import { findLevels } from "./levels";
import { isMeasure } from "./roles";
import type {
  ArchetypeId,
  ArchetypeScore,
  Archetypes,
  ChannelMeta,
  ChannelProfile,
  Role,
  RoleId,
  RoleMap,
} from "./types";
import { ARCHETYPE_IDS } from "./types";

const clamp = (x: number) => Math.max(0, Math.min(1, x));

/** 0 at `lo`, 1 at `hi`, logarithmic in between. */
function logRamp(x: number, lo: number, hi: number): number {
  if (!(x > lo)) return 0;
  if (x >= hi) return 1;
  return Math.log(x / lo) / Math.log(hi / lo);
}

// A specific structure outranks a generic one at equal evidence: almost anything with
// numbers looks like a measurement, very few things look like a transcript.
const PRIOR: Record<ArchetypeId, number> = {
  transcript: 1,
  gauge: 1,
  geo_track: 1,
  snapshot: 1,
  interval: 0.95,
  conversation: 0.95,
  feed_item: 0.9,
  document: 0.9,
  measurement: 0.85,
  registry: 0.85,
};

export interface ShapeFacts {
  rows: number;
  observed: boolean;
  rowsPerThread: number | null;
  endedShare: number | null;
  geoShare: number | null;
  avgChars: number | null;
  speakers: number;
  fieldCount: number;
  measures: number;
  has(role: RoleId): boolean;
  count(role: RoleId): number;
  first(role: RoleId): Role | undefined;
}

/** The handful of numbers the archetype functions (and the lenses) read. */
export function shapeFacts(meta: Pick<ChannelMeta, "fields"> | null, profile: ChannelProfile, roles: RoleMap): ShapeFacts {
  const env = profile.envelope;
  const rows = env.rows;
  const observed = profile.source !== "declared" && rows > 0;
  const live = Object.values(roles).filter((r) => !r.hidden);
  const geoKnown = env.with_geo_source ?? env.with_geo;
  return {
    rows,
    observed,
    rowsPerThread: observed && env.threads > 0 ? rows / env.threads : null,
    endedShare: observed ? env.with_ended_at / rows : null,
    geoShare: observed ? Math.min(1, geoKnown / rows) : null,
    avgChars: observed ? env.avg_content_chars : null,
    speakers: Math.max(env.people, env.authors),
    fieldCount: meta?.fields.length ?? profile.fields.length,
    measures: live.filter((r) => isMeasure(r.role)).length,
    has: (role) => live.some((r) => r.role === role),
    count: (role) => live.filter((r) => r.role === role).length,
    first: (role) => live.filter((r) => r.role === role).sort((a, b) => b.confidence - a.confidence)[0],
  };
}

interface Acc {
  score: number;
  evidence: string[];
}

function add(acc: Acc, weight: number, strength: number, reason: string): void {
  const s = clamp(strength);
  if (s <= 0 || weight === 0) return;
  acc.score += weight * s;
  if (weight * s >= 0.04 || weight < 0) acc.evidence.push(reason);
}

const pct = (x: number) => `${Math.round(x * 100)}%`;

/**
 * Score the ten archetypes for one channel. With no rows the envelope evidence is unknown
 * and the scores come from declared fields, flags and the description alone.
 */
export function scoreArchetypes(meta: ChannelMeta, profile: ChannelProfile, roles: RoleMap): Archetypes {
  const f = shapeFacts(meta, profile, roles);
  const env = profile.envelope;
  const description = meta.description ?? "";
  const words = `${meta.name} ${description}`.toLowerCase();
  const nFields = Math.max(1, f.fieldCount);
  const rpt = f.rowsPerThread;
  const chars = f.avgChars;
  const constantDuration =
    env.min_duration_s !== null && env.max_duration_s !== null && env.with_ended_at >= 5 &&
    Math.abs(env.max_duration_s - env.min_duration_s) <= Math.max(2, env.max_duration_s * 0.001);
  const dailyCue = /\bper (utc |local )?day\b|\bone [a-z ]{0,40} per day\b|\bdaily\b/.test(words);
  const fieldInterval = Object.values(roles).some((r) => r.role === "time.start" && r.pair && !r.hidden && r.confidence >= 0.8);
  const offsetPair = Object.values(roles).some((r) => r.role === "time.offset" && r.pair && !r.hidden);
  const geoPair = f.has("geo.lat") && f.has("geo.lon");
  const hasSpeaker = f.has("category.speaker");
  const hasGroup = f.has("group_key");
  const levels = f.count("percent") + Object.values(roles).filter((r) => !r.hidden && /\b(level|balance|remaining|left|used)\b/.test(r.field.replace(/_/g, " ")) && isMeasure(r.role)).length;
  const logCategory = Object.values(roles).some(
    (r) => !r.hidden && r.role === "category" && ["event", "change", "action", "operation", "op", "transition"].includes(r.field),
  );
  const plumbingish = Object.values(roles).filter(
    (r) => r.plumbing || r.role === "group_key" || r.role.startsWith("category") || r.role.startsWith("flag") || r.role === "title",
  ).length;

  const acc: Record<ArchetypeId, Acc> = Object.fromEntries(
    ARCHETYPE_IDS.map((id) => [id, { score: 0, evidence: [] as string[] }]),
  ) as Record<ArchetypeId, Acc>;

  // -- conversation
  {
    const a = acc.conversation;
    add(a, 0.4, hasSpeaker ? 1 : 0, `a speaker field (${f.first("category.speaker")?.field})`);
    if (rpt !== null) add(a, 0.3, logRamp(rpt, 2.5, 8), `about ${Math.round(rpt)} records per thread`);
    add(a, 0.15, hasSpeaker || f.speakers >= 2 ? 1 : 0, f.speakers >= 2 ? `${f.speakers} different authors` : "two sides to speak");
    if (chars !== null) add(a, 0.15, 1 - logRamp(chars, 250, 2500), `short content (about ${Math.round(chars)} characters)`);
    else add(a, 0.07, hasSpeaker ? 1 : 0, "content length not seen yet");
  }

  // -- transcript
  {
    const a = acc.transcript;
    add(a, 0.5, offsetPair ? 1 : 0, "a start/end offset pair inside each recording");
    add(a, 0.2, meta.whole_thread ? 1 : 0, "threads are read whole");
    if (f.endedShare !== null) add(a, 0.15, f.endedShare >= 0.9 && (rpt ?? 0) >= 5 ? 1 : 0, "every record has an end and threads are long");
    add(a, 0.15, Object.values(roles).some((r) => r.role === "category.speaker" && /speaker/.test(r.field)) ? 1 : 0, "a speaker label");
    add(a, 0.15, /\btranscript/.test(words) && (offsetPair || meta.whole_thread) ? 1 : 0, "described as a transcript");
  }

  // -- measurement
  {
    const a = acc.measurement;
    const share = f.measures / nFields;
    add(a, 0.5, share / 0.5, `${f.measures} of ${nFields} fields are quantities`);
    add(a, 0.2, f.measures >= 3 ? 1 : f.measures / 3, "several quantities per record");
    if (chars !== null) add(a, 0.15, 1 - logRamp(chars, 200, 1500), "content is a short caption");
    else add(a, 0.08, f.measures >= 3 ? 1 : 0, "content length not seen yet");
    add(a, 0.15, !hasSpeaker && f.measures >= 2 ? 1 : 0, "nobody is speaking");
  }

  // -- interval
  {
    const a = acc.interval;
    let damp = 1;
    if (constantDuration) damp = 0.4;
    else if (dailyCue && env.min_duration_s === null) damp = 0.5;
    if (f.endedShare !== null && f.endedShare > 0) {
      add(a, 1 * damp, f.endedShare, `${pct(f.endedShare)} of records have an end time`);
      if (damp < 1) a.evidence.push(constantDuration ? "but every interval has the same length: a period, not an event" : "but the description says one record per day");
    }
    if (a.score < 0.6) {
      const fieldScore = fieldInterval ? 0.6 : f.has("duration") && Object.values(roles).some((r) => r.role === "duration" && /^(duration|length)/.test(r.field)) ? 0.55 : 0;
      if (fieldScore > a.score) {
        a.score = fieldScore;
        a.evidence.push(fieldInterval ? "a start/end field pair" : "a duration field for the whole record");
      }
    }
  }

  // -- geo track
  {
    const a = acc.geo_track;
    if (f.geoShare !== null && f.geoShare > 0.02) add(a, 1, f.geoShare, `${pct(f.geoShare)} of records carry their own position`);
    if (geoPair && a.score < 0.92) {
      a.score = 0.92;
      a.evidence.push("a latitude/longitude field pair");
    }
  }

  // -- document
  {
    const a = acc.document;
    const titled = f.has("title");
    if (chars !== null) add(a, 0.55, logRamp(chars, 150, 2000), `long content (about ${Math.round(chars)} characters)`);
    else add(a, 0.2, f.has("body") && titled ? 1 : 0, "a title and body text");
    if (rpt !== null) add(a, 0.25, 1 - logRamp(rpt, 2, 60), rpt < 1.5 ? "one record per thread" : `about ${Math.round(rpt)} records per thread`);
    add(a, 0.2, titled ? 1 : 0, `a title field (${f.first("title")?.field})`);
    if (meta.record_kind === "page_revision") add(a, 0.3, 1, "records are page revisions");
  }

  // -- feed item
  {
    const a = acc.feed_item;
    add(a, 0.3, f.has("title") ? 1 : 0, "a title");
    add(a, 0.3, f.has("url") ? 1 : 0, "a link");
    add(a, 0.15, f.has("actor.label") ? 1 : 0, "a publisher or organiser");
    add(a, 0.25, meta.is_outside ? 1 : meta.record_kind === "derived" ? 0.8 : 0, meta.is_outside ? "describes the outside world" : "derived from another source");
    if (!f.has("title") && !f.has("url")) a.score *= 0.3;
  }

  // -- gauge
  {
    const a = acc.gauge;
    const tested = findLevels(profile, roles).find((l) => l.tested);
    add(a, 0.35, levels > 0 && hasGroup ? 1 : levels > 0 ? 0.4 : 0, hasGroup ? "a level per group key" : "a level field");
    add(a, 0.25, f.count("time.deadline") > 0 ? 1 : 0, `a time still to come (${f.first("time.deadline")?.field})`);
    add(a, 0.15, logCategory ? 1 : 0, "each record is a change of state");
    add(a, 0.1, levels > 0 && profile.cadence.class !== "daily" ? 1 : 0, "sampled when the value changes, not on a schedule");
    add(a, 0.25, tested ? tested.strength : 0, `${tested?.role.field} climbs and drops back ${tested?.drops} times in the sample`);
  }

  // -- snapshot
  {
    const a = acc.snapshot;
    const perSourceDay = /\bper source per day\b|\bone (record|row|snapshot) per [a-z ]{0,24}per day\b|\bdaily [a-z ]{0,30}snapshots?\b/.test(words);
    add(a, 0.3, perSourceDay ? 1 : dailyCue ? 0.4 : 0, "described as one record per source per day");
    add(a, 0.2, f.has("category.status") ? 1 : 0, "a status");
    add(a, 0.15, f.has("count") ? 1 : 0, "an item count");
    add(a, 0.2, f.has("json.object") ? 1 : 0, "a large JSON object");
    add(a, 0.15, f.has("date.text") ? 1 : 0, "a day written as text");
  }

  // -- registry
  {
    const a = acc.registry;
    add(a, 0.45, meta.is_reference || meta.record_kind === "reference" ? 1 : 0, "a reference channel");
    add(a, 0.25, plumbingish / nFields / 0.7, "mostly identifiers, categories and flags");
    if (chars !== null) add(a, 0.2, 1 - logRamp(chars, 200, 1500), "little text per record");
    else add(a, 0.1, 1, "content length not seen yet");
    if (f.observed) add(a, 0.1, f.rows <= 5000 ? 1 : 0, `${f.rows.toLocaleString("en-US")} records`);
    if (!(meta.is_reference || meta.record_kind === "reference")) a.score *= 0.5;
    if (geoPair || offsetPair) {
      a.score *= 0.7;
      a.evidence.push("but its fields have a more specific structure");
    }
  }

  const scores: ArchetypeScore[] = ARCHETYPE_IDS.map((id) => ({
    archetype: id,
    score: Math.round(clamp(acc[id].score) * PRIOR[id] * 1000) / 1000,
    evidence: acc[id].evidence,
  })).sort((x, y) => y.score - x.score || ARCHETYPE_IDS.indexOf(x.archetype) - ARCHETYPE_IDS.indexOf(y.archetype));

  const top = scores[0] as ArchetypeScore;
  // Nothing fits: a channel with no declared structure is read, so call it a document
  // (or a conversation when its threads are long).
  let primary = top.archetype;
  if (top.score < 0.3) primary = rpt !== null && rpt >= 8 ? "conversation" : "document";
  return {
    primary,
    secondary: scores.filter((s) => s.archetype !== primary && s.score > 0.5).map((s) => s.archetype),
    scores,
    predicted: !f.observed,
  };
}

/** The score of one archetype, 0 when absent. */
export function archetypeScore(archetypes: Archetypes, id: ArchetypeId): number {
  return archetypes.scores.find((s) => s.archetype === id)?.score ?? 0;
}
