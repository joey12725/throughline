import { describe, it, expect } from "vitest";
import { buildConstellation, recencyNorm, recencyRings, hash01, familyTotals, type PersonInput } from "./layout";

const now = Date.parse("2026-11-21T00:00:00Z");

function person(over: Partial<PersonInput> & { id: number }): PersonInput {
  return {
    name: `P${over.id}`,
    records: 100,
    channels: 1,
    firstAt: "2020-01-01T00:00:00Z",
    lastAt: "2026-11-01T00:00:00Z",
    family: "fam.a",
    topChannel: "fam.a.one",
    ...over,
  };
}

describe("recencyNorm", () => {
  it("is 0 for the freshest and climbs monotonically with age", () => {
    expect(recencyNorm(0, DAY(30))).toBe(0);
    expect(recencyNorm(DAY(1), DAY(30))).toBeGreaterThan(0);
    expect(recencyNorm(DAY(10), DAY(30))).toBeLessThan(recencyNorm(DAY(20), DAY(30)));
    expect(recencyNorm(DAY(30), DAY(30))).toBeCloseTo(1, 5);
  });
  it("never leaves [0,1]", () => {
    expect(recencyNorm(-5, DAY(30))).toBe(0);
    expect(recencyNorm(DAY(1000), DAY(30))).toBeLessThanOrEqual(1);
  });
});

describe("hash01", () => {
  it("is deterministic and bounded", () => {
    expect(hash01(17)).toBe(hash01(17));
    for (const id of [1, 2, 99, 100000]) {
      const h = hash01(id);
      expect(h).toBeGreaterThanOrEqual(0);
      expect(h).toBeLessThanOrEqual(1);
    }
  });
});

describe("buildConstellation", () => {
  it("returns nothing for no people", () => {
    expect(buildConstellation([], { now })).toEqual([]);
  });

  it("places more recent contacts closer to the centre", () => {
    const recent = person({ id: 1, lastAt: "2026-11-20T00:00:00Z" });
    const stale = person({ id: 2, lastAt: "2021-01-01T00:00:00Z" });
    const stars = buildConstellation([recent, stale], { now });
    const byId = new Map(stars.map((s) => [s.id, s]));
    expect(byId.get(1)!.radius).toBeLessThan(byId.get(2)!.radius);
  });

  it("sizes stars by sqrt of volume", () => {
    const small = person({ id: 1, records: 25 });
    const big = person({ id: 2, records: 2500 });
    const stars = buildConstellation([small, big], { now, sizeMin: 2, sizeMax: 20 });
    const byId = new Map(stars.map((s) => [s.id, s]));
    expect(byId.get(2)!.size).toBeGreaterThan(byId.get(1)!.size);
  });

  it("keeps every star inside the unit disc", () => {
    const people = Array.from({ length: 40 }, (_, i) =>
      person({ id: i + 1, records: (i + 1) * 13, family: `fam.${i % 4}` }),
    );
    for (const s of buildConstellation(people, { now })) {
      expect(Math.hypot(s.x, s.y)).toBeLessThanOrEqual(1.0001);
      expect(s.radius).toBeGreaterThanOrEqual(0);
      expect(s.radius).toBeLessThanOrEqual(1);
      expect(s.brightness).toBeGreaterThanOrEqual(0);
      expect(s.brightness).toBeLessThanOrEqual(1);
    }
  });

  it("is deterministic", () => {
    const people = [person({ id: 1 }), person({ id: 2, family: "fam.b" }), person({ id: 3 })];
    expect(buildConstellation(people, { now })).toEqual(buildConstellation(people, { now }));
  });

  it("gives each family a contiguous angular sector", () => {
    const people = [
      person({ id: 1, family: "fam.a" }),
      person({ id: 2, family: "fam.a" }),
      person({ id: 3, family: "fam.b" }),
      person({ id: 4, family: "fam.b" }),
      person({ id: 5, family: "fam.c" }),
    ];
    const stars = buildConstellation(people, { now });
    // Families are ordered by name a,b,c; their angle ranges must not interleave.
    const ranges = new Map<string, [number, number]>();
    for (const s of stars) {
      const r = ranges.get(s.family);
      if (!r) ranges.set(s.family, [s.angle, s.angle]);
      else ranges.set(s.family, [Math.min(r[0], s.angle), Math.max(r[1], s.angle)]);
    }
    const a = ranges.get("fam.a")!;
    const b = ranges.get("fam.b")!;
    const c = ranges.get("fam.c")!;
    expect(a[1]).toBeLessThanOrEqual(b[0] + 1e-9);
    expect(b[1]).toBeLessThanOrEqual(c[0] + 1e-9);
  });
});

