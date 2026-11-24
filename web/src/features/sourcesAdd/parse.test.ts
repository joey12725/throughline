import { describe, it, expect } from "vitest";
import { detectKind, parseFile, parseText, inferColumns } from "./parse";

const CSV = `id,when,note,rating,active
1,2024-01-01T08:00:00Z,hello there friend,5,true
2,2024-01-02T09:30:00Z,a longer message with words,3,false
3,2024-01-03T10:00:00Z,short,4,true`;

describe("detectKind", () => {
  it("uses the extension first", () => {
    expect(detectKind("a.tsv", "x")).toBe("tsv");
    expect(detectKind("a.ndjson", "{}")).toBe("ndjson");
    expect(detectKind("a.json", "[]")).toBe("json");
    expect(detectKind("a.csv", "a,b")).toBe("csv");
  });
  it("sniffs a JSON array and NDJSON", () => {
    expect(detectKind("x.txt", "[{}]")).toBe("json");
    expect(detectKind("x.txt", '{"a":1}\n{"a":2}')).toBe("ndjson");
  });
  it("sniffs a tab-delimited body", () => {
    expect(detectKind("x.txt", "a\tb\n1\t2")).toBe("tsv");
  });
});

describe("parse + infer (CSV)", () => {
  const { table, columns } = parseFile("data.csv", CSV);
  it("reads the header and rows", () => {
    expect(table.columns).toEqual(["id", "when", "note", "rating", "active"]);
    expect(table.rows).toHaveLength(3);
  });
  it("infers types", () => {
    const by = Object.fromEntries(columns.map((c) => [c.name, c.type]));
    expect(by.id).toBe("integer");
    expect(by.when).toBe("timestamp");
    expect(by.note).toBe("text");
    expect(by.rating).toBe("integer");
    expect(by.active).toBe("boolean");
  });
  it("computes uniqueness and avg length", () => {
    const id = columns.find((c) => c.name === "id")!;
    expect(id.uniqueness).toBe(1);
    const note = columns.find((c) => c.name === "note")!;
    expect(note.avgLen).toBeGreaterThan(5);
  });
});

describe("parse (JSON array and NDJSON)", () => {
  it("parses a JSON array of objects", () => {
    const t = parseText("x.json", '[{"a":1,"b":"x"},{"a":2,"b":"y"}]');
    expect(t.columns).toEqual(["a", "b"]);
    expect(t.rows).toHaveLength(2);
  });
  it("parses NDJSON and unions keys across rows", () => {
    const t = parseText("x.ndjson", '{"a":1}\n{"a":2,"c":9}\n');
    expect(t.columns).toEqual(["a", "c"]);
    expect(t.rows).toHaveLength(2);
  });
  it("infers json columns from nested objects", () => {
    const t = parseText("x.ndjson", '{"meta":{"k":1}}\n{"meta":{"k":2}}');
    const cols = inferColumns(t);
    expect(cols[0]!.type).toBe("json");
  });
});

describe("epoch timestamps", () => {
  it("treats 10- and 13-digit numbers as timestamps", () => {
    const t = parseText("x.csv", "ts\n1704067200\n1704153600");
    const cols = inferColumns(t);
    expect(cols[0]!.type).toBe("timestamp");
  });
});
