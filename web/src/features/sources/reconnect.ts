/*
 * The exact steps to (re)connect a source, derived from its row. Tokens are
 * minted only on the box, so every command here runs there, never from the web.
 * The commands are the real throughline CLI forms (README, docs/writing-a-plugin)
 * with the source name interpolated — nothing is special-cased per source.
 */
import type { PluginRow } from "@/api/types";
import { deriveSourceState } from "./status";

export interface ReconnectStep {
  title: string;
  detail?: string;
  command?: string;
}

export function reconnectSteps(plugin: PluginRow, now: number = Date.now()): ReconnectStep[] {
  const name = plugin.name;
  const state = deriveSourceState(plugin, now).state;
  const steps: ReconnectStep[] = [];

  if (state === "failing") {
    steps.push({
      title: "Read the last error above and fix the cause",
      detail: "Credentials expired, an endpoint moved, or a rate limit — the error text names it.",
    });
  }
  if (state === "paused") {
    steps.push({ title: "This source is paused. Resume it on the box when you are ready." });
  }

  steps.push({
    title: "Confirm the source still has a plugin token",
    detail: "If it was revoked, mint a fresh one. Tokens are only ever created on the box.",
    command: `throughline token create ${name} --kind plugin --plugin ${name}`,
  });

  steps.push({
    title: "Run the source once and watch it land",
    detail: "A successful run posts a manifest, a heartbeat and records; this page turns green.",
    command: `throughline run ${name}`,
  });

  steps.push({
    title: "Check its status from the box",
    command: `throughline status ${name}`,
  });

  return steps;
}
