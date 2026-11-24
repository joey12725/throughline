/*
 * Command registry. The palette shows routes, channels and people derived from
 * data, plus a registry of actions that any feature can extend:
 *
 *   import { registerCommand } from "@/app/commands";
 *   registerCommand({ id: "query.run", group: "Query", label: "Run query",
 *                     perform: ({ navigate }) => navigate("/query") });
 *
 * Keeping it a plain registry means the shell carries no per-feature code and a
 * new screen can add its verbs without touching this file.
 */
import type { NavigateFunction } from "react-router-dom";
import type { ReactNode } from "react";

export interface CommandContext {
  navigate: NavigateFunction;
  close: () => void;
}

export interface Command {
  id: string;
  group: string;
  label: string;
  /** extra searchable text */
  keywords?: string;
  icon?: ReactNode;
  /** a trailing hint (e.g. a shortcut) */
  hint?: ReactNode;
  perform: (ctx: CommandContext) => void;
}

const registry = new Map<string, Command>();
const listeners = new Set<() => void>();
// A cached snapshot with a stable identity — required by useSyncExternalStore,
// which loops forever if getSnapshot returns a fresh array each call.
let snapshot: Command[] = [];

function refresh(): void {
  snapshot = Array.from(registry.values());
  listeners.forEach((l) => l());
}

export function registerCommand(cmd: Command): () => void {
  registry.set(cmd.id, cmd);
  refresh();
  return () => {
    registry.delete(cmd.id);
    refresh();
  };
}

export function listCommands(): Command[] {
  return snapshot;
}

export function subscribeCommands(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
