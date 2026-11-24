import { describe, expect, test } from "vitest";
import { ZOO, ZOO_NAMES, allMetas, metaOf, syntheticProfile } from "./__fixtures__";
import { sleepSample } from "./__fixtures__/samples";
import { declaredProfile } from "./profile";
import { channelFamilies, fieldFamilies, inferRoles, mineDomain, mineUnit, partsOfWhole } from "./roles";
import type { RoleId, RoleMap } from "./types";
import { ROLE_IDS } from "./types";

function rolesFor(name: string): RoleMap {
  const meta = metaOf(name);
  return inferRoles(syntheticProfile(name), meta);
}

describe("golden roles over the 47 schemas (declared shape only)", () => {
  test("the fixture has the 47 shapes", () => {
    expect(ZOO.length).toBe(47);
    expect(ZOO.reduce((n, c) => n + c.fields.length, 0)).toBeGreaterThan(600);
  });

  test("every field gets exactly one valid role with evidence", () => {
    for (const name of ZOO_NAMES) {
      const roles = rolesFor(name);
      const meta = metaOf(name);
      expect(Object.keys(roles).sort()).toEqual(meta.fields.map((f) => f.name).sort());
      for (const r of Object.values(roles)) {
        expect(ROLE_IDS).toContain(r.role);
        expect(r.confidence).toBeGreaterThan(0);
        expect(r.confidence).toBeLessThanOrEqual(1);
        expect(r.evidence.length).toBeGreaterThan(0);
        for (const e of r.evidence) expect(e.length).toBeGreaterThan(3);
      }
    }
  });

  test("mapping_revision is bookkeeping in every channel that has it, never a measure", () => {
    let seen = 0;
    for (const name of ZOO_NAMES) {
      const r = rolesFor(name).mapping_revision;
      if (!r) continue;
      seen++;
      expect(r.role, name).toBe("bookkeeping");
      expect(r.plumbing).toBe(true);
    }
    expect(seen).toBeGreaterThan(30);
  });

  const expected: [string, string, RoleId, Partial<{ pair: string; unit: string }>?][] = [
    // pairs
    ["location.places", "latitude", "geo.lat", { pair: "longitude" }],
    ["location.places", "longitude", "geo.lon", { pair: "latitude" }],
    ["location.timeline", "latitude", "geo.lat", { pair: "longitude" }],
    ["calendar.changes", "event_start", "time.start", { pair: "event_end" }],
    ["calendar.changes", "event_end", "time.end", { pair: "event_start" }],
    ["research.items", "occurrence_start", "time.start", { pair: "occurrence_end" }],
    ["research.items", "occurrence_end", "time.end", { pair: "occurrence_start" }],
    ["health.oura.daily", "main_sleep_start", "time.start", { pair: "main_sleep_end" }],
    ["health.oura.daily", "main_sleep_end", "time.end", { pair: "main_sleep_start" }],
    ["transcripts.wave", "start_s", "time.offset", { pair: "end_s" }],
    ["transcripts.wave", "end_s", "time.offset", { pair: "start_s" }],
    ["youtube.transcripts", "start_s", "time.offset", { pair: "end_s" }],
    // durations, distances, masses
    ["health.oura.sleep", "total_sleep_s", "duration", { unit: "s" }],
    ["health.oura.sleep", "latency_s", "duration", { unit: "s" }],
    ["health.oura.workout", "duration_s", "duration", { unit: "s" }],
    ["youtube.uploads", "duration_s", "duration", { unit: "s" }],
    ["t3code.usage", "window_minutes", "duration", { unit: "min" }],
    ["t3code.usage", "time_left_hours", "duration", { unit: "h" }],
    ["t3code.resets", "hours_since_previous_reset", "duration", { unit: "h" }],
    ["health.oura.workout", "distance_m", "distance", { unit: "m" }],
    ["location.timeline", "accuracy_meters", "distance", { unit: "m" }],
    ["health.oura.daily", "equivalent_walking_distance_m", "distance", { unit: "m" }],
    ["health.weight", "weight_kg", "mass", { unit: "kg" }],
    ["health.weight", "weight", "mass"],
    // a minimum is not minutes
    ["health.oura.heartrate", "bpm_min", "measure", { unit: "bpm" }],
    ["health.oura.heartrate", "bpm_mean", "measure", { unit: "bpm" }],
    // scores, percents, probabilities
    ["health.oura.daily", "sleep_score", "score"],
    ["health.oura.daily", "readiness_score", "score"],
    ["health.oura.daily", "activity_score", "score"],
    ["health.oura.sleep", "readiness_score", "score"],
    ["health.oura.daily", "sleep_contrib_deep_sleep", "score"],
    ["health.oura.daily", "readiness_contrib_hrv_balance", "score"],
    ["health.oura.daily", "resilience_stress", "score"],
    ["t3code.usage", "used_percent", "percent", { unit: "%" }],
    ["t3code.usage", "left_percent", "percent", { unit: "%" }],
    ["t3code.usage", "window_elapsed_percent", "percent"],
    ["t3code.resets", "used_before_percent", "percent"],
    ["health.weight", "body_fat_pct", "percent"],
    ["health.oura.sleep", "efficiency", "percent"],
    ["health.oura.daily", "spo2_average", "percent"],
    ["youtube.picks", "progress_if_watched", "percent"],
    ["youtube.picks", "watch_probability", "probability"],
    ["youtube.picks", "junk_food_probability", "probability"],
    ["t3code.usage", "pace_points", "measure"],
    // counts
    ["health.oura.daily", "steps", "count"],
    ["morning.weather", "item_count", "count"],
    ["calendar.events", "attendee_count", "count"],
    ["research.results", "citation_count", "count"],
    ["email.message", "body_chars", "count"],
    // deadlines and bookkeeping times
    ["t3code.usage", "resets_at", "time.deadline"],
    ["t3code.resets", "next_reset_at", "time.deadline"],
    ["t3code.resets", "next_credit_expires_at", "time.deadline"],
    ["calendar.changes", "observed_at", "time.bookkeeping"],
    ["morning.weather", "collected_at", "time.bookkeeping"],
    ["youtube.picks", "scored_at", "time.bookkeeping"],
    ["health.weight", "server_timestamp", "time.bookkeeping"],
    ["t3code.projects", "last_updated", "time.bookkeeping"],
    // dates written as text
    ["health.oura.daily", "day", "date.text"],
    ["morning.weather", "report_date", "date.text"],
    ["research.results", "run_date", "date.text"],
    ["research.results", "window_start", "date.text", { pair: "window_end" }],
    // identifiers and group keys
    ["t3code.usage", "window_id", "group_key"],
    ["t3code.resets", "window_id", "group_key"],
    ["calendar.events", "series_id", "group_key"],
    ["calendar.changes", "series_id", "group_key"],
    ["research.items", "search_id", "group_key"],
    ["research.items", "stream", "group_key"],
    ["t3code.messages", "root_thread", "group_key"],
    ["t3code.messages", "t3_thread_id", "group_key"],
    ["youtube.uploads", "youtube_channel_id", "group_key"],
    ["calendar.events", "event_id", "identifier"],
    ["email.message", "gmail_message_id", "identifier"],
    ["youtube.picks", "video_id", "identifier"],
    ["gbrain.pages", "source_hash", "identifier"],
    ["research.results", "prompt_hash", "identifier"],
    ["youtube.transcripts", "segment", "identifier"],
    ["research.items", "position", "identifier"],
    ["health.oura.sleep", "sleep_period", "identifier"],
    ["location.places", "source_table", "bookkeeping"],
    ["chat.discord", "time_precision", "bookkeeping"],
    ["location.timeline", "time_basis", "bookkeeping"],
    // categories
    ["calendar.changes", "change", "category"],
    ["health.oura.sleep", "sleep_type", "category"],
    ["health.oura.workout", "intensity", "category"],
    ["youtube.picks", "verdict", "category"],
    ["health.oura.daily", "stress_day_summary", "category"],
    ["calendar.events", "event_type", "category"],
    ["chat.discord", "direction", "category.speaker"],
    ["t3code.messages", "role", "category.speaker"],
    ["t3code.messages", "author_kind", "category.speaker"],
    ["transcripts.wave", "speaker_label", "category.speaker"],
    ["morning.weather", "status", "category.status"],
    ["research.results", "status", "category.status"],
    ["calendar.events", "status", "category.status"],
    ["t3code.usage", "status", "category.status"],
    // text roles
    ["calendar.events", "title", "title"],
    ["email.message", "subject", "title"],
    ["research.results", "stream_title", "title"],
    ["calendar.events", "description", "body"],
    ["email.message", "snippet", "body"],
    ["research.results", "prompt", "body"],
    ["morning.weather", "error", "body"],
    ["youtube.picks", "url", "url"],
    ["calendar.events", "html_link", "url"],
    ["research.items", "ticket_url", "url"],
    ["calendar.events", "conference_url", "url"],
    ["email.message", "from_address", "actor.label"],
    ["calendar.events", "organizer_email", "actor.label"],
    ["calendar.events", "organizer_name", "actor.label"],
    ["youtube.uploads", "channel_title", "actor.label"],
    ["research.items", "organizer", "actor.label"],
    ["calendar.events", "place", "geo.label"],
    ["location.timeline", "place_name", "geo.label"],
    ["research.items", "venue", "geo.label"],
    ["research.items", "city", "geo.label"],
    // flags and json
    ["calendar.events", "all_day", "flag"],
    ["youtube.picks", "picked", "flag"],
    ["youtube.uploads", "is_short", "flag"],
    ["health.weight", "deleted", "flag.tombstone"],
    ["t3code.threads", "archived", "flag.tombstone"],
    ["youtube.watch_later", "removed", "flag.tombstone"],
    ["email.message", "labels", "json.tags"],
    ["gbrain.pages", "tags", "json.tags"],
    ["research.results", "cited_urls", "json.tags"],
    ["calendar.events", "attendees", "json.items"],
    ["email.message", "attachments", "json.items"],
    ["research.results", "citations", "json.items"],
    ["email.message", "to", "json.items"],
    ["morning.weather", "normalized", "json.object"],
    ["gbrain.pages", "frontmatter", "json.object"],
    ["t3code.subscriptions", "windows", "json.object"],
    // plain measures
    ["health.oura.daily", "vo2_max", "measure"],
    ["health.weight", "bmi", "measure"],
    ["health.oura.workout", "calories", "measure", { unit: "kcal" }],
    ["health.oura.daily", "breathing_disturbance_index", "measure"],
  ];

  test.each(expected)("%s.%s is %s", (channel, field, role, extra) => {
    const r = rolesFor(channel)[field];
    expect(r, `${channel}.${field} has a role`).toBeDefined();
    expect(r?.role).toBe(role);
    if (extra?.pair) expect(r?.pair).toBe(extra.pair);
    if (extra?.unit) expect(r?.unit).toBe(extra.unit);
  });

  test("descriptions give category domains before any row exists", () => {
    expect(rolesFor("calendar.changes").change?.domain).toEqual(["created", "changed", "cancelled", "removed", "restored"]);
    expect(rolesFor("youtube.picks").verdict?.domain).toEqual(["pick", "skip"]);
    expect(rolesFor("health.oura.workout").intensity?.domain).toEqual(["easy", "moderate", "hard"]);
    expect(rolesFor("morning.weather").status?.domain?.slice(0, 3)).toEqual(["ok", "zero_results", "empty"]);
  });
});

