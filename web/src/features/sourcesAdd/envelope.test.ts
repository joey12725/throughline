import { describe, it, expect } from "vitest";
import { parseFile } from "./parse";
import {
  inferMapping,
  validateMapping,
  buildMessages,
  previewRows,
  buildChannelMeta,
  columnSignature,
  rowToMessage,
  type Mapping,
} from "./envelope";

const CSV = `msg_id,sent_at,sender,body,rating
a1,2024-01-01T08:00:00Z,alice,hello there,5
a2,2024-01-02T09:30:00Z,bob,a longer note about things,3
a3,2024-01-03T10:00:00Z,alice,another message,4`;

const { table, columns } = parseFile("chat.csv", CSV);

describe("inferMapping", () => {
  const m = inferMapping(columns);
  it("maps the time, id, text and author columns from names and shape", () => {
    expect(m.slots.happened_at).toBe("sent_at");
    expect(m.slots.external_id).toBe("msg_id");
    expect(m.slots.content).toBe("body");
    expect(m.slots.author).toBe("sender");
  });
  it("keeps the rest as declared fields with sane names", () => {
    const names = m.fields.map((f) => f.name);
    expect(names).toContain("rating");
    const rating = m.fields.find((f) => f.name === "rating")!;
    expect(rating.type).toBe("integer");
  });
  it("defaults to per-row threads when no thread column is found", () => {
    expect(m.threadMode).toBe("per_row");
  });
});

describe("validateMapping", () => {
  it("passes a complete mapping", () => {
    const m = inferMapping(columns);
    m.channelName = "import.chat";
    expect(validateMapping(m).filter((p) => p.level === "error")).toHaveLength(0);
  });
  it("rejects a bad channel name and a missing required slot", () => {
    const m: Mapping = { slots: {}, fields: [], threadMode: "per_row", channelName: "Bad Name", channelDescription: "" };
    const errs = validateMapping(m).filter((p) => p.level === "error");
    expect(errs.length).toBeGreaterThanOrEqual(2);
  });
});

describe("buildMessages", () => {
  const m = inferMapping(columns);
  m.channelName = "import.chat";
  const built = buildMessages(m, table.rows);
  it("builds a valid, idempotent-keyed envelope per row", () => {
    expect(built.messages).toHaveLength(3);
    expect(built.rejected).toHaveLength(0);
    const first = built.messages[0]!;
    expect(first.channel).toBe("import.chat");
    expect(first.external_id).toBe("a1");
    expect(first.happened_at).toMatch(/\+00:00$|Z$/);
    expect(first.content).toBe("hello there");
    expect(first.author).toEqual({ system: "webimport", kind: "account", value: "alice" });
    expect(first.fields).toEqual({ rating: 5 });
    expect(first).toHaveProperty("payload");
  });

  it("rejects a row with no timestamp", () => {
    const r = rowToMessage(m, { msg_id: "x", sent_at: "", body: "hi", sender: "a", rating: "1" }, 9);
    expect(r.message).toBeNull();
    expect(r.error).toMatch(/timestamp/);
  });

  it("rejects a row with empty text", () => {
    const r = rowToMessage(m, { msg_id: "x", sent_at: "2024-01-01T00:00:00Z", body: "", sender: "a", rating: "1" }, 9);
    expect(r.message).toBeNull();
    expect(r.error).toMatch(/text/);
  });
});

describe("happened_at normalisation", () => {
  it("adds UTC midnight to a bare date", () => {
    const m = inferMapping(columns);
    const r = rowToMessage(m, { msg_id: "x", sent_at: "2024-06-01", body: "hi", sender: "a", rating: "2" }, 0);
    expect(r.message?.happened_at).toBe("2024-06-01T00:00:00.000Z");
  });
});

describe("previewRows", () => {
  it("shapes rows like the generated channel view", () => {
    const m = inferMapping(columns);
    m.channelName = "import.chat";
    const built = buildMessages(m, table.rows);
    const { rows, columns: cols } = previewRows(m, built);
    const colNames = cols.map((c) => c.name);
    expect(colNames).toEqual(expect.arrayContaining(["id", "happened_at", "thread_id", "content", "author", "rating"]));
    expect(rows[0]!.content).toBe("hello there");
    expect(typeof rows[0]!.thread_id).toBe("number");
  });
});

describe("buildChannelMeta", () => {
  it("carries the declared fields for the lens engine", () => {
    const m = inferMapping(columns);
    const meta = buildChannelMeta(m, 3);
    expect(meta.fields.map((f) => f.name)).toContain("rating");
    expect(meta.rows).toBe(3);
  });
});

describe("columnSignature", () => {
  it("is stable under column reordering and case", () => {
    expect(columnSignature({ columns: ["A", "b", "c"] })).toBe(columnSignature({ columns: ["c", "B", "a"] }));
  });
  it("differs for a different column set", () => {
    expect(columnSignature({ columns: ["a", "b"] })).not.toBe(columnSignature({ columns: ["a", "b", "c"] }));
  });
});
