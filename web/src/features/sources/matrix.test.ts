import { describe, it, expect } from "vitest";
import { classifyChannel, intensity } from "./matrix";

// Fixed window of 7 days, Mon 2026-11-16 .. Sun 2026-11-22.
const DAYS = [
  "2026-11-16",
  "2026-11-17",
  "2026-11-18",
  "2026-11-19",
  "2026-11-20",
  "2026-11-21",
  "2026-11-22",
];
const weekdayOf = (d: string): number => {
  const p = d.split("-");
  return new Date(Date.UTC(Number(p[0]), Number(p[1]) - 1, Number(p[2]))).getUTCDay();
};

describe("classifyChannel", () => {
  it("marks days with records active and scales max", () => {
    const counts = new Map([
      ["2026-11-16", 2],
      ["2026-11-22", 9],
    ]);
    const f = classifyChannel(DAYS, counts, weekdayOf);
    expect(f.maxCount).toBe(9);
    expect(f.days[0]?.kind).toBe("active");
    expect(f.days[6]?.kind).toBe("active");
  });

  it("empty days outside the active span are 'outside', not holes", () => {
    const counts = new Map([["2026-11-18", 5]]); // single active day
    const f = classifyChannel(DAYS, counts, weekdayOf);
    // Before and after the only active day: outside.
    expect(f.days[0]?.kind).toBe("outside");
    expect(f.days[6]?.kind).toBe("outside");
    expect(f.days[2]?.kind).toBe("active");
  });

  it("a dense channel's empty interior day is a hole", () => {
    // active every day except the 19th -> density high -> the gap is a hole
    const counts = new Map(DAYS.filter((d) => d !== "2026-11-19").map((d) => [d, 3] as const));
    const f = classifyChannel(DAYS, counts, weekdayOf);
    expect(f.days[3]?.kind).toBe("hole");
  });

  it("a sparse channel's empty interior day on an inactive weekday is quiet", () => {
    // active only on two Mondays-ish far apart, low density, empty midweek day
    const counts = new Map([
      ["2026-11-16", 1],
      ["2026-11-22", 1],
    ]);
    const f = classifyChannel(DAYS, counts, weekdayOf);
    // 2026-11-19 (Thu) is empty, inside span, not an active weekday, low density => quiet
    expect(f.days[3]?.kind).toBe("quiet");
  });

  it("source-reported gaps outrank inference and read as reported", () => {
    const counts = new Map(DAYS.filter((d) => d !== "2026-11-19").map((d) => [d, 3] as const));
    const f = classifyChannel(DAYS, counts, weekdayOf, [{ from: "2026-11-19", to: "2026-11-19" }]);
    expect(f.days[3]?.kind).toBe("reported");
  });
});

describe("intensity", () => {
  it("is 0 for empty and monotonic in count", () => {
    expect(intensity(0, 10)).toBe(0);
    expect(intensity(1, 10)).toBeGreaterThan(0);
    expect(intensity(10, 10)).toBeCloseTo(1, 5);
    expect(intensity(5, 10)).toBeGreaterThan(intensity(2, 10));
  });
});
