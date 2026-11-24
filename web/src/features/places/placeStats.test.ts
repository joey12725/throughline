import { describe, expect, it } from "vitest";
import { gridMax, humanDuration, weekdayHour, weekdayHourGrid } from "./placeStats";

describe("weekdayHour", () => {
  it("reads ISO weekday and hour in UTC", () => {
    // 2026-01-01 is a Thursday.
    const { weekday, hour } = weekdayHour(Date.UTC(2026, 0, 1, 15), "UTC");
    expect(weekday).toBe(4);
    expect(hour).toBe(15);
  });
  it("shifts with the zone", () => {
    const t = Date.UTC(2026, 0, 1, 1); // 01:00 UTC
    expect(weekdayHour(t, "UTC").hour).toBe(1);
    const ny = weekdayHour(t, "America/New_York"); // previous day, 20:00
    expect(ny.hour).toBe(20);
    expect(ny.weekday).toBe(3);
  });
});

describe("weekdayHourGrid", () => {
  it("accumulates into a 7×24 grid", () => {
    const t = Date.UTC(2026, 0, 1, 15); // Thu 15:00
    const grid = weekdayHourGrid([t, t, t], "UTC");
    expect(grid).toHaveLength(7);
    expect(grid[0]).toHaveLength(24);
    expect(grid[3]![15]).toBe(3);
    expect(gridMax(grid)).toBe(3);
  });
  it("ignores non-finite times", () => {
    expect(gridMax(weekdayHourGrid([NaN, Infinity], "UTC"))).toBe(1);
  });
});

describe("humanDuration", () => {
  it("formats across scales", () => {
    expect(humanDuration(30)).toBe("30s");
    expect(humanDuration(90)).toBe("1m");
    expect(humanDuration(3700)).toBe("1h 1m");
    expect(humanDuration(90000)).toBe("1d 1h");
    expect(humanDuration(-5)).toBe("—");
  });
});
