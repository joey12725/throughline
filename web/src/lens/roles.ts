// Semantic roles: one role per field, with a confidence and the evidence for it.
// Evidence comes from four places: the declared type, the tokens of the name, the
// description text and, when there are rows, observed values. Identifier and bookkeeping
// detection runs before measure detection, so a revision counter is never charted.

import { num } from "./profile";
import type {
  ChannelFamily,
  ChannelMeta,
  ChannelProfile,
  FieldFamily,
  FieldProfile,
  PartOfWhole,
  ResultProfile,
  Role,
  RoleId,
  RoleMap,
  Row,
} from "./types";

// ---------------------------------------------------------------- vocabulary

const set = (words: string) => new Set(words.split(/\s+/).filter(Boolean));

const SPEAKER_NAMES = set("role author_kind speaker_label speaker direction sender_kind from_me author_role speaker_role");
const URL_TOKENS = set("url uri link href permalink");
const TEXT_BOOKKEEPING_LAST = set("revision precision basis provenance table header path root dir file filename");
const ID_LAST = set("id uid uuid guid");
const HASH_LAST = set("hash sha digest key slug uid uuid guid");
const ID_NAMES_RECORD = set("reply_to in_reply_to references forked_from");
const ID_NAMES_GROUP = set("run parent_thread root_thread stream account");
const GROUP_STEMS = set(
  "series window thread project calendar guild channel environment playlist list stream search account parent root " +
    "group session run cluster source result conversation user device subscription plan page team org folder " +
    "category album artist author sender recipient owner",
);
const GROUP_CUE = /\bparent\b|belongs to|of the series|that found|top-level/i;
const ACTOR_NAMES = set("organizer author sender creator owner from_name from_address channel_title");
const ACTOR_CUE = /\b(publisher|publishing|sender|organizer|creator|hosting|subscribed channel)\b.*\b(title|name|email|address|organi[sz]ation)\b|display name/i;
const ACTOR_FIRST = set("from organizer creator sender author owner attendee contact user");
const GEO_LABEL_LAST = set("place venue address city location country region neighborhood neighbourhood street");
const TITLE_LAST = set("title subject headline");
const BODY_LAST = set(
  "description snippet reason notes note prompt error summary text body content message comment details query audience",
);
const STATUS_LAST = set("status state outcome");
const STATUS_WORDS = set(
  "ok error failed fail failure success succeeded unavailable unauthorized empty partial rejected allowed pending done " +
    "cancelled canceled confirmed tentative removed timeout zero_results skipped running complete completed warning",
);
const TOMBSTONE = set("deleted removed archived cancelled canceled is_deleted is_removed is_archived trashed");

const TIME_BOOKKEEPING = set(
  "observed collected scored server updated created edited added deleted fetched synced imported modified seen stored " +
    "ingested indexed snapshot timestamp",
);
const TIME_DEADLINE = set("resets reset expires expiry expiration due deadline next until scheduled");

const NUM_BOOKKEEPING = set("revision version schema");
const ORDINAL_NAMES = set("position segment index rank ordinal seq sequence thread_no");
const ORDINAL_CUE = /\b(0-based|1-based|ordinal|position within|index within|index of|rank is)\b/i;
const PERCENT_TOKENS = set("pct percent percentage");
const PROBABILITY_TOKENS = set("probability prob likelihood fraction");
const DURATION_LAST: Record<string, string> = {
  s: "s", sec: "s", secs: "s", seconds: "s", ms: "ms", min: "min", mins: "min", minutes: "min",
  h: "h", hrs: "h", hours: "h", days: "d",
};
const DISTANCE_LAST: Record<string, string> = { m: "m", meters: "m", metres: "m", km: "km", mi: "mi", miles: "mi", ft: "ft" };
const MASS_LAST: Record<string, string> = { kg: "kg", lb: "lb", lbs: "lb", g: "g" };
const CURRENCY_TOKENS = set("price cost amount usd eur gbp balance fee spend spent revenue");
const COUNT_LAST = set(
  "count total steps visits views likes items messages tokens alerts periods threads credits chars characters " +
    "length bytes words lines calls attendees replies",
);
const TAG_TOKENS = set("tags labels aliases ids urls keywords categories sources participants fields topics genres");
const ITEM_NAMES = set(
  "attendees attachments reactions citations frames to cc bcc reply_to items entries options recipients events results",
);

