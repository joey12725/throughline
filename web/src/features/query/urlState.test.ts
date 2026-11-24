import { describe, it, expect } from "vitest";
import { encodeStatement, decodeStatement, readQueryUrl, shareQueryString } from "./urlState";

const LONG =
  "SELECT channel, count(*) AS n, min(happened_at) AS first, max(happened_at) AS last " +
  "FROM api.messages WHERE happened_at >= :from AND happened_at < :to GROUP BY 1 ORDER BY n DESC LIMIT 100";

describe("urlState codec", () => {
  it("round-trips a short statement", async () => {
    const stmt = "SELECT 1";
    const enc = await encodeStatement(stmt);
    expect(await decodeStatement(enc)).toBe(stmt);
  });

  it("round-trips a long statement", async () => {
    const enc = await encodeStatement(LONG);
    expect(await decodeStatement(enc)).toBe(LONG);
  });

  it("compresses a very repetitive long statement below its original length", async () => {
    const repetitive = `SELECT ${"a_very_long_column_name, ".repeat(60)} 1`;
    const enc = await encodeStatement(repetitive);
    expect(enc.length).toBeLessThan(repetitive.length);
    expect(await decodeStatement(enc)).toBe(repetitive);
  });

  it("round-trips unicode", async () => {
    const stmt = "SELECT '日本語 — café ☕' AS greeting";
    expect(await decodeStatement(await encodeStatement(stmt))).toBe(stmt);
  });

  it("treats an unmarked value as raw SQL (ChartPanel deep link)", async () => {
    // ChartPanel sets q to the raw, URL-decoded statement.
    expect(await decodeStatement("SELECT * FROM api.ch_chat_sms")).toBe("SELECT * FROM api.ch_chat_sms");
  });

  it("reads ChartPanel's ?q=<rawSql>&spec=<json> deep link and runs it when asked", async () => {
    const spec = { v: 1, title: "t", why: "w", lens: "table", source: { sql: "x", row_cap: 1 }, encode: {}, provenance: { by: "engine", sampled: false } };
    const search = `?q=${encodeURIComponent("SELECT * FROM api.messages")}&spec=${encodeURIComponent(JSON.stringify(spec))}`;
    const state = await readQueryUrl(search, "");
    expect(state.statement).toBe("SELECT * FROM api.messages");
    expect(state.spec?.lens).toBe("table");
    expect(state.run).toBeUndefined();
  });

  it("reads our own ?q=<encoded>&run=1 share link", async () => {
    const qs = await shareQueryString("SELECT 42", true);
    const state = await readQueryUrl(`?${qs}`, "");
    expect(state.statement).toBe("SELECT 42");
    expect(state.run).toBe(true);
  });

  it("falls back to the hash when the search has no q", async () => {
    const enc = await encodeStatement("SELECT 7");
    const state = await readQueryUrl("", `#q=${enc}&run=1`);
    expect(state.statement).toBe("SELECT 7");
    expect(state.run).toBe(true);
  });

  it("ignores a malformed spec", async () => {
    const state = await readQueryUrl(`?q=${encodeURIComponent("SELECT 1")}&spec=not-json`, "");
    expect(state.spec).toBeUndefined();
  });
});
