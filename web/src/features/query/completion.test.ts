import { describe, it, expect } from "vitest";
import { proposeCompletions } from "./completion";
import type { Catalogue } from "./catalogue";

const cat: Catalogue = {
  views: [
    { view: "api.ch_chat_sms", channel: "chat.sms", plugin: "chat", rows: 9001, record_kind: "message", fields: [{ name: "direction", type: "text", description: "incoming or outgoing" }] },
    { view: "api.ch_health_sleep", channel: "health.sleep", plugin: "health", rows: 930, record_kind: "measurement", fields: [{ name: "score", type: "int4", description: "sleep score" }] },
  ],
  functions: [
    { name: "search", signature: "search(query text) RETURNS SETOF api.search_results", description: "keyword search", example: "SELECT * FROM api.search('x')", setReturning: true },
    { name: "day", signature: "day(ts timestamptz) RETURNS date", description: "local day", setReturning: false },
  ],
  builtins: ["count", "date_trunc", "width_bucket"],
  columns: { "api.ch_chat_sms": [{ name: "id", type: "int8" }, { name: "happened_at", type: "timestamptz" }, { name: "direction", type: "text" }] },
};

describe("completion builder", () => {
  it("offers parameters after a colon", () => {
    const out = proposeCompletions(cat, { word: ":fr", before: "SELECT * FROM api.ch_chat_sms WHERE happened_at > :fr" });
    expect(out.every((s) => s.kind === "param")).toBe(true);
    expect(out.map((s) => s.label)).toContain(":from");
  });

  it("offers views and functions after 'api.'", () => {
    const out = proposeCompletions(cat, { word: "api.", before: "SELECT * FROM api." });
    expect(out.find((s) => s.label === "ch_chat_sms")?.kind).toBe("view");
    expect(out.find((s) => s.label === "search")?.kind).toBe("function");
    // Functions apply with an open paren.
    expect(out.find((s) => s.label === "search")?.apply).toBe("search(");
  });

  it("offers the typed columns of a referenced view", () => {
    const out = proposeCompletions(cat, { word: "", before: "SELECT  FROM api.ch_chat_sms" });
    const cols = out.filter((s) => s.kind === "column").map((s) => s.label);
    expect(cols).toContain("happened_at");
    expect(cols).toContain("direction");
  });

  it("completes a view's columns after 'view.'", () => {
    const out = proposeCompletions(cat, { word: "ch_chat_sms.dir", before: "SELECT ch_chat_sms.dir" });
    expect(out.find((s) => s.label === "direction")?.kind).toBe("column");
  });

  it("includes functions with their signature as info", () => {
    const out = proposeCompletions(cat, { word: "sea", before: "SELECT * FROM sea" });
    const fn = out.find((s) => s.label === "api.search");
    expect(fn?.info).toContain("RETURNS SETOF");
  });
});
