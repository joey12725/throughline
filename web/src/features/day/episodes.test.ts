import { describe, it, expect } from "vitest";
import {
  addDays,
  parseDate,
  localHour,
  hourOffset,
  groupEpisodes,
  dayVitals,
  todayInZone,
  type DayRecord,
} from "./episodes";

function rec(over: Partial<DayRecord> & { id: number; happenedAt: string }): DayRecord {
  return {
    channel: "chat.x",
    localTime: over.happenedAt.replace("Z", "").slice(0, 19),
    content: "hi",
    ...over,
  };
}

describe("parseDate", () => {
  it("accepts YYYY-MM-DD and rejects junk", () => {
    expect(parseDate("2026-11-20")).toBe("2026-11-20");
    expect(parseDate("2026-13-20")).toBeNull();
    expect(parseDate("nope")).toBeNull();
    expect(parseDate(null)).toBeNull();
  });
});

describe("addDays", () => {
  it("moves forward and backward across month and year boundaries", () => {
    expect(addDays("2026-11-20", 1)).toBe("2026-11-21");
    expect(addDays("2026-11-20", -1)).toBe("2026-11-19");
    expect(addDays("2026-11-30", 1)).toBe("2026-12-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2024-02-28", 1)).toBe("2024-02-29"); // leap year
  });
});

describe("localHour", () => {
  it("turns a naive local time into a fraction of the day", () => {
    expect(localHour("2026-11-20T00:00:00")).toBeCloseTo(0, 6);
    expect(localHour("2026-11-20T12:30:00")).toBeCloseTo(12.5, 6);
    expect(localHour("2026-11-20T23:45:00")).toBeCloseTo(23.75, 6);
  });
});

describe("hourOffset", () => {
  it("is the local hour for same-day records", () => {
    expect(hourOffset("2026-11-20T09:30:00", "2026-11-20")).toBeCloseTo(9.5, 6);
  });
  it("goes negative for the previous evening and past 24 for the next morning", () => {
    expect(hourOffset("2026-11-19T22:00:00", "2026-11-20")).toBeCloseTo(-2, 6);
    expect(hourOffset("2026-11-21T07:00:00", "2026-11-20")).toBeCloseTo(31, 6);
  });
});

describe("groupEpisodes", () => {
  it("splits on gaps larger than the threshold and is order-independent", () => {
    const records = [
      rec({ id: 3, happenedAt: "2026-11-20T09:00:00Z" }),
      rec({ id: 1, happenedAt: "2026-11-20T08:00:00Z" }),
      rec({ id: 2, happenedAt: "2026-11-20T08:10:00Z" }),
      rec({ id: 4, happenedAt: "2026-11-20T18:00:00Z" }),
    ];
    const eps = groupEpisodes(records, 40);
    expect(eps.length).toBe(3); // 08:00+08:10, 09:00, 18:00
    expect(eps[0]!.records.map((r) => r.id)).toEqual([1, 2]);
    expect(eps[2]!.records.map((r) => r.id)).toEqual([4]);
  });

  it("keeps an episode open across a long interval's end", () => {
    const records = [
      rec({ id: 1, happenedAt: "2026-11-20T23:00:00Z", endedAt: "2026-11-21T07:00:00Z" }),
    ];
    const eps = groupEpisodes(records, 40);
    expect(eps.length).toBe(1);
    expect(eps[0]!.endMs).toBe(Date.parse("2026-11-21T07:00:00Z"));
  });

  it("returns nothing for no records", () => {
    expect(groupEpisodes([], 40)).toEqual([]);
  });
});

describe("dayVitals", () => {
  it("counts records, channels, people (excluding owner), places and episodes", () => {
    const records = [
      rec({ id: 1, happenedAt: "2026-11-20T08:00:00Z", channel: "chat.a", personId: 2 }),
      rec({ id: 2, happenedAt: "2026-11-20T09:00:00Z", channel: "chat.b", personId: 13 }),
      rec({ id: 3, happenedAt: "2026-11-20T10:00:00Z", channel: "chat.a", personId: 2, lat: 1, lon: 2 }),
    ];
    const eps = groupEpisodes(records, 40);
    const v = dayVitals(records, eps, 13);
    expect(v.records).toBe(3);
    expect(v.channels).toBe(2);
    expect(v.people).toBe(1); // person 13 is the owner, excluded
    expect(v.places).toBe(1);
    expect(v.episodes).toBe(eps.length);
    expect(v.firstMs).toBe(Date.parse("2026-11-20T08:00:00Z"));
  });
});

describe("todayInZone", () => {
  it("renders a calendar date for a zone", () => {
    const at = new Date("2026-11-20T05:30:00Z");
    // 05:30 UTC is still Nov 20 in New York (00:30) and Nov 20 in UTC.
    expect(todayInZone("America/New_York", at)).toBe("2026-11-20");
    expect(todayInZone("UTC", at)).toBe("2026-11-20");
    // 02:00 UTC is the previous evening in New York.
    const at2 = new Date("2026-11-20T02:00:00Z");
    expect(todayInZone("America/New_York", at2)).toBe("2026-11-19");
  });
});
