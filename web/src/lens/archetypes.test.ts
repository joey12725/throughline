import { describe, expect, test } from "vitest";
import { ZOO_NAMES, metaOf, syntheticProfile } from "./__fixtures__";
import { sleepSample, usageSample } from "./__fixtures__/samples";
import { scoreArchetypes } from "./archetypes";
import { analyzeChannel } from "./engine";
import { declaredProfile } from "./profile";
import { inferRoles } from "./roles";
import type { ArchetypeId } from "./types";
import { ARCHETYPE_IDS } from "./types";

// Written by reading the 47 shapes. Profiles are synthetic: rounded envelope counts, no
// field values, so this is what the engine concludes from shape and volume alone.
const PRIMARY: Record<string, ArchetypeId> = {
  // A change log whose records carry the changed event's start and end.
  "calendar.changes": "interval",
  "calendar.events": "interval",
  "chat.discord": "conversation",
  "chat.instagram": "conversation",
  "chat.reddit": "conversation",
  "chat.sms": "conversation",
  "chat.snapchat": "conversation",
  // whole_thread is set, but there is one long message per thread and a subject.
  "email.message": "document",
  "gbrain.pages": "document",
  "gbrain.timeline": "document",
  "health.oura.daily": "measurement",
  // Every row has an end, but it is the UTC day the samples were summarised over.
  "health.oura.heartrate": "measurement",
  "health.oura.session": "measurement",
  "health.oura.sleep": "interval",
  "health.oura.workout": "interval",
  "health.weight": "measurement",
  // A reference channel, yet its latitude/longitude pair makes it a map.
  "location.places": "geo_track",
  "location.timeline": "geo_track",
  "morning.activity": "snapshot",
  "morning.calendar": "snapshot",
  "morning.email": "snapshot",
  "morning.events": "snapshot",
  "morning.finance": "snapshot",
  "morning.health": "snapshot",
  "morning.location": "snapshot",
  "morning.media": "snapshot",
  "morning.news": "snapshot",
  "morning.other": "snapshot",
  "morning.pipeline": "snapshot",
  "morning.tasks": "snapshot",
  "morning.weather": "snapshot",
  // Long summaries, but every record is a recording with a start and an end.
  "recordings.wave": "interval",
  "research.items": "feed_item",
  "research.results": "document",
  // No authors at all: detected from role and author_kind.
  "t3code.messages": "conversation",
  "t3code.projects": "registry",
  "t3code.resets": "gauge",
  "t3code.subscriptions": "registry",
  "t3code.threads": "registry",
  "t3code.usage": "gauge",
  "transcripts.wave": "transcript",
  "youtube.picks": "feed_item",
  "youtube.subscriptions": "feed_item",
  "youtube.transcripts": "transcript",
  "youtube.uploads": "feed_item",
  "youtube.watch": "feed_item",
  "youtube.watch_later": "feed_item",
};

function score(name: string) {
  const meta = metaOf(name);
  const p = syntheticProfile(name);
  return scoreArchetypes(meta, p, inferRoles(p, meta));
}

describe("primary archetype per channel", () => {
  test("the table covers every fixture channel", () => {
    expect(Object.keys(PRIMARY).sort()).toEqual([...ZOO_NAMES].sort());
  });

  test.each(Object.entries(PRIMARY))("%s is a %s", (name, expected) => {
    const a = score(name);
    expect(a.primary).toBe(expected);
    expect(a.scores.length).toBe(ARCHETYPE_IDS.length);
    const top = a.scores.find((s) => s.archetype === expected);
    expect(top?.evidence.length).toBeGreaterThan(0);
    for (const s of a.scores) {
      expect(s.score).toBeGreaterThanOrEqual(0);
      expect(s.score).toBeLessThanOrEqual(1);
    }
  });

  test("secondaries are scored, not exclusive", () => {
    expect(score("research.items").secondary).toContain("interval");
    expect(score("health.oura.sleep").secondary).toContain("measurement");
    expect(score("transcripts.wave").secondary).toEqual(expect.arrayContaining(["conversation", "interval"]));
    expect(score("email.message").secondary).toContain("conversation");
  });

  test("an interval of constant length is a period, not an event", () => {
    const meta = metaOf("health.oura.heartrate");
    const p = syntheticProfile(meta.name);
    Object.assign(p.envelope, { min_duration_s: 86399, avg_duration_s: 86399, max_duration_s: 86399 });
    const a = scoreArchetypes(meta, p, inferRoles(p, meta));
    expect(a.primary).toBe("measurement");
    expect(a.scores.find((s) => s.archetype === "interval")?.evidence.join(" ")).toMatch(/same length/);
  });

  test("a tested sawtooth strengthens the gauge", () => {
    const meta = metaOf("t3code.usage");
    const bare = syntheticProfile(meta.name);
    const withSample = syntheticProfile(meta.name);
    withSample.sample = usageSample();
    const a = scoreArchetypes(meta, bare, inferRoles(bare, meta)).scores.find((s) => s.archetype === "gauge");
    const b = scoreArchetypes(meta, withSample, inferRoles(withSample, meta)).scores.find((s) => s.archetype === "gauge");
    expect(b?.score ?? 0).toBeGreaterThan(a?.score ?? 1);
    expect(b?.evidence.join(" ")).toMatch(/climbs and drops back/);
  });
});

describe("declared fields only (a channel with no rows)", () => {
  test.each(ZOO_NAMES)("%s still gets roles, archetypes and predicted lenses", (name) => {
    const meta = metaOf(name, true);
    const profile = declaredProfile(meta);
    expect(profile.source).toBe("declared");
    const a = analyzeChannel(meta, profile);
    expect(a.errors).toEqual([]);
    expect(Object.keys(a.roles).length).toBe(meta.fields.length);
    expect(a.archetypes.predicted).toBe(true);
    expect(ARCHETYPE_IDS).toContain(a.archetypes.primary);
    const lenses = a.candidates.map((c) => c.spec.lens);
    expect(lenses).toContain("spine");
    expect(lenses).toContain("table");
    expect(lenses.length).toBeGreaterThanOrEqual(4);
    for (const c of a.candidates) {
      expect(c.spec.provenance.predicted).toBe(true);
      expect(c.spec.why).toMatch(/awaiting the first record/);
    }
  });

  test("the five empty channels of the installation get the archetype their fields predict", () => {
    const predicted = (name: string) => analyzeChannel(metaOf(name, true)).archetypes.primary;
    expect(predicted("youtube.watch")).toBe("feed_item");
    expect(predicted("youtube.watch_later")).toBe("feed_item");
    expect(predicted("youtube.subscriptions")).toBe("feed_item");
    expect(predicted("youtube.transcripts")).toBe("transcript");
    expect(predicted("health.oura.session")).toBe("measurement");
  });

  test("shape alone is stable: most channels predict the archetype their data confirms", () => {
    let same = 0;
    for (const name of ZOO_NAMES) if (analyzeChannel(metaOf(name, true)).archetypes.primary === PRIMARY[name]) same++;
    expect(same).toBeGreaterThanOrEqual(36);
  });

  test("a sample is not needed for the sleep stages to be recognised, but it confirms them", () => {
    const meta = metaOf("health.oura.sleep");
    const p = syntheticProfile(meta.name);
    expect(analyzeChannel(meta, p).wholes[0]?.tested).toBe(false);
    p.sample = sleepSample(60);
    expect(analyzeChannel(meta, p).wholes[0]?.tested).toBe(true);
  });
});
