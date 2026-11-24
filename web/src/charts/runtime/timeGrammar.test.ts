import { describe, expect, it } from "vitest";
import { isImpreciseGrain, nowPosition, timeSegments } from "./timeGrammar";

const DAY = 86_400_000;

describe("timeSegments", () => {
  it("splits past-covered and future at now", () => {
    const from = 0;
    const to = 10 * DAY;
    const now = 6 * DAY;
    const segs = timeSegments(from, to, null, now);
    expect(segs).toEqual([
      { kind: "covered", from: 0, to: 6 * DAY },
      { kind: "future", from: 6 * DAY, to: 10 * DAY },
    ]);
  });

  it("hatches periods outside the coverage window", () => {
    const now = 10 * DAY;
    const segs = timeSegments(0, 10 * DAY, { from: 3 * DAY, to: 8 * DAY }, now);
    expect(segs.map((s) => s.kind)).toEqual(["uncovered", "covered", "uncovered"]);
    expect(segs[1]).toMatchObject({ from: 3 * DAY, to: 8 * DAY });
  });

  it("returns nothing for a degenerate domain", () => {
    expect(timeSegments(5, 5, null, 0)).toEqual([]);
  });

  it("merges adjacent same-kind segments", () => {
    // now beyond the domain: everything is covered (one segment).
    const segs = timeSegments(0, 4 * DAY, null, 100 * DAY);
    expect(segs).toEqual([{ kind: "covered", from: 0, to: 4 * DAY }]);
  });
});

describe("nowPosition", () => {
  it("returns now inside the domain and null outside", () => {
    expect(nowPosition(0, 10, 5)).toBe(5);
    expect(nowPosition(0, 10, 20)).toBeNull();
  });
});

describe("isImpreciseGrain", () => {
  it("treats day and coarser as imprecise", () => {
    expect(isImpreciseGrain("day")).toBe(true);
    expect(isImpreciseGrain("month")).toBe(true);
    expect(isImpreciseGrain("hour")).toBe(false);
    expect(isImpreciseGrain("minute")).toBe(false);
    expect(isImpreciseGrain(null)).toBe(false);
  });
});
