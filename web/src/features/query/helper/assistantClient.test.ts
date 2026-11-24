import { describe, it, expect, vi } from "vitest";
import { startRun, followRun, AssistantUnavailable, type RunState, type Transport } from "./assistantClient";
import { ApiError } from "@/api/errors";

describe("assistant client", () => {
  it("startRun posts and returns the start response", async () => {
    const transport = vi.fn(async () => ({ run: "r_1", status: "running", provider: "anthropic", model: "m", tier: "schema" })) as unknown as Transport;
    const res = await startRun({ verb: "ask", question: "hi" }, transport);
    expect(res.run).toBe("r_1");
    expect(transport).toHaveBeenCalledWith("/web/assistant", expect.objectContaining({ method: "POST" }));
  });

  it("startRun maps a 404 to AssistantUnavailable", async () => {
    const transport = vi.fn(async () => {
      throw new ApiError({ status: 404, message: "not found" });
    }) as unknown as Transport;
    await expect(startRun({ verb: "ask" }, transport)).rejects.toBeInstanceOf(AssistantUnavailable);
  });

  it("followRun long-polls from the last seq and accumulates events until terminal", async () => {
    const calls: string[] = [];
    const pages: RunState[] = [
      {
        run: "r_1", status: "running", provider: "a", model: "m", tier: "schema",
        events: [{ seq: 1, kind: "step", tool: "describe", ms: 10 }],
        final: null, usage: null, error: null,
      },
      {
        run: "r_1", status: "done", provider: "a", model: "m", tier: "schema",
        events: [
          { seq: 2, kind: "step", tool: "run_sql", sql: "SELECT 1", row_count: 1, ms: 5 },
          { seq: 3, kind: "final" },
        ],
        final: { answer: "done", statement: "SELECT 1", spec: null },
        usage: { tokens_in: 10, tokens_out: 5, cost_usd: 0.01, steps: 2 },
        error: null,
      },
    ];
    let i = 0;
    const transport = vi.fn(async (path: string) => {
      calls.push(path);
      return pages[i++] as RunState;
    }) as unknown as Transport;

    const snapshots: RunState[] = [];
    for await (const s of followRun("r_1", { transport, wait: 1 })) snapshots.push(s);

    // First poll starts at after=0, second resumes after seq 1.
    expect(calls[0]).toContain("after=0");
    expect(calls[1]).toContain("after=1");
    // Events accumulate across pages; terminal status stops the loop.
    const last = snapshots[snapshots.length - 1] as RunState;
    expect(last.events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(last.status).toBe("done");
    expect(last.final?.answer).toBe("done");
  });

  it("followRun yields a failed snapshot when the run is gone (404)", async () => {
    const transport = vi.fn(async () => {
      throw new ApiError({ status: 404, message: "gone" });
    }) as unknown as Transport;
    const snapshots: RunState[] = [];
    for await (const s of followRun("r_dead", { transport })) snapshots.push(s);
    expect(snapshots).toHaveLength(1);
    expect(snapshots[0]?.status).toBe("failed");
    expect(snapshots[0]?.error).toMatch(/no longer available/i);
  });

  it("followRun stops promptly when aborted", async () => {
    const ctrl = new AbortController();
    ctrl.abort();
    const transport = vi.fn() as unknown as Transport;
    const snapshots: RunState[] = [];
    for await (const s of followRun("r_1", { transport, signal: ctrl.signal })) snapshots.push(s);
    expect(snapshots).toHaveLength(0);
    expect(transport).not.toHaveBeenCalled();
  });
});
