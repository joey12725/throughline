/*
 * Toasts. A tiny zustand store so any module (including the API error boundary)
 * can raise one without prop-drilling. The <Toaster /> renders them.
 */
import { create } from "zustand";

export type ToastTone = "neutral" | "ok" | "warn" | "bad";

export interface Toast {
  id: number;
  tone: ToastTone;
  title: string;
  body?: string;
  /** ms before auto-dismiss; 0 keeps it until dismissed */
  duration: number;
  action?: { label: string; onClick: () => void };
}

interface ToastState {
  toasts: Toast[];
  push: (t: Omit<Toast, "id" | "duration"> & { duration?: number }) => number;
  dismiss: (id: number) => void;
}

let nextId = 1;

export const useToasts = create<ToastState>((set) => ({
  toasts: [],
  push: (t) => {
    const id = nextId++;
    const toast: Toast = { duration: t.tone === "bad" ? 8000 : 4500, ...t, id };
    set((s) => ({ toasts: [...s.toasts, toast] }));
    return id;
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })),
}));

/** Imperative helper for non-React code. */
export const toast = {
  info: (title: string, body?: string): number => useToasts.getState().push({ tone: "neutral", title, body }),
  ok: (title: string, body?: string): number => useToasts.getState().push({ tone: "ok", title, body }),
  warn: (title: string, body?: string): number => useToasts.getState().push({ tone: "warn", title, body }),
  error: (title: string, body?: string): number => useToasts.getState().push({ tone: "bad", title, body }),
};
