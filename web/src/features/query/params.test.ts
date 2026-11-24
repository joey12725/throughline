import { describe, it, expect } from "vitest";
import { resolveStatement, statementParams, unresolvedParams, parseBarValue } from "./params";

const scope = { from: "2026-01-01T00:00:00Z", to: "2026-02-01T00:00:00Z", tz: "America/New_York" };

describe("params", () => {
  it("lists distinct params in order of first use", () => {
    expect(statementParams("SELECT :from, :to, :from, :mine")).toEqual(["from", "to", "mine"]);
  });

  it("substitutes global scope params as typed literals", () => {
    const r = resolveStatement("SELECT * FROM t WHERE happened_at >= :from AND happened_at < :to", scope);
    expect(r.sql).toContain("2026-01-01");
    expect(r.sql).not.toContain(":from");
    expect(r.unresolved).toEqual([]);
  });

  it("leaves a custom param unresolved until supplied", () => {
    expect(unresolvedParams("SELECT * FROM t WHERE x = :handle", scope)).toEqual(["handle"]);
    const r = resolveStatement("SELECT * FROM t WHERE x = :handle", scope, { handle: parseBarValue("alice") });
    expect(r.sql).toContain("'alice'");
    expect(r.unresolved).toEqual([]);
  });

  it("always resolves :grain from the scope", () => {
    const r = resolveStatement("SELECT date_trunc(:grain, happened_at, :tz)", scope);
    expect(r.unresolved).toEqual([]);
  });

  it("guesses a value's type", () => {
    expect(parseBarValue("42").type).toBe("int");
    expect(parseBarValue("3.14").type).toBe("number");
    expect(parseBarValue("true").type).toBe("bool");
    expect(parseBarValue("2026-01-01").type).toBe("date");
    expect(parseBarValue("hello").type).toBe("text");
  });
});
