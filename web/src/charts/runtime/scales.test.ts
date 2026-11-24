import { describe, expect, it } from "vitest";
import { band, clamp, extent, linear, niceTicks, rampIndex, timeTicks } from "./scales";

describe("extent", () => {
  it("finds min and max, ignoring non-finite", () => {
    expect(extent([3, 1, 2, NaN, Infinity])).toEqual([1, 3]);
  });
  it("pads a single value", () => {
    const [lo, hi] = extent([5, 5]);
    expect(lo).toBeLessThan(hi);
  });
  it("falls back to 0..1 when empty", () => {
    expect(extent([])).toEqual([0, 1]);
  });
});

describe("niceTicks", () => {
  it("returns rounded ticks within range", () => {
    const ticks = niceTicks(0, 100, 5);
    expect(ticks[0]).toBe(0);
    expect(ticks[ticks.length - 1]).toBeLessThanOrEqual(100);
    expect(ticks.every((t, i) => i === 0 || t > ticks[i - 1]!)).toBe(true);
  });
  it("handles a degenerate range", () => {
    expect(niceTicks(5, 5)).toEqual([5]);
  });
});

describe("linear", () => {
  it("maps domain to range and inverts", () => {
    const s = linear([0, 10], [0, 100]);
    expect(s(5)).toBe(50);
    expect(s.invert(50)).toBeCloseTo(5);
  });
});

describe("band", () => {
  it("places keys with a bandwidth and centres them", () => {
    const b = band(["a", "b", "c"], [0, 300], 0);
    expect(b.bandwidth).toBeCloseTo(100);
    expect(b.center("a")).toBeCloseTo(50);
    expect(b("b")).toBeCloseTo(100);
  });
});

describe("timeTicks", () => {
  it("returns ascending instants under the tick budget", () => {
    const from = Date.parse("2026-01-01T00:00:00Z");
    const to = Date.parse("2026-01-08T00:00:00Z");
    const ticks = timeTicks(from, to, "day", 7);
    expect(ticks.length).toBeLessThanOrEqual(8);
    expect(ticks.every((t, i) => i === 0 || t > ticks[i - 1]!)).toBe(true);
  });
});

describe("helpers", () => {
  it("clamps", () => {
    expect(clamp(5, 0, 3)).toBe(3);
    expect(clamp(-1, 0, 3)).toBe(0);
  });
  it("ramp index", () => {
    expect(rampIndex(0, 5)).toBe(0);
    expect(rampIndex(1, 5)).toBe(4);
    expect(rampIndex(0.5, 5)).toBe(2);
  });
});