describe("description mining", () => {
  test("value lists", () => {
    expect(mineDomain("created, changed, cancelled, removed or restored")).toEqual(["created", "changed", "cancelled", "removed", "restored"]);
    expect(mineDomain("incoming, outgoing, or unknown relative to the owner")).toEqual(["incoming", "outgoing", "unknown"]);
    expect(mineDomain("opaque (busy) or transparent (free)")).toEqual(["opaque", "transparent"]);
    expect(mineDomain("Oura sleep type: long_sleep, sleep, late_nap, rest or deleted")).toEqual(["long_sleep", "sleep", "late_nap", "rest", "deleted"]);
    expect(mineDomain("Workout intensity (easy, moderate, hard)")).toEqual(["easy", "moderate", "hard"]);
    expect(mineDomain("Pipeline source name, e.g. weather or event_discovery")).toEqual(["weather", "event_discovery"]);
    expect(mineDomain("manual or automatic, for captions")).toEqual(["manual", "automatic"]);
    expect(mineDomain("position, capture, stay, or the table name for other rows")).toEqual(["position", "capture", "stay"]);
    expect(mineDomain("default, outOfOffice, focusTime, workingLocation, birthday, ...")).toEqual(["default", "outOfOffice", "focusTime", "workingLocation", "birthday"]);
  });
  test("prose that only looks like a list", () => {
    for (const d of [
      "Place or label name when the row has one",
      "Tracker, entity or device the row names",
      "Ticket, RSVP or signup URL",
      "Venue address or details",
      "Calendar name (summaryOverride, else summary)",
      "Subscription name: Claude Ultra, ChatGPT Pro 100, ...",
      "Sender address, lower case",
      "Event title",
      "Total calories (kcal)",
      "Oura sleep score (0-100)",
      "",
    ]) {
      expect(mineDomain(d), d).toEqual([]);
    }
  });
  test("units", () => {
    expect(mineUnit("Lowest sample, bpm")).toBe("bpm");
    expect(mineUnit("Average heart rate variability, ms")).toBe("ms");
    expect(mineUnit("Body temperature deviation from baseline, degrees C")).toBe("°C");
    expect(mineUnit("Energy burned, kcal")).toBe("kcal");
    expect(mineUnit("Event title")).toBeUndefined();
  });
});

