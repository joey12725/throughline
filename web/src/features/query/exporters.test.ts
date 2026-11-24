import { describe, it, expect } from "vitest";
import { toCsv, toTsv, toJson, toCliCommand } from "./exporters";
import type { Row, SqlColumn } from "@/api/types";

const columns: SqlColumn[] = [
  { name: "channel", type: "text" },
  { name: "n", type: "int8" },
  { name: "meta", type: "jsonb" },
];
const rows: Row[] = [
  { channel: "chat.sms", n: 9001, meta: { a: 1 } },
  { channel: 'has "quote", comma', n: null, meta: null },
];

describe("exporters", () => {
  it("writes CSV with a header and escapes quotes/commas", () => {
    const csv = toCsv(rows, columns);
    const lines = csv.split("\n");
    expect(lines[0]).toBe("channel,n,meta");
    expect(lines[1]).toBe('chat.sms,9001,"{""a"":1}"');
    expect(lines[2]).toBe('"has ""quote"", comma",,');
  });

  it("writes TSV", () => {
    const tsv = toTsv(rows, columns);
    expect(tsv.split("\n")[0]).toBe("channel\tn\tmeta");
  });

  it("writes JSON of the rows", () => {
    expect(JSON.parse(toJson(rows))).toHaveLength(2);
  });

  it("builds a copy-as-CLI command and escapes single quotes", () => {
    expect(toCliCommand("SELECT 1")).toBe("throughline sql 'SELECT 1'");
    expect(toCliCommand("SELECT 'x'")).toBe("throughline sql 'SELECT '\\''x'\\'''");
  });
});
