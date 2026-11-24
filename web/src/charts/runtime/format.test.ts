import { describe, expect, it } from "vitest";
import { clip, compactNumber, formatMeasure, hourLabel, humanDuration, humanPercent, timeTickLabel, weekdayLabel } from "./format";

describe("compactNumber", () => {
  it("abbreviates magnitudes", () => {
    expect(compactNumber(0)).toBe("0");
    expect(compactNumber(12)).toBe("12");
    expect(compactNumber(1200)).toBe("1.2k");
    expect(compactNumber(3_400_000)).toBe("3.4M");
    expect(compactNumber(2_000_000_000)).toBe("2B");
  });
  it("keeps small fractions readable", () => {
    expect(compactNumber(0.08)).toBe("0.08");
    expect(compactNumber(-1500)).toBe("-1.5k");
  });
});

describe("humanDuration", () => {
  it("scales seconds to days", () => {
    expect(humanDuration(45)).toBe("45s");
    expect(humanDuration(90)).toBe("1m 30s");
    expect(humanDuration(3600)).toBe("1h");
    expect(humanDuration(3660)).toBe("1h 1m");
    expect(humanDuration(90000)).toBe("1d 1h");
  });
});

describe("humanPercent", () => {
  it("treats 0..1 as a fraction and >1 as already-percent", () => {
    expect(humanPercent(0.5)).toBe("50%");
    expect(humanPercent(42, "pct")).toBe("42%");
    expect(humanPercent(0.034)).toBe("3.4%");
  });
});

describe("formatMeasure", () => {
  it("honours known units", () => {
    expect(formatMeasure(3600, "s")).toBe("1h");
    expect(formatMeasure(85, "pct")).toBe("85%");
    expect(formatMeasure(72, "kg")).toBe("72 kg");
    expect(formatMeasure(5)).toBe("5");
  });
});

describe("time labels", () => {
  it("weekday and hour", () => {
    expect(weekdayLabel(1)).toBe("Mon");
    expect(weekdayLabel(7)).toBe("Sun");
    expect(hourLabel(0)).toBe("12a");
    expect(hourLabel(13)).toBe("1p");
  });
  it("picks a tick label by grain", () => {
    const ms = Date.parse("2026-03-14T09:30:00Z");
    expect(timeTickLabel(ms, "day")).toBe("Mar 14");
    expect(timeTickLabel(ms, "year")).toBe("2026");
    expect(timeTickLabel(ms, "hour")).toBe("09:30");
  });
});

describe("clip", () => {
  it("clips on a word boundary and ellipsises", () => {
    expect(clip("short", 10)).toBe("short");
    expect(clip("the quick brown fox jumps", 12).endsWith("…")).toBe(true);
    expect(clip("the quick brown fox jumps", 12).length).toBeLessThanOrEqual(13);
  });
});
