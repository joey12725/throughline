/*
 * Drawers. A right-hand, stackable, URL-addressable panel stack. Feature authors
 * register a renderer for a key and open it with params; the stack serialises
 * into the `d` search param, so any open record/channel/etc. is a link and the
 * back button pops one drawer. The registry keeps the shell free of per-feature
 * drawer code.
 */
import { create } from "zustand";
import type { ComponentType } from "react";

export interface DrawerSpec {
  key: string;
  params: Record<string, string>;
}

export interface DrawerRenderProps {
  params: Record<string, string>;
  close: () => void;
  depth: number;
}

const registry = new Map<string, ComponentType<DrawerRenderProps>>();

/** Register a drawer renderer under a key (call at module load). */
export function registerDrawer(key: string, component: ComponentType<DrawerRenderProps>): void {
  registry.set(key, component);
}

export function getDrawerRenderer(key: string): ComponentType<DrawerRenderProps> | undefined {
  return registry.get(key);
}

interface DrawerState {
  stack: DrawerSpec[];
  open: (key: string, params?: Record<string, string>) => void;
  replace: (key: string, params?: Record<string, string>) => void;
  closeTop: () => void;
  closeAll: () => void;
  setStack: (stack: DrawerSpec[]) => void;
}

export const useDrawers = create<DrawerState>((set) => ({
  stack: [],
  open: (key, params = {}) => set((s) => ({ stack: [...s.stack, { key, params }] })),
  replace: (key, params = {}) =>
    set((s) => ({ stack: [...s.stack.slice(0, -1), { key, params }] })),
  closeTop: () => set((s) => ({ stack: s.stack.slice(0, -1) })),
  closeAll: () => set({ stack: [] }),
  setStack: (stack) => set({ stack }),
}));

/** Serialise / parse the stack for the URL. */
export function encodeStack(stack: DrawerSpec[]): string {
  if (!stack.length) return "";
  return stack.map((d) => `${d.key}:${new URLSearchParams(d.params).toString()}`).join("|");
}

export function decodeStack(raw: string | null): DrawerSpec[] {
  if (!raw) return [];
  return raw
    .split("|")
    .map((part) => {
      const i = part.indexOf(":");
      if (i < 0) return { key: part, params: {} };
      const key = part.slice(0, i);
      const params = Object.fromEntries(new URLSearchParams(part.slice(i + 1)));
      return { key, params };
    })
    .filter((d) => d.key);
}
