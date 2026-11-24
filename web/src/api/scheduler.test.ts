import { describe, it, expect } from "vitest";
import { Scheduler } from "./scheduler";

const tick = (): Promise<void> => new Promise((r) => setTimeout(r, 5));

describe("Scheduler", () => {
  it("runs at most `max` jobs concurrently", async () => {
    const s = new Scheduler(2);
    let active = 0;
    let peak = 0;
    const job = () => async () => {
      active++;
      peak = Math.max(peak, active);
      await tick();
      active--;
      return 1;
    };
    await Promise.all(Array.from({ length: 6 }, () => s.schedule(job())));
    expect(peak).toBeLessThanOrEqual(2);
  });

  it("runs visible-priority jobs before normal ones", async () => {
    const s = new Scheduler(1);
    const order: string[] = [];
    // Occupy the single slot so the next two queue.
    const blocker = s.schedule(async () => {
      await tick();
    });
    const normal = s.schedule(async () => { order.push("normal"); }, { priority: "normal" });
    const visible = s.schedule(async () => { order.push("visible"); }, { priority: "visible" });
    await Promise.all([blocker, normal, visible]);
    expect(order[0]).toBe("visible");
  });

  it("rejects a job whose signal is already aborted", async () => {
    const s = new Scheduler(1);
    const controller = new AbortController();
    controller.abort();
    await expect(s.schedule(async () => 1, { signal: controller.signal })).rejects.toMatchObject({
      name: "AbortError",
    });
  });
});