describe("observed values sharpen the roles", () => {
  test("a constant category becomes bookkeeping; an id shared by many records a group key", () => {
    const meta = metaOf("health.oura.sleep");
    const p = declaredProfile(meta);
    const provider = p.fields.find((f) => f.name === "provider");
    if (provider) Object.assign(provider, { observed: 200, present: 200, fill: 1, distinct: 1, text: { avg_len: 4, max_len: 4, top: [{ value: "oura", n: 200 }], date_like: 0, url_like: 0 } });
    expect(inferRoles(p, meta).provider?.role).toBe("bookkeeping");

    const picks = metaOf("youtube.picks");
    const pp = declaredProfile(picks);
    const vid = pp.fields.find((f) => f.name === "video_id");
    if (vid) Object.assign(vid, { observed: 500, present: 500, fill: 1, distinct: 40, text: { avg_len: 11, max_len: 11, top: [], date_like: 0, url_like: 0 } });
    expect(inferRoles(pp, picks).video_id?.role).toBe("group_key");
  });

  test("a timestamp mostly in the future is a deadline; ISO-looking text is a date", () => {
    const meta = metaOf("calendar.events");
    const p = declaredProfile(meta);
    const f = p.fields.find((x) => x.name === "original_start");
    if (f) Object.assign(f, { observed: 50, time: { min: "2027-01-01T00:00:00Z", max: "2028-01-01T00:00:00Z", future_share: 0.9, lead_s: null } });
    expect(inferRoles(p, meta).original_start?.role).toBe("time.deadline");
    const tz = p.fields.find((x) => x.name === "start_time_zone");
    if (tz) Object.assign(tz, { observed: 50, distinct: 40, text: { avg_len: 10, max_len: 10, top: [], date_like: 1, url_like: 0 } });
    expect(inferRoles(p, meta).start_time_zone?.role).toBe("date.text");
  });
});

