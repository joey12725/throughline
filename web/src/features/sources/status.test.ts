import { describe, it, expect } from "vitest";
import type { PluginRow } from "@/api/types";
import {
  deriveSourceState,
  summarySentence,
  tallyStates,
  formatCadence,
  cadenceBand,
  SEVERITY,
} from "./status";

const NOW = Date.parse("2026-11-20T12:00:00Z");

function plugin(over: Partial<PluginRow>): PluginRow {
  return {
    name: "src",
    version: "1.0.0",
    mode: "poll",
    interval_s: 3600,
    last_success_at: new Date(NOW - 1800_000).toISOString(), // 30 min ago
    last_error: null,
    messages_ingested: 10,
    stale: false,
    updated_at: new Date(NOW).toISOString(),
    ...over,
  };
}

describe("deriveSourceState", () => {
  it("is ok when fresh within its cadence", () => {
    expect(deriveSourceState(plugin({}), NOW).state).toBe("ok");
  });

  it("is late when overdue past 1.5x its interval but not host-stale", () => {
    const p = plugin({ interval_s: 3600, last_success_at: new Date(NOW - 2 * 3600_000).toISOString() });
    expect(deriveSourceState(p, NOW).state).toBe("late");
  });

  it("is stale when the host flags it", () => {
    expect(deriveSourceState(plugin({ stale: true }), NOW).state).toBe("stale");
  });

  it("is failing on a last_error, regardless of freshness", () => {
    expect(deriveSourceState(plugin({ last_error: "boom", stale: true }), NOW).state).toBe("failing");
  });

  it("is failing on a consecutive-failure streak even without an error string", () => {
    const h = deriveSourceState(plugin({ last_error: null, consecutive_failures: 3 }), NOW);
    expect(h.state).toBe("failing");
    expect(h.failures).toBe(3);
  });

  it("is paused when paused_at is set, outranking an error", () => {
    expect(deriveSourceState(plugin({ paused_at: "2026-01-01T00:00:00Z", last_error: "x" }), NOW).state).toBe("paused");
  });

  it("is never_ran when scheduled but never succeeded", () => {
    expect(deriveSourceState(plugin({ last_success_at: null, interval_s: 3600, mode: "poll" }), NOW).state).toBe(
      "never_ran",
    );
  });

  it("is one_shot when it has no cadence and never succeeded", () => {
    expect(deriveSourceState(plugin({ last_success_at: null, interval_s: null }), NOW).state).toBe("one_shot");
  });

  it("is one_shot for a manual/import mode that never ran", () => {
    expect(deriveSourceState(plugin({ last_success_at: null, interval_s: 0, mode: "manual" }), NOW).state).toBe(
      "one_shot",
    );
  });

  it("reports age in seconds since last success", () => {
    const h = deriveSourceState(plugin({ last_success_at: new Date(NOW - 7200_000).toISOString() }), NOW);
    expect(h.ageS).toBe(7200);
  });

  it("is deterministic in now", () => {
    const p = plugin({ last_success_at: new Date(NOW - 2 * 3600_000).toISOString() });
    expect(deriveSourceState(p, NOW)).toEqual(deriveSourceState(p, NOW));
  });
});

describe("severity ordering", () => {
  it("ranks failing worst and ok best", () => {
    expect(SEVERITY.failing).toBeLessThan(SEVERITY.late);
    expect(SEVERITY.late).toBeLessThan(SEVERITY.ok);
  });
});

describe("summarySentence", () => {
  it("folds ok and one-shot into healthy and names problems", () => {
    const plugins = [
      plugin({ name: "a" }),
      plugin({ name: "b" }),
      plugin({ name: "c", last_error: "x" }),
      plugin({ name: "d", interval_s: 3600, last_success_at: new Date(NOW - 3 * 3600_000).toISOString() }),
    ];
    expect(summarySentence(plugins, NOW)).toBe("2 sources healthy, 1 failing, 1 late");
  });

  it("handles the empty case", () => {
    expect(summarySentence([], NOW)).toBe("No sources");
  });

  it("tallies every state", () => {
    const t = tallyStates([plugin({}), plugin({ last_error: "x" })], NOW);
    expect(t.ok).toBe(1);
    expect(t.failing).toBe(1);
  });
});

describe("formatCadence", () => {
  it("formats round cadences", () => {
    expect(formatCadence(900)).toBe("every 15 min");
    expect(formatCadence(3600)).toBe("hourly");
    expect(formatCadence(86400)).toBe("every day");
    expect(formatCadence(21600)).toBe("every 6 h");
  });
  it("handles on-demand and one-shot", () => {
    expect(formatCadence(null)).toBe("on demand");
    expect(formatCadence(0)).toBe("one-shot");
  });
});

describe("cadenceBand", () => {
  it("returns a p10-p90 band over nonzero counts", () => {
    const band = cadenceBand([0, 10, 20, 30, 40, 50]);
    expect(band).not.toBeNull();
    expect(band!.lo).toBeLessThanOrEqual(band!.typical);
    expect(band!.typical).toBeLessThanOrEqual(band!.hi);
  });
  it("is null when there is no activity", () => {
    expect(cadenceBand([0, 0, 0])).toBeNull();
  });
});
