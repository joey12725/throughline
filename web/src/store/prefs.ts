/*
 * Preferences: theme, density, the privacy veil, map-tile opt-in and the helper
 * disclosure tier. The source of truth is tl.web_state (kind "prefs"); until that
 * is reachable (or when features.web_state is false) it falls back to
 * localStorage. Theme and density apply to <html> immediately so there is no
 * flash, and respond to OS changes while the theme is "system".
 */
import { create } from "zustand";

export type ThemePref = "system" | "nocturne" | "daybook";
export type Density = "observatory" | "workbench";
export type DisclosureTier = "schema" | "samples";

export interface Prefs {
  theme: ThemePref;
  density: Density;
  veil: boolean; // privacy veil on
  mapTiles: boolean; // street-level tiles opt-in
  disclosure: DisclosureTier;
  tz: string;
}

const LS_KEY = "tl.prefs";

function defaultTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function readLocal(): Partial<Prefs> {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) return JSON.parse(raw) as Partial<Prefs>;
  } catch {
    /* storage blocked */
  }
  return {};
}

const base: Prefs = {
  theme: "system",
  density: "observatory",
  veil: false,
  mapTiles: false,
  disclosure: "schema",
  tz: defaultTz(),
};

const initial: Prefs = { ...base, ...readLocal() };

interface PrefsState extends Prefs {
  set: <K extends keyof Prefs>(key: K, value: Prefs[K]) => void;
  /** merge a server-loaded prefs object without clobbering local-only tz */
  hydrate: (p: Partial<Prefs>) => void;
}

export const usePrefs = create<PrefsState>((setState, getState) => ({
  ...initial,
  set: (key, value) => {
    setState({ [key]: value } as Partial<PrefsState>);
    persist(getState());
    applyDocument(getState());
  },
  hydrate: (p) => {
    setState({ ...p });
    applyDocument(getState());
  },
}));

function persist(p: Prefs): void {
  try {
    const { theme, density, veil, mapTiles, disclosure, tz } = p;
    localStorage.setItem(LS_KEY, JSON.stringify({ theme, density, veil, mapTiles, disclosure, tz }));
  } catch {
    /* storage blocked */
  }
}

/** Resolve "system" to a concrete theme using the OS preference. */
export function resolveTheme(theme: ThemePref): "nocturne" | "daybook" {
  if (theme === "system") {
    try {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "nocturne" : "daybook";
    } catch {
      return "nocturne";
    }
  }
  return theme;
}

/** Apply theme + density + veil to <html>. Called on every change. */
export function applyDocument(p: Prefs): void {
  const root = document.documentElement;
  if (p.theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", p.theme);
  root.setAttribute("data-density", p.density);
  root.toggleAttribute("data-veil", p.veil);
}

/** Call once at start-up: apply current prefs and watch OS theme changes. */
export function initPrefsDocument(): () => void {
  applyDocument(usePrefs.getState());
  let mql: MediaQueryList | null = null;
  const onChange = (): void => {
    if (usePrefs.getState().theme === "system") applyDocument(usePrefs.getState());
  };
  try {
    mql = window.matchMedia("(prefers-color-scheme: dark)");
    mql.addEventListener("change", onChange);
  } catch {
    /* ignore */
  }
  return () => mql?.removeEventListener("change", onChange);
}
