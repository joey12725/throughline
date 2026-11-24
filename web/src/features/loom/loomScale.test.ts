import { describe, it, expect } from "vitest";
import { makeLoomScale, choosePivot } from "./loomScale";

const WEEK = 7 * 24 * 3600 * 1000;

describe("makeLoomScale — linear", () => {
  const s = makeLoomScale({ domainFrom: 0, domainTo: 1000, plotW: 500, mode: "linear" });

  it("maps the domain onto the full width", () => {
    expect(s.toX(0)).toBe(0);
    expect(s.toX(1000)).toBe(500);
    expect(s.toX(500)).toBe(250);
  });

  it("is invertible", () => {
    for (const ms of [0, 123, 500, 777, 1000]) {
      expect(s.toTime(s.toX(ms))).toBeCloseTo(ms, 6);
    }
  });

  it("has no context band", () => {
    expect(s.contextW).toBe(0);
    expect(s.pivotX).toBeNull();
  });
});

describe("makeLoomScale — focus+context", () => {
  const domainFrom = 0;
  const domainTo = 1000;
  const pivotMs = 800; // last 200 is the focus, first 800 compressed
  const s = makeLoomScale({ domainFrom, domainTo, plotW: 1000, mode: "focus", pivotMs, contextFraction: 0.15, breakPx: 12 });

  it("places the pivot at the left edge of the focus band", () => {
    expect(s.pivotX).toBe(s.contextW);
    expect(s.toX(pivotMs)).toBeCloseTo(s.contextW + s.breakW, 6);
  });

  it("compresses the context and expands the focus", () => {
    const contextPxPerMs = (s.toX(pivotMs) - s.breakW) / pivotMs;
    const focusPxPerMs = (s.toX(domainTo) - s.toX(pivotMs)) / (domainTo - pivotMs);
    // The focus is far more generous per unit time than the context.
    expect(focusPxPerMs).toBeGreaterThan(contextPxPerMs * 5);
  });

  it("maps endpoints to the full width", () => {
    expect(s.toX(domainFrom)).toBeCloseTo(0, 6);
    expect(s.toX(domainTo)).toBeCloseTo(1000, 6);
  });

  it("round-trips instants on both sides of the pivot", () => {
    for (const ms of [0, 100, 400, 799, 800, 801, 900, 1000]) {
      const back = s.toTime(s.toX(ms));
      expect(back).toBeCloseTo(ms, 4);
    }
  });

  it("round-trips pixels", () => {
    for (const px of [0, 50, 149, s.contextW, s.contextW + s.breakW, 600, 1000]) {
      const t = s.toTime(px);
      // A pixel inside the break [contextW, contextW+breakW] maps to the pivot;
      // otherwise it round-trips.
      if (px >= s.contextW && px <= s.contextW + s.breakW) {
        expect(t).toBeCloseTo(pivotMs, 6);
      } else {
        expect(s.toX(t)).toBeCloseTo(px, 4);
      }
    }
  });

  it("falls back to linear when the pivot is outside the domain", () => {
    const flat = makeLoomScale({ domainFrom, domainTo, plotW: 1000, mode: "focus", pivotMs: null });
    expect(flat.pivotX).toBeNull();
    expect(flat.toX(500)).toBe(500);
  });
});

describe("choosePivot", () => {
  it("finds the boundary that leaves ~95% in the focus", () => {
    // 100 weeks: the first 90 nearly empty, the last 10 carry almost everything.
    const weeks = Array.from({ length: 100 }, (_, i) => i * WEEK);
    const totals = weeks.map((_, i) => (i >= 90 ? 1000 : 1));
    const pivot = choosePivot(weeks, totals, { focusShare: 0.95, minContextWeeks: 8 });
    expect(pivot).not.toBeNull();
    const idx = weeks.indexOf(pivot as number);
    expect(idx).toBeGreaterThanOrEqual(85);
    expect(idx).toBeLessThanOrEqual(90);
  });

  it("returns null when the data is already uniform (no sparse tail)", () => {
    const weeks = Array.from({ length: 50 }, (_, i) => i * WEEK);
    const totals = weeks.map(() => 10);
    // 95% of a uniform series puts the pivot within minContextWeeks of the start.
    expect(choosePivot(weeks, totals, { minContextWeeks: 8 })).toBeNull();
  });

  it("returns null for an empty or single-week series", () => {
    expect(choosePivot([], [])).toBeNull();
    expect(choosePivot([0], [5])).toBeNull();
  });

  it("returns null when everything is in the last week (focus is the whole chart)", () => {
    const weeks = Array.from({ length: 20 }, (_, i) => i * WEEK);
    const totals = weeks.map((_, i) => (i === 19 ? 1000 : 0));
    expect(choosePivot(weeks, totals)).toBeNull();
  });
});
