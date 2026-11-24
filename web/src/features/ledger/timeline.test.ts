import { describe, expect, it } from "vitest";
import { aggregateSpend, daysUntil, dueBucket, hourOfDayBins } from "./timeline";

const NOW = Date.UTC(2026, 10, 21, 12); // 2026-11-21 12:00 UTC
const DAY = 86_400_000;

describe("dueBucket", () => {
  it("classifies open commitments by their due date", () => {
    expect(dueBucket(NOW - DAY, "open", NOW)).toBe("overdue");
    expect(dueBucket(NOW + 3600_000, "open", NOW)).toBe("today");
    expect(dueBucket(NOW + 3 * DAY, "open", NOW)).toBe("week");
    expect(dueBucket(NOW + 30 * DAY, "open", NOW)).toBe("later");
    expect(dueBucket(null, "open", NOW)).toBe("undated");
  });
  it("sends non-open states to the closed column regardless of date", () => {
    expect(dueBucket(NOW - DAY, "met", NOW)).toBe("closed");
    expect(dueBucket(null, "dropped", NOW)).toBe("closed");
  });
});

describe("daysUntil", () => {
  it("is negative for overdue and positive for upcoming", () => {
    expect(daysUntil(NOW - 2 * DAY, NOW)).toBe(-2);
    expect(daysUntil(NOW + 5 * DAY, NOW)).toBe(5);
  });
});

describe("hourOfDayBins", () => {
  it("bins instants into 24 hours", () => {
    const bins = hourOfDayBins([Date.UTC(2026, 0, 1, 9), Date.UTC(2026, 0, 2, 9), Date.UTC(2026, 0, 1, 18)], "UTC");
    expect(bins).toHaveLength(24);
    expect(bins[9]).toBe(2);
    expect(bins[18]).toBe(1);
  });
});

describe("aggregateSpend", () => {
  const by = [
    { provider: "local", purpose: "extract", tokens: 100, cost: 0, calls: 2 },
    { provider: "local", purpose: "librarian", tokens: 50, cost: 0.01, calls: 1 },
    { provider: "voyage", purpose: "extract", tokens: 300, cost: 0.2, calls: 3 },
  ];
  it("sums by provider", () => {
    const s = aggregateSpend(by, "provider");
    expect(s).toHaveLength(2);
    const local = s.find((x) => x.key === "local")!;
    expect(local.tokens).toBe(150);
    expect(local.calls).toBe(3);
  });
  it("sums by purpose and sorts by tokens desc", () => {
    const s = aggregateSpend(by, "purpose");
    expect(s[0]!.key).toBe("extract");
    expect(s[0]!.tokens).toBe(400);
  });
  it("labels missing dimensions as unknown", () => {
    expect(aggregateSpend([{ tokens: 1 }], "provider")[0]!.key).toBe("unknown");
  });
});
