import { describe, it, expect } from "vitest";
import {
  assignPlugins,
  emptyAssignments,
  HUE_ORDER,
  pluginHue,
  reassignPlugin,
  oklchToSrgb,
} from "./sourceColor";

describe("assignPlugins", () => {
  it("assigns hues in the fixed order on first sight", () => {
    const a = assignPlugins(emptyAssignments, ["alpha", "beta", "gamma"]);
    expect(pluginHue(a, "alpha")).toBe(HUE_ORDER[0]);
    expect(pluginHue(a, "beta")).toBe(HUE_ORDER[1]);
    expect(pluginHue(a, "gamma")).toBe(HUE_ORDER[2]);
  });

  it("is stable: a new plugin never recolours the others", () => {
    const a = assignPlugins(emptyAssignments, ["alpha", "beta"]);
    const b = assignPlugins(a, ["alpha", "beta", "delta"]);
    expect(pluginHue(b, "alpha")).toBe(pluginHue(a, "alpha"));
    expect(pluginHue(b, "beta")).toBe(pluginHue(a, "beta"));
    expect(pluginHue(b, "delta")).toBe(HUE_ORDER[2]);
  });

  it("is idempotent for already-known plugins", () => {
    const a = assignPlugins(emptyAssignments, ["alpha"]);
    const b = assignPlugins(a, ["alpha"]);
    expect(b).toEqual(a);
  });
});

describe("reassignPlugin", () => {
  it("swaps with the holder so the palette stays a permutation", () => {
    const a = assignPlugins(emptyAssignments, ["alpha", "beta"]);
    const moved = reassignPlugin(a, "alpha", 1); // beta holds index 1
    expect(moved.plugins.alpha).toBe(1);
    expect(moved.plugins.beta).toBe(0);
  });
});

describe("oklchToSrgb", () => {
  it("maps to valid 0..255 bytes", () => {
    const [r, g, b] = oklchToSrgb(0.74, 0.13, 25);
    for (const c of [r, g, b]) {
      expect(c).toBeGreaterThanOrEqual(0);
      expect(c).toBeLessThanOrEqual(255);
      expect(Number.isInteger(c)).toBe(true);
    }
  });

  it("near-white lightness yields high channels", () => {
    const [r, g, b] = oklchToSrgb(0.98, 0.0, 0);
    expect(Math.min(r, g, b)).toBeGreaterThan(230);
  });
});
