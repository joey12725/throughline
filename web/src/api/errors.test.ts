import { describe, it, expect } from "vitest";
import { apiErrorFromBody } from "./errors";

describe("apiErrorFromBody", () => {
  it("handles a plain string detail (400 ValueError)", () => {
    const e = apiErrorFromBody(400, { detail: "relations must be explicitly in schema api" });
    expect(e.status).toBe(400);
    expect(e.message).toBe("relations must be explicitly in schema api");
    expect(e.code).toBeNull();
    expect(e.issues).toBeNull();
  });

  it("extracts SQLSTATE from a database rejection", () => {
    const e = apiErrorFromBody(400, { detail: "database operation rejected", code: "42703" });
    expect(e.code).toBe("42703");
    expect(e.short).toContain("42703");
  });

  it("summarises a pydantic issue list (422)", () => {
    const e = apiErrorFromBody(422, {
      detail: [{ type: "less_than_equal", loc: ["body", "row_cap"], msg: "Input should be less than or equal to 10000" }],
    });
    expect(e.issues).toHaveLength(1);
    expect(e.message).toContain("row_cap");
    expect(e.message).not.toContain("body");
  });

  it("handles an object detail (409 hash_conflict)", () => {
    const e = apiErrorFromBody(409, { detail: { reason: "hash_conflict", current_hash: "abc" } });
    expect(e.message).toBe("hash_conflict");
  });

  it("flags 401 as unauthorized", () => {
    const e = apiErrorFromBody(401, { detail: "invalid or revoked token" });
    expect(e.isUnauthorized).toBe(true);
  });
});
