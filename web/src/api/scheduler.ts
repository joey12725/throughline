/*
 * A tiny priority scheduler for SQL statements. At most 4 run concurrently.
 * Visible panels enqueue at higher priority; panels that unmount abort. This
 * keeps the audit log (one row per request) and the database from being swamped
 * when a screen full of panels mounts at once.
 */

export type Priority = "visible" | "normal" | "background";

const WEIGHT: Record<Priority, number> = { visible: 0, normal: 1, background: 2 };

interface Job<T> {
  run: (signal: AbortSignal) => Promise<T>;
  priority: Priority;
  seq: number;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
  signal: AbortSignal;
}

export class Scheduler {
  private readonly max: number;
  private active = 0;
  private seq = 0;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private queue: Job<any>[] = [];

  constructor(max = 4) {
    this.max = max;
  }

  schedule<T>(
    run: (signal: AbortSignal) => Promise<T>,
    opts: { priority?: Priority; signal?: AbortSignal } = {},
  ): Promise<T> {
    const priority = opts.priority ?? "normal";
    const external = opts.signal;
    return new Promise<T>((resolve, reject) => {
      const controller = new AbortController();
      if (external) {
        if (external.aborted) {
          reject(new DOMException("Aborted", "AbortError"));
          return;
        }
        external.addEventListener("abort", () => controller.abort(), { once: true });
      }
      const job: Job<T> = {
        run,
        priority,
        seq: this.seq++,
        resolve,
        reject,
        signal: controller.signal,
      };
      // Drop queued (not-yet-started) jobs when their caller aborts.
      controller.signal.addEventListener(
        "abort",
        () => {
          const i = this.queue.indexOf(job);
          if (i >= 0) {
            this.queue.splice(i, 1);
            reject(new DOMException("Aborted", "AbortError"));
          }
        },
        { once: true },
      );
      this.enqueue(job);
      this.pump();
    });
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private enqueue(job: Job<any>): void {
    // Insert keeping the queue ordered by (priority weight, seq).
    const w = WEIGHT[job.priority];
    let i = this.queue.length;
    while (i > 0) {
      const prev = this.queue[i - 1]!;
      if (WEIGHT[prev.priority] < w || (WEIGHT[prev.priority] === w && prev.seq < job.seq)) break;
      i--;
    }
    this.queue.splice(i, 0, job);
  }

  private pump(): void {
    while (this.active < this.max && this.queue.length > 0) {
      const job = this.queue.shift()!;
      if (job.signal.aborted) continue;
      this.active++;
      job
        .run(job.signal)
        .then(job.resolve, job.reject)
        .finally(() => {
          this.active--;
          this.pump();
        });
    }
  }

  get inFlight(): number {
    return this.active;
  }
  get pending(): number {
    return this.queue.length;
  }
}

/** One shared scheduler for the whole app. */
export const sqlScheduler = new Scheduler(4);