describe("rank-based radius", () => {
  it("spreads a cluster of near-equal recency evenly across the disc", () => {
    // 20 people all contacted within a few days of each other: a log/linear age
    // scale would pile them into one ring; the rank transform must not.
    const people = Array.from({ length: 20 }, (_, i) =>
      person({ id: i + 1, lastAt: new Date(now - (i + 1) * 3_600_000).toISOString() }),
    );
    const radii = buildConstellation(people, { now })
      .map((s) => s.radius)
      .sort((a, b) => a - b);
    const spread = radii[radii.length - 1]! - radii[0]!;
    expect(spread).toBeGreaterThan(0.6); // fills most of the radial axis
    // Neighbouring radii are roughly evenly spaced (no big empty gap, no pile-up).
    const gaps = radii.slice(1).map((r, i) => r - radii[i]!);
    expect(Math.max(...gaps)).toBeLessThan(0.15);
  });

  it("keeps recent nearer than stale after the transform", () => {
    const recent = person({ id: 1, lastAt: "2026-11-20T00:00:00Z" });
    const mid = person({ id: 2, lastAt: "2026-06-01T00:00:00Z" });
    const stale = person({ id: 3, lastAt: "2021-01-01T00:00:00Z" });
    const byId = new Map(buildConstellation([recent, mid, stale], { now }).map((s) => [s.id, s]));
    expect(byId.get(1)!.radius).toBeLessThan(byId.get(2)!.radius);
    expect(byId.get(2)!.radius).toBeLessThan(byId.get(3)!.radius);
  });
});

describe("recencyRings", () => {
  it("reports horizons that fall within the contact spread, inside the disc", () => {
    const people = [
      person({ id: 1, lastAt: new Date(now - 2 * 3_600_000).toISOString() }), // hours ago
      person({ id: 2, lastAt: new Date(now - 3 * 86_400_000).toISOString() }), // 3 days
      person({ id: 3, lastAt: new Date(now - 20 * 86_400_000).toISOString() }), // 20 days
      person({ id: 4, lastAt: new Date(now - 200 * 86_400_000).toISOString() }), // ~7 months
    ];
    const rings = recencyRings(people, { now });
    expect(rings.length).toBeGreaterThan(0);
    for (const r of rings) {
      expect(r.radius).toBeGreaterThan(0);
      expect(r.radius).toBeLessThanOrEqual(1);
      expect(typeof r.label).toBe("string");
    }
    // Rings are ordered outward, and nearer horizons sit nearer the centre.
    for (let i = 1; i < rings.length; i++) {
      expect(rings[i]!.ageMs).toBeGreaterThan(rings[i - 1]!.ageMs);
      expect(rings[i]!.radius).toBeGreaterThanOrEqual(rings[i - 1]!.radius);
    }
  });

  it("returns nothing for fewer than two people", () => {
    expect(recencyRings([], { now })).toEqual([]);
    expect(recencyRings([person({ id: 1 })], { now })).toEqual([]);
  });
});

describe("familyTotals", () => {
  it("aggregates and ranks by volume", () => {
    const totals = familyTotals([
      person({ id: 1, family: "x", records: 10 }),
      person({ id: 2, family: "x", records: 40 }),
      person({ id: 3, family: "y", records: 30 }),
    ]);
    expect(totals[0]).toEqual({ family: "x", records: 50, count: 2 });
    expect(totals[1]).toEqual({ family: "y", records: 30, count: 1 });
  });
});

function DAY(n: number): number {
  return n * 86_400_000;
}