describe("families and wholes", () => {
  test("prefix and suffix families", () => {
    const meta = metaOf("health.oura.daily");
    const p = syntheticProfile(meta.name);
    const roles = inferRoles(p, meta);
    const fams = fieldFamilies(p.fields, roles);
    const byId = new Map(fams.map((f) => [f.id, f]));
    expect(byId.get("sleep_contrib_*")?.members.length).toBe(7);
    expect(byId.get("readiness_contrib_*")?.members.length).toBe(9);
    expect(byId.get("activity_contrib_*")?.members.length).toBe(6);
    expect(roles.sleep_contrib_timing?.family).toBe("sleep_contrib_*");
    // bookkeeping never joins a family
    expect(fams.some((f) => f.members.includes("mapping_revision"))).toBe(false);

    const sleep = metaOf("health.oura.sleep");
    const sp = syntheticProfile(sleep.name);
    const sfam = fieldFamilies(sp.fields, inferRoles(sp, sleep));
    expect(sfam.find((f) => f.id === "*_sleep_s")?.members.sort()).toEqual(["deep_sleep_s", "light_sleep_s", "rem_sleep_s", "total_sleep_s"]);
  });

  test("used_percent and left_percent are predicted complements with no rows", () => {
    const meta = metaOf("t3code.usage");
    const p = syntheticProfile(meta.name);
    const wholes = partsOfWhole(p, inferRoles(p, meta));
    const w = wholes.find((x) => x.parts.includes("used_percent"));
    expect(w?.parts.sort()).toEqual(["left_percent", "used_percent"]);
    expect(w?.whole).toBe(100);
    expect(w?.tested).toBe(false);
    const resets = metaOf("t3code.resets");
    const rp = syntheticProfile(resets.name);
    expect(partsOfWhole(rp, inferRoles(rp, resets))[0]?.parts.sort()).toEqual(["left_before_percent", "used_before_percent"]);
  });

  test("sums are tested on the sample when there is one", () => {
    const meta = metaOf("health.oura.sleep");
    const p = syntheticProfile(meta.name);
    p.sample = sleepSample(60);
    const wholes = partsOfWhole(p, inferRoles(p, meta));
    expect(wholes[0]?.tested).toBe(true);
    expect(wholes[0]?.whole).toBe("time_in_bed_s");
    expect(wholes[0]?.parts.sort()).toEqual(["awake_s", "deep_sleep_s", "light_sleep_s", "rem_sleep_s"]);

    const usage = metaOf("t3code.usage");
    const up = syntheticProfile(usage.name);
    up.sample = Array.from({ length: 40 }, (_x, i) => ({ used_percent: (i * 7) % 100, left_percent: 100 - ((i * 7) % 100), window_elapsed_percent: (i * 13) % 100 }));
    const uw = partsOfWhole(up, inferRoles(up, usage))[0];
    expect(uw).toMatchObject({ whole: 100, tested: true });
    expect(uw?.parts.sort()).toEqual(["left_percent", "used_percent"]);
  });

  test("a sample that contradicts the names withdraws the prediction", () => {
    const usage = metaOf("t3code.usage");
    const up = syntheticProfile(usage.name);
    up.sample = Array.from({ length: 40 }, (_x, i) => ({ used_percent: (i * 7) % 100, left_percent: (i * 3) % 50 }));
    const w = partsOfWhole(up, inferRoles(up, usage)).find((x) => x.parts.includes("used_percent"));
    expect(w?.tested ?? false).toBe(false);
    expect(w?.confidence ?? 0).toBeLessThan(0.5);
  });

  test("channels with the same field signature form families", () => {
    const fams = channelFamilies(allMetas());
    const chat = fams.find((f) => f.id === "chat.*");
    const morning = fams.find((f) => f.id === "morning.*");
    expect(chat?.channels.length).toBe(5);
    expect(morning?.channels.length).toBe(13);
    expect(morning?.shared_fields).toContain("status");
    expect(fams.length).toBe(2);
  });
});