const UNIT_PATTERNS: [RegExp, string][] = [
  [/\bml\/kg\/min\b/i, "ml/kg/min"],
  [/\bdegrees C\b/, "°C"],
  [/\bbpm\b/i, "bpm"],
  [/\bkcal\b/i, "kcal"],
  [/\bbytes\b/i, "bytes"],
  [/\bcharacters\b/i, "chars"],
  [/[,(]\s*ms\b/, "ms"],
  [/\byears\b/i, "years"],
  [/\bpercentage points\b/i, "pp"],
  [/\bkilograms\b/i, "kg"],
  [/[,(]\s*percent\b|\bpercent$/i, "%"],
  [/\bseconds\b/i, "s"],
  [/\bmet(re|er)s\b/i, "m"],
  [/\bMET\b/, "MET"],
];

export function tokens(name: string): string[] {
  return name
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

/** A unit named in a field description ("Distance, metres" -> "m"). */
export function mineUnit(description: string | null | undefined): string | undefined {
  if (!description) return undefined;
  for (const [re, unit] of UNIT_PATTERNS) if (re.test(description)) return unit;
  return undefined;
}

// A value list inside prose: "created, changed, cancelled, removed or restored",
// "(easy, moderate, hard)", "Source status: ok, zero_results, ...", "e.g. weather or x".
const ITEM = /^[A-Za-z0-9][A-Za-z0-9_\-]*/;
const GLOSS = /^ \([^()]*\)/;
const SEPARATOR = /^(, or |, and | or |, )/;
const TERMINATOR =
  /^($|[.;)]|,? (when|relative|for|if|as|depending|in|by|from|of|to|that|which)\b|,? or (the|a|an|why|when)\b|,? ?(\.\.\.|\u2026)|: )/;

function parseList(text: string): string[] {
  const items: string[] = [];
  let rest = text;
  let best: string[] = [];
  for (;;) {
    const m = ITEM.exec(rest);
    if (!m) break;
    items.push(m[0]);
    rest = rest.slice(m[0].length);
    const g = GLOSS.exec(rest);
    if (g) rest = rest.slice(g[0].length);
    if (TERMINATOR.test(rest) && items.length >= 2) best = [...items];
    const sep = SEPARATOR.exec(rest);
    if (!sep) break;
    rest = rest.slice(sep[0].length);
  }
  return best;
}

/**
 * The value domain a description spells out, or [] when it does not. This is what lets a
 * channel with no rows still get categories.
 */
export function mineDomain(description: string | null | undefined): string[] {
  if (!description) return [];
  const starts = [0];
  const anchor = /(: |\(|e\.g\. |for example )/g;
  let m: RegExpExecArray | null;
  while ((m = anchor.exec(description)) !== null) starts.push(m.index + m[0].length);
  let best: string[] = [];
  for (const at of starts) {
    const found = parseList(description.slice(at));
    if (found.length > best.length) best = found;
  }
  const unique = [...new Set(best)];
  // Units and ranges in parentheses are not value domains.
  if (unique.every((v) => /^\d/.test(v))) return [];
  return unique.length >= 2 && unique.length <= 40 ? unique : [];
}

// ---------------------------------------------------------------- rules

interface Verdict {
  role: RoleId;
  confidence: number;
  evidence: string[];
  unit?: string;
  pair?: string;
  domain?: string[];
}

interface Ctx {
  names: Set<string>;
  byName: Map<string, FieldProfile>;
  wholeThread: boolean;
}

const v = (role: RoleId, confidence: number, evidence: string, extra: Partial<Verdict> = {}): Verdict => ({
  role,
  confidence,
  evidence: [evidence],
  ...extra,
});

function partner(name: string, ctx: Ctx, pairs: [string, string][]): { other: string; side: 0 | 1 } | null {
  const t = tokens(name);
  for (const [a, b] of pairs) {
    for (let i = 0; i < t.length; i++) {
      const tok = t[i];
      if (tok !== a && tok !== b) continue;
      const swapped = [...t];
      swapped[i] = tok === a ? b : a;
      const other = swapped.join("_");
      if (other !== name && ctx.names.has(other)) return { other, side: tok === a ? 0 : 1 };
    }
  }
  return null;
}

const START_END: [string, string][] = [
  ["start", "end"], ["started", "ended"], ["begin", "finish"], ["from", "until"], ["opened", "closed"],
];

function timestampRole(f: FieldProfile, ctx: Ctx): Verdict {
  const t = tokens(f.name);
  const has = (s: Set<string>) => t.some((x) => s.has(x));
  const future = f.time?.future_share ?? 0;
  if (has(TIME_DEADLINE)) return v("time.deadline", 0.8, `name says a time still to come ("${f.name}")`);
  if (f.observed >= 5 && future > 0.5) {
    return v("time.deadline", 0.75, `${Math.round(future * 100)}% of values lie in the future`);
  }
  if (has(TIME_BOOKKEEPING)) return v("time.bookkeeping", 0.8, `name says when the source handled the record ("${f.name}")`);
  const p = partner(f.name, ctx, START_END);
  if (p && ctx.byName.get(p.other)?.kind === f.kind) {
    return v(p.side === 0 ? "time.start" : "time.end", 0.85, `pairs with ${p.other} as start and end`, { pair: p.other });
  }
  const fl = partner(f.name, ctx, [["first", "last"]]);
  if (fl && ctx.byName.get(fl.other)?.kind === f.kind) {
    return v(fl.side === 0 ? "time.start" : "time.end", 0.55, `pairs with ${fl.other} as first and last`, { pair: fl.other });
  }
  if (t.includes("end") || t.includes("ended")) return v("time.end", 0.5, "name says an end, no start field beside it");
  if (t.includes("start") || t.includes("started")) return v("time.start", 0.5, "name says a start, no end field beside it");
  return v("time.start", 0.35, "a timestamp with no recognisable partner");
}

function jsonRole(f: FieldProfile): Verdict {
  const shape = f.json?.shape;
  if (shape === "array_scalar") return v("json.tags", 0.9, "sampled values are arrays of plain values");
  if (shape === "array_object") return v("json.items", 0.9, "sampled values are arrays of objects");
  if (shape === "object") return v("json.object", 0.9, "sampled values are objects");
  const t = tokens(f.name);
  const d = f.description;
  if (ITEM_NAMES.has(f.name) || /\[\{/.test(d) || /\bwith titles\b|\bmetadata\b/i.test(d)) {
    return v("json.items", 0.55, "name or description says a list of structured items");
  }
  if (
    t.some((x) => TAG_TOKENS.has(x)) ||
    /\b(array|list) of\b|label names|label ids|\burls\b|\blines of\b|every distinct|collections present|fields that/i.test(d)
  ) {
    return v("json.tags", 0.55, "name or description says a list of plain values");
  }
  return v("json.object", 0.4, "a JSON value; structure not seen yet");
}

function numberRole(f: FieldProfile, ctx: Ctx): Verdict {
  const t = tokens(f.name);
  const last = t[t.length - 1] ?? "";
  const first = t[0] ?? "";
  const d = f.description;
  const unit = mineUnit(d);
  const s = f.numeric;
  const seen = f.observed >= 20 && s !== undefined;
  const has = (words: Set<string>) => t.some((x) => words.has(x));

  if (has(NUM_BOOKKEEPING)) return v("bookkeeping", 0.95, `name says a revision counter ("${f.name}"), not a quantity`);
  if (seen && s.min === s.max) return v("bookkeeping", 0.8, `the same value (${s.min}) in every record`);
  if (ID_LAST.has(last)) return v("identifier", 0.85, `name ends in "${last}"`);
  if (ORDINAL_NAMES.has(f.name) || ORDINAL_NAMES.has(last) && last !== "index" || ORDINAL_CUE.test(d)) {
    return v("identifier", 0.6, "an index or position inside its group, not a quantity");
  }

  // Pairs.
  if (["lat", "latitude"].includes(last)) {
    const other = ["lon", "lng", "long", "longitude"].map((x) => [...t.slice(0, -1), x].join("_")).find((x) => ctx.names.has(x));
    if (other) return v("geo.lat", 0.95, `pairs with ${other} as coordinates`, { pair: other, unit: "°" });
  }
  if (["lon", "lng", "long", "longitude"].includes(last)) {
    const other = ["lat", "latitude"].map((x) => [...t.slice(0, -1), x].join("_")).find((x) => ctx.names.has(x));
    if (other) return v("geo.lon", 0.95, `pairs with ${other} as coordinates`, { pair: other, unit: "°" });
  }
  if ((last === "s" || last === "ms") && t.length >= 2) {
    const p = partner(f.name, ctx, [["start", "end"]]);
    if (p) {
      return v("time.offset", ctx.wholeThread ? 0.9 : 0.8, `pairs with ${p.other} as offsets inside a recording`, {
        pair: p.other,
        unit: last,
      });
    }
    if (first === "at" || t.includes("offset")) return v("time.offset", 0.6, "name says an offset in seconds", { unit: last });
  }

  if (has(PERCENT_TOKENS)) return v("percent", 0.9, `name says percent ("${f.name}")`, { unit: "%" });
  if (has(PROBABILITY_TOKENS) || /\(0-1\)|\bprobability\b/i.test(d)) {
    return v("probability", 0.9, "name or description says a probability between 0 and 1");
  }
  if (last === "score" || t.includes("contrib") || /\bscore\b.*\(0-100\)|\bcontributor\b/i.test(d)) {
    return v("score", 0.9, "name or description says a score", { unit: /\(0-100\)/.test(d) ? "0-100" : undefined });
  }
  if (/\bpercent\b|\(0-100\)/i.test(d) && !/percentage points/i.test(d)) {
    return v("percent", 0.75, "description says percent", { unit: "%" });
  }
  if (t.includes("met")) return v("measure", 0.6, "a metabolic-equivalent measure", { unit: last === "minutes" ? "MET min" : "MET" });
  // "bpm_min" is a minimum, not minutes: a unit in the description outranks a short suffix.
  const suffixOk = (u: string | undefined) => u !== undefined && (unit === undefined || unit === u || last.length > 3);
  if (suffixOk(DURATION_LAST[last]) && t.length >= 2 && !(last === "min" && ctx.names.has([...t.slice(0, -1), "max"].join("_")))) {
    return v("duration", 0.9, `name ends in a time unit ("_${last}")`, { unit: DURATION_LAST[last] });
  }
  if (DURATION_LAST[first] && first.length > 2 && t.length >= 2) {
    return v("duration", 0.8, `name starts with a time unit ("${first}_")`, { unit: DURATION_LAST[first] });
  }
  if (t.includes("duration") || t.includes("elapsed")) return v("duration", 0.8, "name says a duration", { unit: unit ?? "s" });
  if ((suffixOk(DISTANCE_LAST[last]) && t.length >= 2) || first === "meters" || first === "metres" || t.includes("distance")) {
    return v("distance", 0.9, "name says a distance", { unit: DISTANCE_LAST[last] ?? unit ?? "m" });
  }
  if ((suffixOk(MASS_LAST[last]) && t.length >= 2) || f.name === "weight" || f.name === "mass") {
    return v("mass", 0.9, "name says a mass", { unit: MASS_LAST[last] });
  }
  if (has(CURRENCY_TOKENS)) return v("currency", 0.7, `name says money ("${f.name}")`);
  if (COUNT_LAST.has(last) || first === "num" || /^(number|count) of\b/i.test(d)) {
    return v("count", 0.85, "name or description says a count", { unit: unit === "bytes" || unit === "chars" ? unit : undefined });
  }
  if (unit === "s") return v("duration", 0.7, "description gives seconds", { unit: "s" });
  if (unit === "m") return v("distance", 0.7, "description gives metres", { unit: "m" });
  if (unit === "bytes") return v("count", 0.6, "description gives bytes", { unit: "bytes" });

  if (seen && s.min >= 0 && s.max <= 1 && !s.integral) {
    return v("probability", 0.5, `values stay between ${fmt(s.min)} and ${fmt(s.max)}`);
  }
  if (seen && s.integral && s.min >= 0 && s.max <= 1 && (f.distinct ?? 3) <= 2) {
    return v("flag", 0.5, "only 0 and 1 occur");
  }
  return v("measure", 0.6, f.kind === "integer" ? "a whole number with no other reading" : "a number with no other reading", { unit });
}

function fmt(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

function statusLike(name: string, domain: string[]): boolean {
  const t = tokens(name);
  if (STATUS_LAST.has(t[t.length - 1] ?? "")) return true;
  const hits = domain.filter((d) => STATUS_WORDS.has(d.toLowerCase())).length;
  return hits >= 2 && hits * 2 >= domain.length;
}

function textRole(f: FieldProfile, ctx: Ctx): Verdict {
  const t = tokens(f.name);
  const last = t[t.length - 1] ?? "";
  const first = t[0] ?? "";
  const d = f.description;
  const s = f.text;
  const seen = f.observed >= 20 && s !== undefined;
  const distinct = f.distinct ?? null;
  const ratio = seen && distinct !== null ? distinct / f.observed : null;
  const top = s?.top.map((x) => x.value) ?? [];

  if (SPEAKER_NAMES.has(f.name)) {
    const domain = top.length ? top.slice(0, 12) : mineDomain(d);
    return v("category.speaker", 0.85, `name says who is speaking ("${f.name}")`, { domain: domain.length ? domain : undefined });
  }
  if (URL_TOKENS.has(last) || (seen && s.url_like >= 0.8)) {
    return v("url", seen && s.url_like >= 0.8 ? 0.95 : 0.85, URL_TOKENS.has(last) ? `name ends in "${last}"` : "values start with http");
  }
  if (TEXT_BOOKKEEPING_LAST.has(last) && t.length >= 2) {
    return v("bookkeeping", 0.75, `name says how the source produced the record ("${f.name}")`);
  }
  if (seen && distinct !== null && distinct <= 1) return v("bookkeeping", 0.8, "the same value in every record");

  if (last === "email") return v("actor.label", 0.8, `name ends in "email"`);

  // Identifiers and group keys.
  const idByName = ID_LAST.has(last) || HASH_LAST.has(last) || ID_NAMES_RECORD.has(f.name) || ID_NAMES_GROUP.has(f.name);
  const idByText = /\b(id|identifier)\b/i.test(d) && (s === undefined || s.avg_len < 80);
  if (idByName || idByText) {
    const stems = t.filter((x) => !ID_LAST.has(x));
    const groupish =
      !HASH_LAST.has(last) || ID_LAST.has(last)
        ? ID_NAMES_GROUP.has(f.name) || (!ID_NAMES_RECORD.has(f.name) && (stems.some((x) => GROUP_STEMS.has(x)) || GROUP_CUE.test(d)))
        : false;
    const why = idByName ? `name reads as an identifier ("${f.name}")` : "description calls it an id";
    if (ratio !== null) {
      if (ratio >= 0.9) return v("identifier", 0.9, `${why}; nearly every record has its own value`);
      return v("group_key", 0.85, `${why}; ${distinct} values shared by ${f.observed} records`);
    }
    return groupish
      ? v("group_key", 0.6, `${why}; it names something records belong to`)
      : v("identifier", 0.7, why);
  }

  // Dates written as text.
  const nameDate = last === "day" || last === "date";
  const textPair = (last === "start" || last === "end") && partner(f.name, ctx, [["start", "end"]]);
  if (seen && s.date_like >= 0.8) return v("date.text", 0.9, "sampled values read as ISO dates");
  if ((nameDate || textPair) && !(seen && s.top.length > 0 && s.date_like < 0.2 && f.observed >= 20 && top.every((x) => !/^\d{4}-/.test(x)))) {
    return v("date.text", nameDate ? 0.7 : 0.45, nameDate ? `name ends in "${last}"` : `text pair with ${(textPair as { other: string }).other}`, {
      pair: textPair ? textPair.other : undefined,
    });
  }

  // A value domain spelled out in the description, or seen.
  const mined = mineDomain(d);
  if (mined.length >= 2) {
    const domain = top.length >= 2 && top.length <= 30 ? top : mined;
    return v(statusLike(f.name, domain) ? "category.status" : "category", 0.75, `description lists its values: ${mined.slice(0, 5).join(", ")}`, { domain });
  }
  if (STATUS_LAST.has(last)) {
    return v("category.status", 0.7, `name ends in "${last}"`, { domain: top.length ? top.slice(0, 12) : undefined });
  }

  if (ACTOR_NAMES.has(f.name) || last === "email" || ((last === "name" || last === "address") && ACTOR_FIRST.has(first)) || ACTOR_CUE.test(d)) {
    return v("actor.label", 0.75, "name or description says a person or organisation");
  }
  if (GEO_LABEL_LAST.has(last) || f.name === "place_name") return v("geo.label", 0.8, `name says a place ("${f.name}")`);

  const fewShort = seen && distinct !== null && distinct <= 12 && s.avg_len < 40;
  if (TITLE_LAST.has(last) || f.name === "name") {
    return v("title", 0.8, `name says a title ("${f.name}")`);
  }
  if (BODY_LAST.has(last)) {
    if (fewShort) return v("category", 0.6, `${distinct} short values`, { domain: top });
    return v("body", 0.7, `name says free text ("${f.name}")`);
  }

  if (seen && distinct !== null && ratio !== null) {
    if (s.avg_len >= 80) return v("body", 0.7, `long text (about ${Math.round(s.avg_len)} characters)`);
    if (distinct <= 30 || ratio <= 0.2) {
      return v(statusLike(f.name, top) ? "category.status" : "category", distinct <= 30 ? 0.8 : 0.55, `${distinct} distinct values`, {
        domain: top.slice(0, 12),
      });
    }
    if (ratio >= 0.9) return s.avg_len >= 25 ? v("title", 0.5, "a different short text in nearly every record") : v("identifier", 0.5, "a different value in nearly every record");
    return v("category", 0.45, `${distinct} distinct values`, { domain: top.slice(0, 12) });
  }
  return v("category", 0.3, "a short text field; values not seen yet");
}

// Result columns named like the message envelope inherit its meaning.
const ENVELOPE_PRIORS: Record<string, [RoleId, string[]]> = {
  happened_at: ["time.start", ["timestamp", "date"]],
  record_time: ["time.start", ["timestamp", "date"]],
  effective_time: ["time.start", ["timestamp", "date"]],
  started_at: ["time.start", ["timestamp", "date"]],
  said_at: ["time.start", ["timestamp", "date"]],
  bucket: ["time.start", ["timestamp", "date"]],
  day: ["time.start", ["timestamp", "date"]],
  week: ["time.start", ["timestamp", "date"]],
  month: ["time.start", ["timestamp", "date"]],
  ended_at: ["time.end", ["timestamp"]],
  ingested_at: ["time.bookkeeping", ["timestamp"]],
  created_at: ["time.bookkeeping", ["timestamp"]],
  lat: ["geo.lat", ["number", "integer"]],
  lon: ["geo.lon", ["number", "integer"]],
  id: ["identifier", ["integer", "text"]],
  external_id: ["identifier", ["text"]],
  message_id: ["identifier", ["integer"]],
  record_id: ["identifier", ["text"]],
  payload_hash: ["identifier", ["text"]],
  version: ["bookkeeping", ["integer"]],
  thread_no: ["identifier", ["integer"]],
  thread_id: ["group_key", ["integer"]],
  channel_id: ["group_key", ["integer"]],
  person_id: ["group_key", ["integer"]],
  conversation_id: ["group_key", ["integer"]],
  content: ["body", ["text"]],
  preview: ["body", ["text"]],
  excerpt: ["body", ["text"]],
  thread_title: ["title", ["text"]],
  author_handle: ["actor.label", ["text"]],
  display_name: ["actor.label", ["text"]],
  person_handle: ["actor.label", ["text"]],
  author: ["actor.label", ["text"]],
  person: ["actor.label", ["text"]],
  channel: ["category", ["text"]],
  plugin: ["category", ["text"]],
  n: ["count", ["integer", "number"]],
  count: ["count", ["integer", "number"]],
  total: ["count", ["integer", "number"]],
  rows: ["count", ["integer", "number"]],
  messages: ["count", ["integer", "number"]],
  records: ["count", ["integer", "number"]],
  weekday: ["category", ["integer"]],
  hour: ["category", ["integer"]],
};

const PLUMBING: ReadonlySet<RoleId> = new Set<RoleId>(["identifier", "bookkeeping", "time.bookkeeping"]);

export function isPlumbing(role: RoleId): boolean {
  return PLUMBING.has(role);
}

const MEASURE_ROLES: ReadonlySet<RoleId> = new Set<RoleId>([
  "duration", "distance", "mass", "percent", "probability", "score", "count", "currency", "measure",
]);

/** Roles that carry a chartable quantity. */
export function isMeasure(role: RoleId): boolean {
  return MEASURE_ROLES.has(role);
}

const CATEGORY_ROLES: ReadonlySet<RoleId> = new Set<RoleId>(["category", "category.speaker", "category.status", "flag", "flag.tombstone"]);

export function isCategory(role: RoleId): boolean {
  return CATEGORY_ROLES.has(role);
}

function finish(f: FieldProfile, verdict: Verdict): Role {
  const role: Role = {
    field: f.name,
    role: verdict.role,
    confidence: Math.max(0, Math.min(1, verdict.confidence)),
    evidence: [`declared ${f.kind}`, ...verdict.evidence].slice(f.declared ? 0 : 1),
    plumbing: PLUMBING.has(verdict.role),
  };
  if (verdict.unit) role.unit = verdict.unit;
  if (verdict.pair) role.pair = verdict.pair;
  if (verdict.domain && verdict.domain.length > 0) role.domain = verdict.domain;
  return role;
}

/**
 * Infer one role per field. Works from declared shape alone (a channel with no rows) and
 * sharpens with observed statistics when the profile has them. For a SQL result pass the
 * result profile; columns named like the message envelope inherit its meaning.
 */
export function inferRoles(
  profile: Pick<ChannelProfile, "fields"> | ResultProfile,
  meta?: Pick<ChannelMeta, "whole_thread"> | null,
): RoleMap {
  const fields: FieldProfile[] = "columns" in profile ? profile.columns : profile.fields;
  const isResult = "columns" in profile;
  const ctx: Ctx = {
    names: new Set(fields.map((f) => f.name)),
    byName: new Map(fields.map((f) => [f.name, f])),
    wholeThread: meta?.whole_thread ?? false,
  };
  const out: RoleMap = {};
  let sawTime = false;
  for (const f of fields) {
    let verdict: Verdict | null = null;
    if (isResult) {
      const prior = ENVELOPE_PRIORS[f.name];
      if (prior && prior[1].includes(f.kind)) verdict = v(prior[0], 0.8, `named like the envelope column ${f.name}`);
    }
    if (!verdict) {
      switch (f.kind) {
        case "boolean":
          verdict = TOMBSTONE.has(f.name)
            ? v("flag.tombstone", 0.9, `name says the record was withdrawn ("${f.name}")`)
            : v("flag", 0.9, "a yes/no field");
          break;
        case "timestamp":
        case "date":
          verdict = timestampRole(f, ctx);
          if (isResult && !sawTime && verdict.role === "time.start" && verdict.confidence < 0.5) {
            verdict = v("time.start", 0.6, "the first time column of the result");
          }
          break;
        case "json":
          verdict = jsonRole(f);
          break;
        case "integer":
        case "number":
          verdict = numberRole(f, ctx);
          break;
        case "interval":
          verdict = v("duration", 0.8, "an interval column");
          break;
        default:
          verdict = textRole(f, ctx);
      }
    }
    if (verdict.role === "time.start") sawTime = true;
    out[f.name] = finish(f, verdict);
  }
  for (const fam of fieldFamilies(fields, out)) {
    for (const m of fam.members) {
      const r = out[m];
      if (r) r.family = fam.id;
    }
  }
  return out;
}

// ---------------------------------------------------------------- families

/**
 * Fields that share a name prefix (sleep_contrib_*) or suffix (*_sleep_s) and a role.
 * A field joins the most specific family that still has three members.
 */
export function fieldFamilies(fields: Pick<FieldProfile, "name">[], roles: RoleMap): FieldFamily[] {
  const numeric = fields.filter((f) => {
    const r = roles[f.name];
    return r !== undefined && isMeasure(r.role) && !r.hidden;
  });
  const toks = new Map(numeric.map((f) => [f.name, tokens(f.name)]));
  const taken = new Set<string>();
  const out: FieldFamily[] = [];
  const collect = (kind: "prefix" | "suffix") => {
    const buckets = new Map<string, string[]>();
    for (const [name, t] of toks) {
      if (taken.has(name)) continue;
      // Suffix families ignore a trailing unit token: deep_sleep_s and rem_sleep_s share "sleep".
      const body = kind === "suffix" && t.length > 2 && (t[t.length - 1] as string).length <= 2 ? t.slice(0, -1) : t;
      const unitTail = body.length < t.length ? `_${t[t.length - 1]}` : "";
      for (let len = body.length - 1; len >= 1; len--) {
        const part = kind === "prefix" ? body.slice(0, len) : body.slice(body.length - len);
        const key = `${len}|${roles[name]?.role}|${kind === "prefix" ? `${part.join("_")}_*` : `*_${part.join("_")}${unitTail}`}`;
        const list = buckets.get(key) ?? [];
        list.push(name);
        buckets.set(key, list);
      }
    }
    const ordered = [...buckets.entries()]
      .filter(([, members]) => members.length >= 3)
      .sort((a, b) => Number(b[0].split("|")[0]) - Number(a[0].split("|")[0]) || b[1].length - a[1].length);
    for (const [key, members] of ordered) {
      const free = members.filter((m) => !taken.has(m));
      if (free.length < 3) continue;
      const [, role, id] = key.split("|") as [string, RoleId, string];
      for (const m of free) taken.add(m);
      const units = new Set(free.map((m) => roles[m]?.unit).filter(Boolean));
      out.push({ id, kind, members: free, role, unit: units.size === 1 ? ([...units][0] as string) : undefined });
    }
  };
  collect("prefix");
  collect("suffix");
  return out;
}

const COMPLEMENTS: [string, string][] = [
  ["used", "left"], ["used", "remaining"], ["used", "free"], ["used", "available"], ["done", "remaining"], ["spent", "left"],
];

function close(a: number, b: number, tolerance = 0.02): boolean {
  const scale = Math.max(Math.abs(a), Math.abs(b), 1e-9);
  return Math.abs(a - b) <= tolerance * scale + 1e-9;
}

/**
 * Part-of-whole relations among same-unit siblings: tested on the sampled rows when there
 * are any (deep + light + rem + awake = time in bed; used + left = 100), predicted from
 * complementary names when there are none.
 */
export function partsOfWhole(profile: Pick<ChannelProfile, "fields" | "sample">, roles: RoleMap): PartOfWhole[] {
  const out: PartOfWhole[] = [];
  const groups = new Map<string, string[]>();
  for (const f of profile.fields) {
    const r = roles[f.name];
    if (!r || r.hidden || !["duration", "percent", "count", "distance", "mass", "currency", "measure"].includes(r.role)) continue;
    if (r.role === "measure" && !r.unit) continue;
    const key = `${r.role}|${r.unit ?? ""}`;
    const list = groups.get(key) ?? [];
    list.push(f.name);
    groups.set(key, list);
  }
  const sample: Row[] = profile.sample ?? [];
  const claimed = new Set<string>();

  for (const [key, members] of groups) {
    const [role, unit] = key.split("|") as [string, string];
    if (members.length < 2) continue;
    const rows = sample
      .map((r) => members.map((m) => num(r[m])))
      .filter((vals) => vals.filter((x) => x !== null).length >= 2);
    let found = false;
    if (rows.length >= 8 && members.length <= 14) {
      // Rank candidate wholes by mean, largest first, then search subsets of the rest.
      const mean = (i: number) => {
        const xs = rows.map((r) => r[i]).filter((x): x is number => x !== null && x !== undefined);
        return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;
      };
      const order = members.map((_m, i) => i).sort((a, b) => mean(b) - mean(a));
      for (const w of order) {
        const others = order.filter((i) => i !== w && mean(i) > 0).slice(0, 10);
        let best: number[] | null = null;
        for (let mask = 3; mask < 1 << others.length; mask++) {
          const subset = others.filter((_o, bit) => mask & (1 << bit));
          if (subset.length < 2 || (best && subset.length <= best.length)) continue;
          let ok = 0;
          let tested = 0;
          for (const r of rows) {
            const whole = r[w];
            const parts = subset.map((i) => r[i]);
            if (whole === null || whole === undefined || parts.some((x) => x === null || x === undefined)) continue;
            tested++;
            if (close((parts as number[]).reduce((a, b) => a + b, 0), whole)) ok++;
          }
          if (tested >= 8 && ok / tested >= 0.9) best = subset;
        }
        if (best) {
          const parts = best.map((i) => members[i] as string);
          const whole = members[w] as string;
          out.push({
            parts,
            whole,
            unit: unit || undefined,
            confidence: 0.95,
            tested: true,
            evidence: [`${parts.join(" + ")} equals ${whole} in the sampled rows`],
          });
          for (const p of parts) claimed.add(p);
          claimed.add(whole);
          found = true;
          break;
        }
      }
      // A constant total (used + left = 100).
      if (!found) {
        const idx = members.map((_m, i) => i).filter((i) => mean(i) !== 0).slice(0, 10);
        let best: { subset: number[]; total: number } | null = null;
        for (let mask = 3; mask < 1 << idx.length; mask++) {
          const subset = idx.filter((_o, bit) => mask & (1 << bit));
          if (subset.length < 2 || (best && subset.length <= best.subset.length)) continue;
          const sums: number[] = [];
          for (const r of rows) {
            const parts = subset.map((i) => r[i]);
            if (parts.some((x) => x === null || x === undefined)) continue;
            sums.push((parts as number[]).reduce((a, b) => a + b, 0));
          }
          if (sums.length < 8) continue;
          const sorted = [...sums].sort((a, b) => a - b);
          const mid = sorted[sorted.length >> 1] as number;
          const round = [1, 100, 24, 60, 1440, 86400].find((c) => close(mid, c, 0.005));
          if (round === undefined) continue;
          if (sums.filter((x) => close(x, round)).length / sums.length >= 0.9) best = { subset, total: round };
        }
        if (best) {
          const parts = best.subset.map((i) => members[i] as string);
          out.push({
            parts,
            whole: best.total,
            unit: unit || undefined,
            confidence: 0.95,
            tested: true,
            evidence: [`${parts.join(" + ")} adds up to ${best.total} in the sampled rows`],
          });
          for (const p of parts) claimed.add(p);
          found = true;
        }
      }
    }
    if (found) continue;

    // Predicted from names: complements of one percent, or a family with a "total" member.
    if (role === "percent") {
      for (const m of members) {
        if (claimed.has(m)) continue;
        const t = tokens(m);
        for (const [a, b] of COMPLEMENTS) {
          const i = t.indexOf(a);
          if (i < 0) continue;
          const swapped = [...t];
          swapped[i] = b;
          const other = swapped.join("_");
          if (members.includes(other) && !claimed.has(other)) {
            out.push({
              parts: [m, other],
              whole: 100,
              unit: "%",
              confidence: rows.length >= 8 ? 0.3 : 0.6,
              tested: false,
              evidence: [`names are complements (${a} / ${b}) of one percentage`],
            });
            claimed.add(m);
            claimed.add(other);
          }
        }
      }
    }
    if (rows.length < 8 && role === "duration") {
      const total = members.find((m) => tokens(m).includes("total") && !claimed.has(m));
      if (total) {
        const stem = tokens(total).filter((x) => x !== "total");
        const parts = members.filter((m) => {
          if (m === total || claimed.has(m)) return false;
          const t = tokens(m);
          return stem.length > 0 && stem.every((x) => t.includes(x)) && t.length === stem.length + 1;
        });
        if (parts.length >= 2) {
          out.push({
            parts,
            whole: total,
            unit: unit || undefined,
            confidence: 0.4,
            tested: false,
            evidence: [`${parts.join(", ")} share the name of ${total}; the sum is not checked yet`],
          });
          for (const p of parts) claimed.add(p);
          claimed.add(total);
        }
      }
    }
  }
  return out.sort((a, b) => b.confidence - a.confidence || b.parts.length - a.parts.length);
}

/** Channels whose declared fields are (nearly) the same: small multiples at no cost to the source. */
export function channelFamilies(metas: Pick<ChannelMeta, "name" | "fields">[], threshold = 0.85): ChannelFamily[] {
  const groups: { names: string[]; base: Set<string> }[] = [];
  for (const m of metas) {
    if (m.fields.length === 0) continue;
    const sig = new Set(m.fields.map((f) => `${f.name}:${f.type}`));
    let home: { names: string[]; base: Set<string> } | undefined;
    for (const g of groups) {
      let shared = 0;
      for (const s of sig) if (g.base.has(s)) shared++;
      const jaccard = shared / (sig.size + g.base.size - shared);
      if (jaccard >= threshold) {
        home = g;
        break;
      }
    }
    if (home) home.names.push(m.name);
    else groups.push({ names: [m.name], base: sig });
  }
  const byName = new Map(metas.map((m) => [m.name, m]));
  return groups
    .filter((g) => g.names.length >= 2)
    .map((g) => {
      const lists = g.names.map((n) => new Set((byName.get(n)?.fields ?? []).map((f) => f.name)));
      const first = lists[0] ?? new Set<string>();
      const shared = [...first].filter((name) => lists.every((l) => l.has(name)));
      const prefix = commonPrefix(g.names);
      return { id: prefix ? `${prefix}*` : g.names.join("+"), channels: [...g.names].sort(), shared_fields: shared };
    });
}

function commonPrefix(names: string[]): string {
  if (names.length === 0) return "";
  let p = names[0] as string;
  for (const n of names) {
    while (p && !n.startsWith(p)) p = p.slice(0, -1);
  }
  const cut = p.lastIndexOf(".");
  return cut >= 0 ? p.slice(0, cut + 1) : "";
}
