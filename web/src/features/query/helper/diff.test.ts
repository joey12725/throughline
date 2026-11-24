import { describe, it, expect } from "vitest";
import { diffLines, statementsDiffer, diffStat } from "./diff";

describe("line diff", () => {
  it("marks identical text as all-same", () => {
    const d = diffLines("a\nb\nc", "a\nb\nc");
    expect(d.every((l) => l.op === "same")).toBe(true);
    expect(diffStat(d)).toEqual({ added: 0, removed: 0 });
  });

  it("detects an added line", () => {
    const d = diffLines("SELECT 1\nFROM t", "SELECT 1\nWHERE x\nFROM t");
    expect(d.find((l) => l.op === "add")?.text).toBe("WHERE x");
    expect(diffStat(d).added).toBe(1);
  });

  it("detects a changed line as a del + add", () => {
    const d = diffLines("LIMIT 10", "LIMIT 100");
    expect(d.some((l) => l.op === "del" && l.text === "LIMIT 10")).toBe(true);
    expect(d.some((l) => l.op === "add" && l.text === "LIMIT 100")).toBe(true);
  });

  it("statementsDiffer ignores trailing whitespace", () => {
    expect(statementsDiffer("SELECT 1", "SELECT 1\n")).toBe(false);
    expect(statementsDiffer("SELECT 1", "SELECT 2")).toBe(true);
  });

  it("reconstructs the after text from same+add lines", () => {
    const before = "a\nb";
    const after = "a\nc\nd";
    const d = diffLines(before, after);
    const rebuilt = d.filter((l) => l.op !== "del").map((l) => l.text).join("\n");
    expect(rebuilt).toBe(after);
  });
});
